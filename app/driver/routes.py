import logging
import math
from datetime import datetime, timedelta, timezone
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, current_app
from flask_login import login_required, current_user  # type: ignore # pyright: ignore[reportMissingImports]
from sqlalchemy.exc import IntegrityError
from app import role_required
from app.tenancy import require_feature
from app.models import db, Driver, Bus, Route, Stop, Trip, Student, Attendance, Alert, Notification, GpsLocation, EmergencyEvent, StudentAbsenceRequest, DriverSafetyTelemetry, User, AuditLog
from app.ai.engine import identify_student_from_face, check_bus_assignment, extract_face_features
from app.gps.tracker import process_gps_update, update_driver_bus_location

logger = logging.getLogger(__name__)
driver_bp = Blueprint('driver', __name__)


def _parse_json_payload():
    """Safely parse JSON body and reject malformed payloads."""
    data = request.get_json(silent=True)
    if data is None or not isinstance(data, dict):
        return None, (jsonify({'status': 'ERROR', 'result': 'rejected', 'message': 'Malformed or missing JSON object payload.'}), 400)
    return data, None


def _validate_client_event_id(raw_id, required=False):
    """Validate client idempotency identifier."""
    if raw_id is None or raw_id == '':
        if required:
            return None, 'client_event_id is required.'
        return None, None
    if not isinstance(raw_id, str):
        return None, 'Invalid client_event_id format.'
    cleaned = raw_id.strip()
    if not cleaned:
        if required:
            return None, 'client_event_id is required.'
        return None, None
    if len(cleaned) > 100:
        return None, 'client_event_id must be 1 to 100 characters.'
    return cleaned, None


def _validate_coordinates(lat_raw, lng_raw):
    """Validate optional latitude/longitude pair."""
    if lat_raw is None and lng_raw is None:
        return None, None, None
    if lat_raw is None or lng_raw is None:
        return None, None, 'Both latitude and longitude must be provided together.'
    if isinstance(lat_raw, bool) or isinstance(lng_raw, bool):
        return None, None, 'Invalid coordinate type.'
    try:
        lat_f = float(lat_raw)
        lng_f = float(lng_raw)
    except (ValueError, TypeError):
        return None, None, 'Invalid latitude or longitude value.'
    if math.isnan(lat_f) or math.isnan(lng_f) or math.isinf(lat_f) or math.isinf(lng_f):
        return None, None, 'Coordinates must be finite numbers.'
    if lat_f < -90.0 or lat_f > 90.0 or lng_f < -180.0 or lng_f > 180.0:
        return None, None, 'Coordinates are out of valid geographic range.'
    return lat_f, lng_f, None


def _parse_iso_timestamp(raw_ts):
    """Parse optional ISO-8601 timestamp normalized to naive UTC datetime."""
    if raw_ts is None or raw_ts == '':
        return datetime.utcnow(), None
    if not isinstance(raw_ts, str):
        return None, 'Invalid timestamp format.'
    try:
        dt = datetime.fromisoformat(raw_ts.strip().replace('Z', '+00:00'))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt, None
    except ValueError:
        return None, 'Invalid ISO-8601 timestamp.'


def _validate_description(raw_desc, default_text):
    """Validate and sanitize optional SOS description."""
    if raw_desc is None:
        return default_text, None
    if not isinstance(raw_desc, str):
        return None, 'Invalid description format.'
    cleaned = raw_desc.strip()
    if len(cleaned) > 1000:
        return None, 'Description exceeds 1000 characters.'
    return cleaned or default_text, None


def _resolve_sos_context(driver, trip_id_raw, allowed_trip_statuses):
    """Resolve and authorize trip, bus, and institution purely from trusted DB relationships."""
    if not current_user.institution_id or not driver.institution_id:
        return None, None, None, 'Institution context is missing for driver or user account.', 400
    if current_user.institution_id != driver.institution_id:
        return None, None, None, 'Access Denied: User and driver institution mismatch.', 403

    trip = None
    if trip_id_raw is not None and trip_id_raw != '':
        if isinstance(trip_id_raw, bool):
            return None, None, None, 'Invalid trip ID.', 400
        try:
            trip_id_int = int(trip_id_raw)
        except (ValueError, TypeError):
            return None, None, None, 'Invalid trip ID.', 400
        trip = db.session.get(Trip, trip_id_int)
        if not trip:
            return None, None, None, 'Trip reference not found.', 400
        if trip.driver_id != driver.id:
            return None, None, None, 'Access Denied: Unauthorized trip reference for SOS alert.', 403
        if trip.status not in allowed_trip_statuses:
            return None, None, None, f'Trip status {trip.status} is not valid for this SOS event.', 400
        if not trip.institution_id:
            return None, None, None, 'Trip institution context is missing.', 400
        if trip.institution_id != driver.institution_id or trip.institution_id != current_user.institution_id:
            return None, None, None, 'Access Denied: Trip institution mismatch.', 403

        bus_id = trip.bus_id
        bus = db.session.get(Bus, bus_id) if bus_id else None
        if not bus or not bus.institution_id or bus.institution_id != trip.institution_id:
            return None, None, None, 'Assigned trip bus institution context is missing or mismatched.', 403

        return trip, bus_id, trip.institution_id, None, 200

    # No trip supplied: resolve strictly via verified driver/user and assigned bus
    bus_id = driver.assigned_bus_id
    if not bus_id:
        return None, None, None, 'No assigned bus found for SOS alert.', 400
    bus = db.session.get(Bus, bus_id)
    if not bus:
        return None, None, None, 'Assigned bus record not found.', 400
    if not bus.institution_id:
        return None, None, None, 'Assigned bus institution context is missing.', 400
    if bus.institution_id != driver.institution_id or bus.institution_id != current_user.institution_id:
        return None, None, None, 'Access Denied: Assigned bus institution mismatch.', 403

    return None, bus_id, driver.institution_id, None, 200


def _sos_payload_matches(existing, trip_id, bus_id, inst_id, lat_f, lng_f, desc, triggered_at_dt, has_explicit_ts):
    """Return True if an existing EmergencyEvent matches the retried SOS payload."""
    if existing.trip_id != trip_id or existing.bus_id != bus_id or existing.institution_id != inst_id:
        return False
    if (existing.description or '') != (desc or ''):
        return False
    if (existing.latitude is None) != (lat_f is None) or (existing.longitude is None) != (lng_f is None):
        return False
    if lat_f is not None and (abs(existing.latitude - lat_f) > 1e-6 or abs(existing.longitude - lng_f) > 1e-6):
        return False
    if has_explicit_ts and existing.triggered_at and abs((existing.triggered_at - triggered_at_dt).total_seconds()) > 1.0:
        return False
    return True


def _persist_sos_event(driver, trip, bus_id, inst_id, lat_f, lng_f, desc, triggered_at_dt, client_event_id, is_offline=False, has_explicit_ts=False):
    """Atomically persist an EmergencyEvent, institution-scoped Alert, and admin Notifications with idempotency and conflict detection."""
    trip_id = trip.id if trip else None
    if client_event_id:
        existing = EmergencyEvent.query.filter_by(
            triggered_by_user_id=current_user.id,
            client_event_id=client_event_id
        ).first()
        if existing:
            if _sos_payload_matches(existing, trip_id, bus_id, inst_id, lat_f, lng_f, desc, triggered_at_dt, has_explicit_ts):
                return 'duplicate', existing, 'SOS event already recorded.'
            return 'conflict', existing, 'Conflict: client_event_id already used for a different SOS event.'

    bus = db.session.get(Bus, bus_id)
    bus_code = bus.bus_code if bus else (driver.assigned_bus.bus_code if driver.assigned_bus else 'N/A')
    mode_tag = " (Offline Queue Synced)" if is_offline else ""
    title = "🚨 CRITICAL EMERGENCY SOS (Offline Synced)" if is_offline else "🚨 CRITICAL EMERGENCY SOS"
    notif_msg = (
        f"Offline SOS Alert synced from Driver {current_user.full_name} on Bus {bus_code} (Triggered at {triggered_at_dt.strftime('%H:%M:%S UTC')})."
        if is_offline
        else f"SOS Alert received from Driver {current_user.full_name} on Bus {bus_code}."
    )

    sos_event = EmergencyEvent(
        institution_id=inst_id,
        bus_id=bus_id,
        trip_id=trip_id,
        triggered_by_user_id=current_user.id,
        client_event_id=client_event_id,
        latitude=lat_f,
        longitude=lng_f,
        description=desc,
        triggered_at=triggered_at_dt,
    )
    db.session.add(sos_event)

    sos_alert = Alert(
        institution_id=inst_id,
        bus_id=bus_id,
        trip_id=trip_id,
        alert_type='SOS_EMERGENCY',
        risk_score=50,
        severity='CRITICAL',
        description=(
            f"🚨 EMERGENCY SOS TRIGGERED{mode_tag} by Driver {current_user.full_name} "
            f"for Bus {bus_code}. Triggered at {triggered_at_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}."
        ),
    )
    db.session.add(sos_alert)

    from app.notifications.service import broadcast_admin_notification, dispatch_sms_for_institution_admins
    broadcast_admin_notification(
        title=title,
        message=notif_msg,
        category="EMERGENCY",
        institution_id=inst_id,
        commit=False,
    )

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if client_event_id:
            existing = EmergencyEvent.query.filter_by(
                triggered_by_user_id=current_user.id,
                client_event_id=client_event_id
            ).first()
            if existing:
                if _sos_payload_matches(existing, trip_id, bus_id, inst_id, lat_f, lng_f, desc, triggered_at_dt, has_explicit_ts):
                    return 'duplicate', existing, 'SOS event already recorded.'
                return 'conflict', existing, 'Conflict: client_event_id already used for a different SOS event.'
        return 'retryable_failure', None, 'Database integrity error recording SOS alert.'
    except Exception as exc:
        db.session.rollback()
        logger.error(f"Database failure persisting SOS event ({type(exc).__name__})")
        return 'retryable_failure', None, 'Database error recording SOS alert. Please retry.'

    dispatch_sms_for_institution_admins(inst_id, title, notif_msg, category="EMERGENCY")
    return 'accepted', sos_event, 'EMERGENCY SOS ALERT TRANSMITTED TO SCHOOL ADMIN'


def _resolve_attendance_event_idempotency(driver_id, trip_id, student, scan_action, client_event_id, already_status, already_message):
    """Look up an attendance event by (driver_id, client_event_id) and return conflict or already_processed if found."""
    if not client_event_id:
        return None
    existing_by_event = Attendance.query.filter_by(
        verified_by_driver_id=driver_id,
        client_event_id=client_event_id
    ).first()
    if not existing_by_event:
        return None
    if (
        existing_by_event.trip_id != trip_id
        or existing_by_event.student_id != student.id
        or existing_by_event.scan_type != scan_action
    ):
        return 'conflict', None, 'Conflict: client_event_id already used for a different attendance scan.'
    return 'already_processed', {
        'status': already_status,
        'student_id': student.id,
        'student_name': student.full_name,
        'roll_number': student.roll_number,
        'message': already_message,
    }, None


def _record_attendance_scan(driver, trip, student, confidence, scan_action, client_event_id=None, note_prefix="AI Face Recognition"):
    """Persist BOARD or DROP attendance with consent, institution, wrong-bus, idempotency, and conflict checks."""
    if student.institution_id != trip.institution_id:
        return 'rejected', None, 'Student institution mismatch.'

    fp = getattr(student, 'face_profile', None)
    if not fp or fp.parental_consent_given is not True or fp.is_archived is True:
        return 'rejected', None, 'Biometric consent is missing or revoked for this student.'

    # Idempotency + conflict check by client_event_id
    event_result = _resolve_attendance_event_idempotency(
        driver.id,
        trip.id,
        student,
        scan_action,
        client_event_id,
        already_status='DROP_VERIFIED' if scan_action == 'DROP' else 'ALREADY_BOARDED',
        already_message=f'{student.full_name} scan already processed.',
    )
    if event_result:
        return event_result

    is_correct_bus, _, assigned_bus_code = check_bus_assignment(student.id, trip.bus_id)
    if not is_correct_bus:
        alert = Alert(
            institution_id=trip.institution_id,
            bus_id=trip.bus_id,
            trip_id=trip.id,
            student_id=student.id,
            alert_type='WRONG_BUS',
            risk_score=40,
            severity='CRITICAL',
            description=f"WRONG BUS DETECTED! Student {student.full_name} (Assigned: {assigned_bus_code}) boarded {trip.bus.bus_code}."
        )
        db.session.add(alert)
        if student.parent and student.parent.user:
            db.session.add(Notification(
                user_id=student.parent.user.id,
                title="🚨 WRONG BUS ALERT",
                message=f"CRITICAL: Your child {student.full_name} was detected boarding BUS {trip.bus.bus_code} instead of assigned {assigned_bus_code}.",
                category="WRONG_BUS"
            ))
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error(f"Database failure recording WRONG_BUS alert ({type(exc).__name__})")
            return 'retryable_failure', None, 'Database error recording wrong-bus alert.'

        return 'rejected', {
            'status': 'WRONG_BUS',
            'student_id': student.id,
            'student_name': student.full_name,
            'assigned_bus': assigned_bus_code,
            'current_bus': trip.bus.bus_code,
            'message': f"WRONG BUS DETECTED! Assigned to {assigned_bus_code}, boarded {trip.bus.bus_code}.",
            'confidence': confidence,
        }, f"Wrong bus detected for {student.full_name}."

    existing_att = Attendance.query.filter_by(student_id=student.id, trip_id=trip.id).first()

    if scan_action == 'DROP':
        if existing_att and existing_att.is_dropped_off:
            return 'already_processed', {
                'status': 'ALREADY_DROPPED',
                'student_id': student.id,
                'student_name': student.full_name,
                'roll_number': student.roll_number,
                'message': f"{student.full_name} drop-off already recorded on this trip.",
            }, None

        if existing_att:
            existing_att.is_dropped_off = True
            existing_att.drop_verification_time = datetime.utcnow()
            existing_att.scan_type = 'DROP'
            if client_event_id and not existing_att.client_event_id:
                existing_att.client_event_id = client_event_id
        else:
            db.session.add(Attendance(
                student_id=student.id,
                trip_id=trip.id,
                bus_id=trip.bus_id,
                client_event_id=client_event_id,
                verification_status='VERIFIED',
                scan_type='DROP',
                is_dropped_off=True,
                drop_verification_time=datetime.utcnow(),
                verified_by_driver_id=trip.driver_id,
                notes=f"{note_prefix} Drop-off (Confidence: {confidence}%)"
            ))

        if student.parent and student.parent.user:
            db.session.add(Notification(
                user_id=student.parent.user.id,
                title="✅ Drop-Off Verified",
                message=f"Your child {student.full_name} was scanned off {trip.bus.bus_code} at drop stop.",
                category="SAFE_DROP"
            ))

        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            logger.warning(f"IntegrityError recording DROP attendance ({type(exc).__name__})")
            event_result = _resolve_attendance_event_idempotency(
                driver.id,
                trip.id,
                student,
                scan_action,
                client_event_id,
                already_status='ALREADY_DROPPED',
                already_message=f"{student.full_name} drop-off already recorded on this trip.",
            )
            if event_result:
                return event_result
            return 'retryable_failure', None, 'Database error recording drop-off attendance.'
        except Exception as exc:
            db.session.rollback()
            logger.error(f"Database failure recording DROP attendance ({type(exc).__name__})")
            return 'retryable_failure', None, 'Database error recording drop-off attendance.'

        return 'accepted', {
            'status': 'DROP_VERIFIED',
            'student_id': student.id,
            'student_name': student.full_name,
            'roll_number': student.roll_number,
            'confidence': confidence,
            'message': f"Student Drop Verified: {student.full_name} (BUS {trip.bus.bus_code})",
        }, None

    # BOARD flow
    if existing_att:
        return 'already_processed', {
            'status': 'ALREADY_BOARDED',
            'student_id': student.id,
            'student_name': student.full_name,
            'roll_number': student.roll_number,
            'message': f"{student.full_name} is already verified and recorded on this trip.",
        }, None

    db.session.add(Attendance(
        student_id=student.id,
        trip_id=trip.id,
        bus_id=trip.bus_id,
        client_event_id=client_event_id,
        verification_status='VERIFIED',
        scan_type='BOARD',
        verified_by_driver_id=trip.driver_id,
        notes=f"{note_prefix} (Confidence: {confidence}%)"
    ))

    if student.parent and student.parent.user:
        trip_time_str = datetime.now().strftime("%I:%M %p")
        db.session.add(Notification(
            user_id=student.parent.user.id,
            title="🚌 Boarding Confirmed",
            message=f"Your child {student.full_name} has boarded {trip.bus.bus_code} at {trip_time_str}.",
            category="BOARDING"
        ))

    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        logger.warning(f"IntegrityError recording BOARD attendance ({type(exc).__name__})")
        event_result = _resolve_attendance_event_idempotency(
            driver.id,
            trip.id,
            student,
            scan_action,
            client_event_id,
            already_status='ALREADY_BOARDED',
            already_message=f"{student.full_name} is already verified and recorded on this trip.",
        )
        if event_result:
            return event_result
        existing_trip_att = Attendance.query.filter_by(
            student_id=student.id,
            trip_id=trip.id,
        ).first()
        if (
            existing_trip_att
            and existing_trip_att.scan_type == 'BOARD'
            and existing_trip_att.verification_status in ('VERIFIED', 'MANUAL_OVERRIDE')
        ):
            return 'already_processed', {
                'status': 'ALREADY_BOARDED',
                'student_id': student.id,
                'student_name': student.full_name,
                'roll_number': student.roll_number,
                'message': f"{student.full_name} is already verified and recorded on this trip.",
            }, None
        return 'retryable_failure', None, 'Database error recording boarding attendance.'
    except Exception as exc:
        db.session.rollback()
        logger.error(f"Database failure recording BOARD attendance ({type(exc).__name__})")
        return 'retryable_failure', None, 'Database error recording boarding attendance.'

    return 'accepted', {
        'status': 'VERIFIED',
        'student_id': student.id,
        'student_name': student.full_name,
        'roll_number': student.roll_number,
        'confidence': confidence,
        'message': f"Student Verified: {student.full_name} (BUS {trip.bus.bus_code})",
    }, None

@driver_bp.route('/dashboard')
@login_required
@role_required('DRIVER')
def dashboard():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    if not driver:
        flash('Driver profile not found.', 'danger')
        return redirect(url_for('auth.login'))

    assigned_bus = Bus.query.get(driver.assigned_bus_id) if driver.assigned_bus_id else None
    assigned_route = Route.query.get(driver.assigned_route_id) if driver.assigned_route_id else None

    # Check for active trip
    active_trip = Trip.query.filter_by(driver_id=driver.id, status='IN_PROGRESS').first()

    # Roster stats
    total_assigned_students = Student.query.filter_by(assigned_bus_id=driver.assigned_bus_id).count() if driver.assigned_bus_id else 0
    boarded_count = 0
    pending_count = total_assigned_students

    if active_trip:
        boarded_count = Attendance.query.filter_by(trip_id=active_trip.id, verification_status='VERIFIED').count()
        pending_count = max(0, total_assigned_students - boarded_count)

    return render_template(
        'driver/dashboard.html',
        driver=driver,
        bus=assigned_bus,
        route=assigned_route,
        active_trip=active_trip,
        total_students=total_assigned_students,
        boarded_count=boarded_count,
        pending_count=pending_count
    )


@driver_bp.route('/trip/start', methods=['POST'])
@login_required
@role_required('DRIVER')
def start_trip():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    if not driver or not driver.assigned_bus_id or not driver.assigned_route_id:
        flash('You must have an assigned bus and route to start a trip.', 'warning')
        return redirect(url_for('driver.dashboard'))

    trip_type = request.form.get('trip_type', 'MORNING_PICKUP')

    # Check if there is already an active trip
    existing = Trip.query.filter_by(driver_id=driver.id, status='IN_PROGRESS').first()
    if existing:
        flash('You already have an active trip in progress.', 'info')
        return redirect(url_for('driver.camera_verify'))

    new_trip = Trip(
        institution_id=current_user.institution_id,
        bus_id=driver.assigned_bus_id,
        driver_id=driver.id,
        route_id=driver.assigned_route_id,
        trip_type=trip_type,
        status='IN_PROGRESS',
        start_time=datetime.utcnow()
    )
    db.session.add(new_trip)
    db.session.commit()

    flash(f'Started {trip_type.replace("_", " ")} trip for Bus {driver.assigned_bus.bus_code}.', 'success')
    return redirect(url_for('driver.camera_verify'))


@driver_bp.route('/camera-verify')
@login_required
@role_required('DRIVER')
def camera_verify():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    active_trip = Trip.query.filter_by(driver_id=driver.id, status='IN_PROGRESS').first()
    
    if not active_trip:
        flash('Please start a trip first before performing student face verification.', 'warning')
        return redirect(url_for('driver.dashboard'))

    assigned_students = Student.query.filter_by(assigned_bus_id=driver.assigned_bus_id).all()
    
    # Get boarded student IDs
    boarded_attendances = Attendance.query.filter_by(trip_id=active_trip.id).all()
    boarded_student_ids = {a.student_id for a in boarded_attendances}

    # Get active absence requests for today
    today_str = datetime.utcnow().strftime('%Y-%m-%d')
    active_absences = StudentAbsenceRequest.query.filter(
        StudentAbsenceRequest.absence_date == today_str,
        StudentAbsenceRequest.status != 'CANCELLED'
    ).all()
    absent_map = {ab.student_id: ab for ab in active_absences}

    return render_template(
        'driver/camera_verify.html',
        driver=driver,
        trip=active_trip,
        students=assigned_students,
        boarded_ids=boarded_student_ids,
        absent_map=absent_map
    )


@driver_bp.route('/api/verify-face', methods=['POST'])
@login_required
@role_required('DRIVER')
def api_verify_face():
    """REST API endpoint called by HTML5 Webcam driver UI."""
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    if not driver:
        return jsonify({'status': 'ERROR', 'message': 'Driver profile not found'}), 403

    data, err_resp = _parse_json_payload()
    if err_resp:
        return err_resp

    image_b64 = data.get('image')
    trip_id_raw = data.get('trip_id')
    scan_action = str(data.get('scan_action', 'BOARD')).strip().upper()
    client_event_id, cid_err = _validate_client_event_id(data.get('client_event_id'))

    if cid_err:
        return jsonify({'status': 'ERROR', 'message': cid_err}), 400
    if scan_action not in ('BOARD', 'DROP'):
        return jsonify({'status': 'ERROR', 'message': 'Invalid scan_action. Must be BOARD or DROP.'}), 400
    if not image_b64 or not isinstance(image_b64, str) or trip_id_raw is None:
        return jsonify({'status': 'ERROR', 'message': 'Missing image frame or trip ID'}), 400

    max_img_bytes = int(current_app.config.get('MAX_OFFLINE_IMAGE_SIZE_BYTES', 2 * 1024 * 1024))
    if len(image_b64) > int(max_img_bytes * 1.4) + 128:
        return jsonify({'status': 'ERROR', 'message': 'Image payload exceeds maximum allowed size.'}), 400

    try:
        trip_id = int(trip_id_raw)
    except (ValueError, TypeError):
        return jsonify({'status': 'ERROR', 'message': 'Invalid trip ID'}), 400

    trip = db.session.get(Trip, trip_id)
    if not trip or trip.status != 'IN_PROGRESS':
        return jsonify({'status': 'ERROR', 'message': 'Active trip session not found'}), 400

    if trip.driver_id != driver.id:
        return jsonify({'status': 'ERROR', 'message': 'Access Denied: You are not authorized for this trip session.'}), 403
    if not trip.institution_id or (driver.institution_id and trip.institution_id != driver.institution_id) or (current_user.institution_id and trip.institution_id != current_user.institution_id):
        return jsonify({'status': 'ERROR', 'message': 'Access Denied: Institution mismatch for trip session.'}), 403

    result = identify_student_from_face(image_b64, active_trip_id=trip.id, institution_id=trip.institution_id)
    if result['status'] != 'MATCHED':
        return jsonify(result)

    student = result['student']
    confidence = result['confidence']
    outcome, payload, err_msg = _record_attendance_scan(
        driver=driver,
        trip=trip,
        student=student,
        confidence=confidence,
        scan_action=scan_action,
        client_event_id=client_event_id,
        note_prefix="AI Face Recognition",
    )
    if outcome == 'retryable_failure':
        return jsonify({'status': 'ERROR', 'result': 'retryable_failure', 'message': err_msg}), 500
    if payload:
        return jsonify(payload)
    return jsonify({'status': 'ERROR', 'result': outcome, 'message': err_msg or 'Verification rejected.'}), 400


def _process_offline_sos_item(driver, sos_item):
    """Validate and process one offline SOS queue item with required idempotency and conflict detection."""
    if not isinstance(sos_item, dict):
        return {'id': None, 'type': 'SOS', 'status': 'rejected', 'message': 'Malformed SOS item.'}

    raw_id = sos_item.get('client_event_id') if sos_item.get('client_event_id') is not None else sos_item.get('id')
    client_event_id, cid_err = _validate_client_event_id(raw_id, required=True)
    if cid_err:
        return {'id': str(raw_id) if raw_id is not None else None, 'type': 'SOS', 'status': 'rejected', 'message': cid_err}

    trip, bus_id, inst_id, ctx_err, _ = _resolve_sos_context(
        driver, sos_item.get('trip_id'), allowed_trip_statuses={'IN_PROGRESS', 'COMPLETED'}
    )
    if ctx_err:
        return {'id': client_event_id, 'type': 'SOS', 'status': 'rejected', 'message': ctx_err}

    lat_f, lng_f, coord_err = _validate_coordinates(sos_item.get('latitude'), sos_item.get('longitude'))
    if coord_err:
        return {'id': client_event_id, 'type': 'SOS', 'status': 'rejected', 'message': coord_err}

    desc, desc_err = _validate_description(sos_item.get('description'), 'Emergency SOS triggered while offline')
    if desc_err:
        return {'id': client_event_id, 'type': 'SOS', 'status': 'rejected', 'message': desc_err}

    raw_ts = sos_item.get('triggered_at')
    has_explicit_ts = raw_ts is not None and raw_ts != ''
    triggered_at_dt, ts_err = _parse_iso_timestamp(raw_ts)
    if ts_err:
        return {'id': client_event_id, 'type': 'SOS', 'status': 'rejected', 'message': ts_err}

    outcome, event_obj, msg = _persist_sos_event(
        driver=driver,
        trip=trip,
        bus_id=bus_id,
        inst_id=inst_id,
        lat_f=lat_f,
        lng_f=lng_f,
        desc=desc,
        triggered_at_dt=triggered_at_dt,
        client_event_id=client_event_id,
        is_offline=True,
        has_explicit_ts=has_explicit_ts,
    )
    is_conflict = (outcome == 'conflict')
    result_status = 'already_processed' if outcome == 'duplicate' else ('rejected' if is_conflict else outcome)
    resp_item = {
        'id': client_event_id,
        'type': 'SOS',
        'status': result_status,
        'event_id': None if is_conflict else (event_obj.id if event_obj else None),
        'message': msg,
    }
    if is_conflict:
        resp_item['conflict'] = True
    return resp_item


def _process_offline_attendance_item(driver, item):
    """Validate and process one offline attendance queue item with required idempotency and conflict detection."""
    if not isinstance(item, dict):
        return {'id': None, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': 'Malformed queue item.'}

    raw_id = item.get('client_event_id') if item.get('client_event_id') is not None else item.get('id')
    client_event_id, cid_err = _validate_client_event_id(raw_id, required=True)
    if cid_err:
        return {'id': str(raw_id) if raw_id is not None else None, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': cid_err}

    scan_action = str(item.get('scan_action', 'BOARD')).strip().upper()
    if scan_action not in ('BOARD', 'DROP'):
        return {'id': client_event_id, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': 'Invalid scan_action. Must be BOARD or DROP.'}

    trip_id_raw = item.get('trip_id')
    img_b64 = item.get('image')
    if trip_id_raw is None or isinstance(trip_id_raw, bool) or not img_b64 or not isinstance(img_b64, str):
        return {'id': client_event_id, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': 'Missing valid trip_id or image payload.'}

    max_img_bytes = int(current_app.config.get('MAX_OFFLINE_IMAGE_SIZE_BYTES', 2 * 1024 * 1024))
    if len(img_b64) > int(max_img_bytes * 1.4) + 128:
        return {'id': client_event_id, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': 'Image payload exceeds maximum allowed size.'}

    raw_ts = item.get('timestamp')
    if raw_ts is not None and raw_ts != '':
        ts_dt, ts_err = _parse_iso_timestamp(raw_ts)
        if ts_err:
            return {'id': client_event_id, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': ts_err}
        expiry_hours = int(current_app.config.get('OFFLINE_QUEUE_EXPIRY_HOURS', 24))
        if datetime.utcnow() - ts_dt > timedelta(hours=expiry_hours):
            return {'id': client_event_id, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': 'Queued attendance scan has expired.'}

    try:
        trip_id_int = int(trip_id_raw)
    except (ValueError, TypeError):
        return {'id': client_event_id, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': 'Invalid trip_id.'}

    trip = db.session.get(Trip, trip_id_int)
    if not trip:
        return {'id': client_event_id, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': 'Trip reference not found.'}
    if trip.driver_id != driver.id:
        return {'id': client_event_id, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': 'Unauthorized trip ownership.'}
    if trip.status != 'IN_PROGRESS':
        return {'id': client_event_id, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': f'Trip status {trip.status} is not valid for attendance sync.'}
    if not trip.institution_id or not driver.institution_id or not current_user.institution_id or trip.institution_id != driver.institution_id or trip.institution_id != current_user.institution_id:
        return {'id': client_event_id, 'type': 'ATTENDANCE', 'status': 'rejected', 'message': 'Trip institution context missing or mismatched.'}

    existing_by_event = Attendance.query.filter_by(
        verified_by_driver_id=driver.id,
        client_event_id=client_event_id
    ).first()
    if existing_by_event and (existing_by_event.trip_id != trip.id or existing_by_event.scan_type != scan_action):
        return {
            'id': client_event_id,
            'type': 'ATTENDANCE',
            'scan_action': scan_action,
            'status': 'rejected',
            'conflict': True,
            'message': 'Conflict: client_event_id already used for a different trip or scan action.',
        }

    res = identify_student_from_face(img_b64, active_trip_id=trip.id, institution_id=trip.institution_id)
    if res.get('status') != 'MATCHED':
        return {
            'id': client_event_id,
            'type': 'ATTENDANCE',
            'scan_action': scan_action,
            'status': 'rejected',
            'message': res.get('message', 'Face could not be matched.'),
        }

    st = res['student']
    if existing_by_event:
        if existing_by_event.student_id != st.id:
            return {
                'id': client_event_id,
                'type': 'ATTENDANCE',
                'scan_action': scan_action,
                'status': 'rejected',
                'conflict': True,
                'message': 'Conflict: client_event_id already used for a different student.',
            }
        return {
            'id': client_event_id,
            'type': 'ATTENDANCE',
            'scan_action': scan_action,
            'status': 'already_processed',
            'student_id': st.id,
            'student_name': st.full_name,
            'message': 'Attendance scan already processed.',
        }

    outcome, payload, err_msg = _record_attendance_scan(
        driver=driver,
        trip=trip,
        student=st,
        confidence=res.get('confidence', 0.0),
        scan_action=scan_action,
        client_event_id=client_event_id,
        note_prefix="AI Face Scan (Offline Queue Auto-Synced)",
    )
    is_conflict = (outcome == 'conflict')
    final_status = 'rejected' if is_conflict else outcome
    resp_item = {
        'id': client_event_id,
        'type': 'ATTENDANCE',
        'scan_action': scan_action,
        'status': final_status,
        'student_id': st.id,
        'student_name': st.full_name,
        'message': (payload or {}).get('message') or err_msg or final_status,
    }
    if is_conflict:
        resp_item['conflict'] = True
    return resp_item


@driver_bp.route('/api/sync-offline-queue', methods=['POST'])
@login_required
@role_required('DRIVER')
def api_sync_offline_queue():
    """Processes background queued attendance scans and SOS emergencies collected when offline."""
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    if not driver:
        return jsonify({'status': 'ERROR', 'message': 'Driver profile not found'}), 403

    data, err_resp = _parse_json_payload()
    if err_resp:
        return err_resp

    queue = data.get('queue', [])
    sos_queue = data.get('sos_queue', [])
    if not isinstance(queue, list) or not isinstance(sos_queue, list):
        return jsonify({'status': 'ERROR', 'message': 'queue and sos_queue must be arrays.'}), 400

    max_items = int(current_app.config.get('MAX_OFFLINE_QUEUE_ITEMS', 50))
    if len(queue) + len(sos_queue) > max_items:
        return jsonify({'status': 'ERROR', 'message': f'Batch size exceeds maximum limit of {max_items} items.'}), 400

    attendance_items = []
    sos_items = list(sos_queue)
    for item in queue:
        if isinstance(item, dict) and str(item.get('type', '')).upper() == 'SOS':
            sos_items.append(item)
        else:
            attendance_items.append(item)

    attendance_results = [_process_offline_attendance_item(driver, item) for item in attendance_items]
    sos_results = [_process_offline_sos_item(driver, sos_item) for sos_item in sos_items]
    all_results = attendance_results + sos_results

    synced_count = sum(1 for r in attendance_results if r['status'] == 'accepted')
    sos_synced_count = sum(1 for r in sos_results if r['status'] == 'accepted')
    accepted_count = sum(1 for r in all_results if r['status'] == 'accepted')
    already_processed_count = sum(1 for r in all_results if r['status'] == 'already_processed')
    rejected_count = sum(1 for r in all_results if r['status'] == 'rejected')
    retryable_failure_count = sum(1 for r in all_results if r['status'] == 'retryable_failure')

    if len(all_results) == 0 or (rejected_count == 0 and retryable_failure_count == 0):
        overall_status = 'SUCCESS'
    elif accepted_count > 0 or already_processed_count > 0:
        overall_status = 'PARTIAL'
    else:
        overall_status = 'ERROR'

    return jsonify({
        'status': overall_status,
        'synced_count': synced_count,
        'sos_synced_count': sos_synced_count,
        'accepted_count': accepted_count,
        'already_processed_count': already_processed_count,
        'rejected_count': rejected_count,
        'retryable_failure_count': retryable_failure_count,
        'results': all_results,
        'attendance_results': attendance_results,
        'sos_results': sos_results,
    }), 200


@driver_bp.route('/manual-verify', methods=['POST'])
@login_required
@role_required('DRIVER')
def manual_verify():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    if not driver:
        flash('Driver profile not found.', 'danger')
        return redirect(url_for('auth.login'))

    student_id = request.form.get('student_id')
    trip_id = request.form.get('trip_id')
    reason = (request.form.get('reason') or '').strip() or 'Driver Manual Override'

    student = db.session.get(Student, int(student_id)) if student_id and str(student_id).isdigit() else None
    trip = db.session.get(Trip, int(trip_id)) if trip_id and str(trip_id).isdigit() else None

    if not student or not trip:
        flash('Invalid student or trip reference.', 'danger')
        return redirect(url_for('driver.dashboard'))

    if trip.driver_id != driver.id:
        flash('Unauthorized access: You cannot verify attendance on another driver\'s trip.', 'danger')
        return redirect(url_for('driver.dashboard'))

    if trip.status != 'IN_PROGRESS':
        flash(f'Manual verification denied: Trip is not active (status: {trip.status}).', 'danger')
        return redirect(url_for('driver.dashboard'))

    if (
        not current_user.institution_id
        or not driver.institution_id
        or not trip.institution_id
        or not student.institution_id
        or len({current_user.institution_id, driver.institution_id, trip.institution_id, student.institution_id}) != 1
    ):
        flash('Access Denied: Institution context missing or mismatched.', 'danger')
        return redirect(url_for('driver.camera_verify'))

    if not trip.bus_id:
        flash('Manual verification denied: Trip has no assigned bus.', 'danger')
        return redirect(url_for('driver.camera_verify'))

    is_assigned, bus_reason, _ = check_bus_assignment(student.id, trip.bus_id)
    if not is_assigned:
        flash(f'Access Denied: {student.full_name} is not assigned to this bus ({bus_reason}).', 'danger')
        return redirect(url_for('driver.camera_verify'))

    existing = Attendance.query.filter_by(student_id=student.id, trip_id=trip.id).first()
    if not existing:
        att = Attendance(
            student_id=student.id,
            trip_id=trip.id,
            bus_id=trip.bus_id,
            verification_status='MANUAL_OVERRIDE',
            verified_by_driver_id=trip.driver_id,
            notes=reason
        )
        db.session.add(att)

        # AuditLog entry for manual verification override tracking
        audit_log = AuditLog(
            institution_id=trip.institution_id,
            user_id=current_user.id,
            action="MANUAL_VERIFY_OVERRIDE",
            ip_address=request.remote_addr,
            details=f"Manual verification override performed by Driver {current_user.full_name} for Student {student.full_name} (Trip #{trip.id}, Bus {trip.bus.bus_code if trip.bus else 'N/A'}). Reason: {reason}"
        )
        db.session.add(audit_log)

        if student.parent and student.parent.user:
            n = Notification(
                user_id=student.parent.user.id,
                title="🚌 Boarding Confirmed (Driver Verification)",
                message=f"Your child {student.full_name} was manually verified on {trip.bus.bus_code if trip.bus else 'assigned bus'}.",
                category="BOARDING"
            )
            db.session.add(n)

        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error(f"Database error in manual_verify ({type(exc).__name__})")
            flash('Database error recording manual verification. Please try again.', 'danger')
            return redirect(url_for('driver.camera_verify'))

        flash(f'Manual verification recorded for {student.full_name}.', 'success')
    else:
        flash(f'{student.full_name} is already marked present.', 'info')

    return redirect(url_for('driver.camera_verify'))


@driver_bp.route('/roster')
@login_required
@role_required('DRIVER')
def roster():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    active_trip = Trip.query.filter_by(driver_id=driver.id, status='IN_PROGRESS').first()
    assigned_students = Student.query.filter_by(assigned_bus_id=driver.assigned_bus_id).all()

    boarded_list = []
    pending_list = []

    if active_trip:
        boarded_atts = Attendance.query.filter_by(trip_id=active_trip.id).all()
        boarded_ids = {a.student_id: a for a in boarded_atts}

        for st in assigned_students:
            if st.id in boarded_ids:
                boarded_list.append({'student': st, 'attendance': boarded_ids[st.id]})
            else:
                pending_list.append(st)
    else:
        pending_list = assigned_students

    # Get active absence requests for today
    today_str = datetime.utcnow().strftime('%Y-%m-%d')
    active_absences = StudentAbsenceRequest.query.filter(
        StudentAbsenceRequest.absence_date == today_str,
        StudentAbsenceRequest.status != 'CANCELLED'
    ).all()
    absent_map = {ab.student_id: ab for ab in active_absences}

    return render_template(
        'driver/roster.html',
        driver=driver,
        active_trip=active_trip,
        boarded=boarded_list,
        pending=pending_list,
        absent_map=absent_map
    )


@driver_bp.route('/api/toggle-online', methods=['POST'])
@login_required
@role_required('DRIVER')
def api_toggle_online():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    if not driver:
        return jsonify({'status': 'ERROR', 'message': 'Driver profile not found'}), 403

    data = request.get_json() or {}
    desired_status = data.get('is_online')

    if desired_status is None:
        driver.is_online = not driver.is_online
    else:
        driver.is_online = bool(desired_status)

    now = datetime.utcnow()
    driver.last_online_at = now

    assigned_bus = Bus.query.get(driver.assigned_bus_id) if driver.assigned_bus_id else None

    if driver.is_online:
        if assigned_bus:
            assigned_bus.status = 'ACTIVE'
            assigned_bus.last_location_update = now
        msg = f"Driver is ONLINE. Bus {assigned_bus.bus_code if assigned_bus else 'N/A'} is now ACTIVE."
    else:
        if assigned_bus:
            assigned_bus.status = 'INACTIVE'
        msg = f"Driver is OFFLINE. Bus {assigned_bus.bus_code if assigned_bus else 'N/A'} is now INACTIVE."

    db.session.commit()

    return jsonify({
        'status': 'SUCCESS',
        'is_online': driver.is_online,
        'bus_status': assigned_bus.status if assigned_bus else 'UNASSIGNED',
        'bus_code': assigned_bus.bus_code if assigned_bus else 'N/A',
        'message': msg
    })


def _resolve_gps_context(driver, trip_id_raw):
    """Validate driver, institution, trip, and assigned bus authorization for GPS updates."""
    if not current_user.institution_id or not driver.institution_id:
        return None, None, 'Institution context is missing for driver or user account.', 400
    if current_user.institution_id != driver.institution_id:
        return None, None, 'Access Denied: User and driver institution mismatch.', 403

    if trip_id_raw is not None and trip_id_raw != '':
        if isinstance(trip_id_raw, (bool, float)):
            return None, None, 'Invalid trip_id.', 400
        try:
            trip_id_int = int(str(trip_id_raw).strip()) if isinstance(trip_id_raw, str) else int(trip_id_raw)
        except (ValueError, TypeError):
            return None, None, 'Invalid trip_id.', 400
        if trip_id_int <= 0:
            return None, None, 'Invalid trip_id.', 400

        trip = db.session.get(Trip, trip_id_int)
        if not trip:
            return None, None, 'Trip reference not found.', 404
        if trip.driver_id != driver.id:
            return None, None, 'Access Denied: Unauthorized trip reference for GPS update.', 403
        if trip.status != 'IN_PROGRESS':
            return None, None, f'Trip status {trip.status} is not active for GPS updates.', 400
        if not trip.institution_id:
            return None, None, 'Trip institution context is missing.', 400
        if trip.institution_id != driver.institution_id or trip.institution_id != current_user.institution_id:
            return None, None, 'Access Denied: Trip institution mismatch.', 403

        if not trip.bus_id:
            return None, None, 'Trip has no assigned bus.', 400
        trip_bus = db.session.get(Bus, trip.bus_id)
        if not trip_bus:
            return None, None, 'Trip bus record not found.', 404
        if not trip_bus.institution_id or trip_bus.institution_id != trip.institution_id:
            return None, None, 'Access Denied: Trip bus institution context missing or mismatched.', 403
        if not driver.assigned_bus_id or driver.assigned_bus_id != trip.bus_id:
            return None, None, 'Access Denied: Trip bus does not match driver assigned bus.', 403
        if trip_bus.driver_id is not None and trip_bus.driver_id != driver.id:
            return None, None, 'Access Denied: Trip bus is assigned to another driver.', 403
        return trip, trip_bus, None, 200

    # Idle online tracking (no trip supplied): verify assigned bus if present
    assigned_bus = None
    if driver.assigned_bus_id:
        assigned_bus = db.session.get(Bus, driver.assigned_bus_id)
        if not assigned_bus:
            return None, None, 'Assigned bus record not found.', 403
        if not assigned_bus.institution_id or assigned_bus.institution_id != driver.institution_id:
            return None, None, 'Access Denied: Assigned bus institution mismatch.', 403
        if assigned_bus.driver_id is not None and assigned_bus.driver_id != driver.id:
            return None, None, 'Access Denied: Assigned bus is assigned to another driver.', 403
    return None, assigned_bus, None, 200


@driver_bp.route('/api/gps-update', methods=['POST'])
@login_required
@role_required('DRIVER')
def api_gps_update():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    if not driver:
        return jsonify({'status': 'ERROR', 'message': 'Driver profile not found'}), 403

    data, err_resp = _parse_json_payload()
    if err_resp:
        return err_resp

    lat = data.get('latitude')
    lng = data.get('longitude')
    if lat is None or lng is None or lat == '' or lng == '':
        return jsonify({'status': 'ERROR', 'message': 'Missing latitude or longitude'}), 400

    lat_f, lng_f, coord_err = _validate_coordinates(lat, lng)
    if coord_err or lat_f is None or lng_f is None:
        return jsonify({'status': 'ERROR', 'message': coord_err or 'Invalid latitude or longitude value'}), 400

    trip, target_bus, ctx_err, ctx_status = _resolve_gps_context(driver, data.get('trip_id'))
    if ctx_err:
        return jsonify({'status': 'ERROR', 'message': ctx_err}), ctx_status

    try:
        update_driver_bus_location(driver, lat_f, lng_f)
        res = {
            'status': 'SUCCESS',
            'lat': lat_f,
            'lng': lng_f,
            'driver_is_online': driver.is_online,
            'bus_code': target_bus.bus_code if target_bus else 'N/A',
            'bus_status': target_bus.status if target_bus else 'UNASSIGNED',
        }

        if trip is not None:
            trip_res = process_gps_update(trip.id, lat_f, lng_f)
            if trip_res.get('status') != 'SUCCESS':
                db.session.rollback()
                return jsonify({'status': 'ERROR', 'message': trip_res.get('message', 'Trip GPS update failed')}), 400
            res.update(trip_res)
        else:
            db.session.commit()

        return jsonify(res), 200
    except Exception as exc:
        db.session.rollback()
        logger.error(f"Database failure updating GPS location ({type(exc).__name__})")
        return jsonify({'status': 'ERROR', 'message': 'Database error updating GPS location.'}), 500


@driver_bp.route('/api/trigger-sos', methods=['POST'])
@login_required
@role_required('DRIVER')
def api_trigger_sos():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    if not driver:
        return jsonify({'status': 'ERROR', 'result': 'rejected', 'message': 'Driver profile not found'}), 403

    data, err_resp = _parse_json_payload()
    if err_resp:
        return err_resp

    raw_id = data.get('client_event_id') if data.get('client_event_id') is not None else data.get('id')
    client_event_id, cid_err = _validate_client_event_id(raw_id, required=True)
    if cid_err:
        return jsonify({'status': 'ERROR', 'result': 'rejected', 'message': cid_err}), 400

    trip, bus_id, inst_id, ctx_err, ctx_status = _resolve_sos_context(
        driver, data.get('trip_id'), allowed_trip_statuses={'IN_PROGRESS'}
    )
    if ctx_err:
        return jsonify({'status': 'ERROR', 'result': 'rejected', 'message': ctx_err}), ctx_status

    lat_f, lng_f, coord_err = _validate_coordinates(data.get('latitude'), data.get('longitude'))
    if coord_err:
        return jsonify({'status': 'ERROR', 'result': 'rejected', 'message': coord_err}), 400

    desc, desc_err = _validate_description(data.get('description'), 'Emergency SOS triggered by driver')
    if desc_err:
        return jsonify({'status': 'ERROR', 'result': 'rejected', 'message': desc_err}), 400

    raw_ts = data.get('triggered_at')
    has_explicit_ts = raw_ts is not None and raw_ts != ''
    triggered_at_dt, ts_err = _parse_iso_timestamp(raw_ts)
    if ts_err:
        return jsonify({'status': 'ERROR', 'result': 'rejected', 'message': ts_err}), 400

    outcome, event_obj, msg = _persist_sos_event(
        driver=driver,
        trip=trip,
        bus_id=bus_id,
        inst_id=inst_id,
        lat_f=lat_f,
        lng_f=lng_f,
        desc=desc,
        triggered_at_dt=triggered_at_dt,
        client_event_id=client_event_id,
        is_offline=False,
        has_explicit_ts=has_explicit_ts,
    )

    if outcome == 'accepted':
        return jsonify({
            'status': 'SUCCESS',
            'result': 'accepted',
            'event_id': event_obj.id,
            'client_event_id': client_event_id,
            'message': msg,
        }), 200
    if outcome == 'duplicate':
        return jsonify({
            'status': 'DUPLICATE',
            'result': 'duplicate',
            'event_id': event_obj.id,
            'client_event_id': client_event_id,
            'message': msg,
        }), 200
    if outcome == 'conflict':
        return jsonify({
            'status': 'ERROR',
            'result': 'rejected',
            'conflict': True,
            'client_event_id': client_event_id,
            'message': msg,
        }), 409
    return jsonify({
        'status': 'ERROR',
        'result': 'retryable_failure',
        'client_event_id': client_event_id,
        'message': msg,
    }), 500


def check_trip_no_shows(trip):
    """
    Evaluates trip completion for unboarded students who have no absence request on file today.
    Creates CRITICAL NO_SHOW alert and dispatches parent notification via Phase 1 pipeline.
    """
    if not trip or not trip.bus_id:
        return []

    from app.notifications.service import send_notification
    today_str = datetime.utcnow().strftime('%Y-%m-%d')
    assigned_students = Student.query.filter_by(assigned_bus_id=trip.bus_id).all()

    active_absences = StudentAbsenceRequest.query.filter(
        StudentAbsenceRequest.absence_date == today_str,
        StudentAbsenceRequest.status != 'CANCELLED'
    ).all()
    absent_student_ids = {ab.student_id for ab in active_absences}

    trip_attendances = Attendance.query.filter_by(trip_id=trip.id).all()
    boarded_student_ids = {att.student_id for att in trip_attendances}

    no_show_alerts = []
    bus_code = trip.bus.bus_code if trip.bus else 'N/A'

    for student in assigned_students:
        if student.id not in boarded_student_ids and student.id not in absent_student_ids:
            alert = Alert(
                institution_id=trip.institution_id,
                bus_id=trip.bus_id,
                trip_id=trip.id,
                student_id=student.id,
                alert_type='NO_SHOW',
                risk_score=45,
                severity='CRITICAL',
                description=f"NO-SHOW ALERT: Student {student.full_name} (Roll #{student.roll_number}) was expected for Trip #{trip.id} on Bus {bus_code} but never boarded and has no active absence request."
            )
            db.session.add(alert)
            no_show_alerts.append(alert)

            if student.parent and student.parent.user:
                send_notification(
                    user_id=student.parent.user.id,
                    title="🚨 NO-SHOW ALERT",
                    message=f"URGENT: Your child {student.full_name} did not board assigned Bus {bus_code} for today's trip and no absence was reported.",
                    category="NO_SHOW"
                )

    return no_show_alerts


def run_no_show_check():
    """
    Evaluates all currently in-progress trips across all institutions for missing/unboarded students.
    """
    active_trips = Trip.query.filter_by(status='IN_PROGRESS').all()
    all_alerts = []
    for trip in active_trips:
        all_alerts.extend(check_trip_no_shows(trip))
    return all_alerts


def check_unscanned_drop_offs(trip, stop_id=None):
    """
    Checks for students who boarded the trip but were not scanned off at their assigned drop stop.
    Triggers CRITICAL UNSCANNED_DROP alert and notifies parent and school admin.
    """
    if not trip or not trip.bus_id:
        return []

    from app.notifications.service import send_notification, broadcast_admin_notification

    query = Student.query.filter_by(assigned_bus_id=trip.bus_id)
    if stop_id:
        query = query.filter_by(drop_stop_id=stop_id)
    target_students = query.all()

    unscanned_alerts = []
    bus_code = trip.bus.bus_code if trip.bus else 'N/A'

    for student in target_students:
        att = Attendance.query.filter_by(student_id=student.id, trip_id=trip.id).first()
        # Student boarded (VERIFIED or MANUAL_OVERRIDE) but was NOT scanned off at drop stop
        if att and att.verification_status in ('VERIFIED', 'MANUAL_OVERRIDE') and not att.is_dropped_off:
            existing_alert = Alert.query.filter_by(
                trip_id=trip.id,
                student_id=student.id,
                alert_type='UNSCANNED_DROP',
                is_resolved=False
            ).first()

            if not existing_alert:
                stop_name = student.drop_stop.stop_name if student.drop_stop else 'assigned drop stop'
                alert = Alert(
                    institution_id=trip.institution_id,
                    bus_id=trip.bus_id,
                    trip_id=trip.id,
                    student_id=student.id,
                    alert_type='UNSCANNED_DROP',
                    risk_score=50,
                    severity='CRITICAL',
                    description=f"UNSCANNED DROP-OFF ALERT: Student {student.full_name} (Roll #{student.roll_number}) boarded Trip #{trip.id} but was NOT scanned off at {stop_name}."
                )
                db.session.add(alert)
                unscanned_alerts.append(alert)

                if student.parent and student.parent.user:
                    send_notification(
                        user_id=student.parent.user.id,
                        title="🚨 UNSCANNED DROP-OFF ALERT",
                        message=f"CRITICAL: Your child {student.full_name} was boarded on Bus {bus_code} but was not scanned off at drop stop {stop_name}.",
                        category="UNSCANNED_DROP"
                    )

                broadcast_admin_notification(
                    title="🚨 UNSCANNED DROP-OFF ALERT",
                    message=f"Driver on Bus {bus_code} concluded trip without scanning off boarded student {student.full_name} at drop stop {stop_name}.",
                    category="EMERGENCY",
                    institution_id=trip.institution_id or student.institution_id
                )

    return unscanned_alerts


@driver_bp.route('/trip/end', methods=['POST'])
@login_required
@role_required('DRIVER')
def end_trip():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    active_trip = Trip.query.filter_by(driver_id=driver.id, status='IN_PROGRESS').first()

    if not active_trip:
        flash('No active trip to conclude.', 'warning')
        return redirect(url_for('driver.dashboard'))

    active_trip.status = 'COMPLETED'
    active_trip.end_time = datetime.utcnow()

    # Trigger automated safety checks
    check_trip_no_shows(active_trip)
    check_unscanned_drop_offs(active_trip)

    # If morning pickup trip, notify arrival at school
    if active_trip.trip_type == 'MORNING_PICKUP':
        assigned_students = Student.query.filter_by(assigned_bus_id=driver.assigned_bus_id).all()
        for st in assigned_students:
            if st.parent and st.parent.user:
                n = Notification(
                    user_id=st.parent.user.id,
                    title="🏫 School Arrival Confirmed",
                    message=f"Bus {driver.assigned_bus.bus_code} has safely arrived at school.",
                    category="ARRIVALS"
                )
                db.session.add(n)

    db.session.commit()

    flash(f'Trip completed successfully for Bus {driver.assigned_bus.bus_code}.', 'success')
    return redirect(url_for('driver.dashboard'))


@driver_bp.route('/telemetry/log', methods=['POST'])
@login_required
@role_required('DRIVER')
@require_feature('driver_telemetry')
def log_telemetry():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    if not driver:
        return jsonify({'error': 'Driver profile not found'}), 404

    active_trip = Trip.query.filter_by(driver_id=driver.id, status='IN_PROGRESS').first()
    data = request.get_json() or {}

    fatigue_score = float(data.get('fatigue_score', 0.0))
    fatigue_level = str(data.get('fatigue_level', 'NORMAL'))
    eye_closure_sec = float(data.get('eye_closure_sec', 0.0))
    yawn_count = int(data.get('yawn_count', 0))
    distraction_event = str(data.get('distraction_event', 'NONE'))
    speed_kph = float(data.get('speed_kph', 35.0))
    overspeed_warning = bool(data.get('overspeed_warning', False))
    harsh_braking_warning = bool(data.get('harsh_braking_warning', False))

    # Calculate dynamic safety score
    safety_score = 100
    if fatigue_level == 'MILD_DROWSINESS':
        safety_score -= 25
    elif fatigue_level == 'CRITICAL_DROWSINESS':
        safety_score -= 60

    if overspeed_warning:
        safety_score -= 20
    if harsh_braking_warning:
        safety_score -= 15
    if distraction_event != 'NONE':
        safety_score -= 20

    safety_score = max(0, min(100, safety_score))

    telemetry = DriverSafetyTelemetry(
        driver_id=driver.id,
        bus_id=driver.assigned_bus_id,
        trip_id=active_trip.id if active_trip else None,
        fatigue_score=fatigue_score,
        fatigue_level=fatigue_level,
        eye_closure_sec=eye_closure_sec,
        yawn_count=yawn_count,
        distraction_event=distraction_event,
        speed_kph=speed_kph,
        overspeed_warning=overspeed_warning,
        harsh_braking_warning=harsh_braking_warning,
        safety_score=safety_score,
        timestamp=datetime.utcnow()
    )
    db.session.add(telemetry)

    alert_created = False
    assigned_bus = Bus.query.get(driver.assigned_bus_id) if driver.assigned_bus_id else None
    bus_code = assigned_bus.bus_code if assigned_bus else 'N/A'

    if fatigue_level == 'CRITICAL_DROWSINESS' or overspeed_warning or distraction_event == 'EYES_OFF_ROAD':
        alert_type = 'DRIVER_FATIGUE' if fatigue_level == 'CRITICAL_DROWSINESS' else ('OVERSPEEDING' if overspeed_warning else 'DISTRACTED_DRIVING')
        desc = f"⚠️ HIGH RISK ALERT: Driver {driver.full_name} (Bus {bus_code}) - {fatigue_level.replace('_', ' ')} (Score: {fatigue_score:.2f}, Speed: {speed_kph:.1f} km/h)"
        
        # Check if an unresolved alert of this type exists in the last 5 minutes to avoid flood
        existing_alert = Alert.query.filter_by(
            bus_id=driver.assigned_bus_id,
            alert_type=alert_type,
            is_resolved=False
        ).first()

        if not existing_alert:
            inst_id = (active_trip.institution_id if active_trip else None) or driver.institution_id or current_user.institution_id
            new_alert = Alert(
                institution_id=inst_id,
                bus_id=driver.assigned_bus_id,
                trip_id=active_trip.id if active_trip else None,
                alert_type=alert_type,
                risk_score=95 if fatigue_level == 'CRITICAL_DROWSINESS' else 85,
                severity='CRITICAL',
                description=desc
            )
            db.session.add(new_alert)
            alert_created = True

            # Notify institutional admins only
            from app.notifications.service import broadcast_admin_notification
            broadcast_admin_notification(
                title=f"🚨 CRITICAL DRIVER ALERT: {driver.full_name}",
                message=desc,
                category="EMERGENCY",
                institution_id=inst_id
            )

    db.session.commit()

    return jsonify({
        'success': True,
        'telemetry_id': telemetry.id,
        'fatigue_level': fatigue_level,
        'safety_score': safety_score,
        'alert_created': alert_created
    })


@driver_bp.route('/telemetry/simulate', methods=['POST'])
@login_required
@role_required('DRIVER')
@require_feature('driver_telemetry')
def simulate_telemetry():
    driver = Driver.query.filter_by(user_id=current_user.id).first()
    if not driver:
        flash('Driver profile not found.', 'danger')
        return redirect(url_for('driver.dashboard'))

    active_trip = Trip.query.filter_by(driver_id=driver.id, status='IN_PROGRESS').first()
    sim_type = request.form.get('sim_type', 'DROWSINESS')

    if sim_type == 'DROWSINESS':
        fatigue_score = 0.92
        fatigue_level = 'CRITICAL_DROWSINESS'
        eye_closure = 3.5
        yawn_cnt = 4
        distraction = 'EYES_OFF_ROAD'
        speed = 52.0
        overspeed = False
        harsh_brake = False
    elif sim_type == 'OVERSPEED':
        fatigue_score = 0.35
        fatigue_level = 'NORMAL'
        eye_closure = 0.2
        yawn_cnt = 0
        distraction = 'NONE'
        speed = 78.5
        overspeed = True
        harsh_brake = True
    else:
        fatigue_score = 0.05
        fatigue_level = 'NORMAL'
        eye_closure = 0.1
        yawn_cnt = 0
        distraction = 'NONE'
        speed = 38.0
        overspeed = False
        harsh_brake = False

    safety_score = 40 if sim_type == 'DROWSINESS' else (65 if sim_type == 'OVERSPEED' else 98)

    telemetry = DriverSafetyTelemetry(
        driver_id=driver.id,
        bus_id=driver.assigned_bus_id,
        trip_id=active_trip.id if active_trip else None,
        fatigue_score=fatigue_score,
        fatigue_level=fatigue_level,
        eye_closure_sec=eye_closure,
        yawn_count=yawn_cnt,
        distraction_event=distraction,
        speed_kph=speed,
        overspeed_warning=overspeed,
        harsh_braking_warning=harsh_brake,
        safety_score=safety_score,
        timestamp=datetime.utcnow()
    )
    db.session.add(telemetry)

    if sim_type in ['DROWSINESS', 'OVERSPEED']:
        desc = f"⚠️ SIMULATED SAFETY ALERT: Driver {driver.full_name} - {sim_type} triggered (Speed: {speed} km/h)"
        new_alert = Alert(
            institution_id=current_user.institution_id,
            bus_id=driver.assigned_bus_id,
            trip_id=active_trip.id if active_trip else None,
            alert_type='DRIVER_FATIGUE' if sim_type == 'DROWSINESS' else 'OVERSPEEDING',
            risk_score=95,
            severity='CRITICAL',
            description=desc
        )
        db.session.add(new_alert)

    db.session.commit()

    flash(f"Simulated telemetry event '{sim_type}' logged successfully. Safety Score: {safety_score}%.", "warning" if sim_type != 'NORMAL' else "success")
    return redirect(request.referrer or url_for('driver.dashboard'))

