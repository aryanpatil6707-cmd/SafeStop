import base64
import json
import logging
from datetime import datetime, date
import numpy as np
import cv2
from config import Config
from app.models import db, Student, Bus, Trip, Attendance, Alert, Notification, SafeDropConfirmation, TemporaryBusReassignment, FaceProfile

logger = logging.getLogger(__name__)

def decode_image_base64(b64_string, max_bytes=None):
    """Decode base64 image data to OpenCV BGR image numpy array without logging raw image data."""
    if not b64_string or not isinstance(b64_string, str):
        return None
    try:
        payload = b64_string.split(',', 1)[1] if ',' in b64_string else b64_string
        limit = max_bytes or getattr(Config, 'MAX_OFFLINE_IMAGE_SIZE_BYTES', 2 * 1024 * 1024)
        # Approximate decoded size guard prior to full decode
        if len(payload) > int(limit * 1.4) + 64:
            logger.warning("Rejected oversized base64 image payload.")
            return None
        img_bytes = base64.b64decode(payload, validate=True)
        if len(img_bytes) > limit:
            logger.warning("Rejected decoded image exceeding maximum byte limit.")
            return None
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return img
    except Exception as e:
        logger.error(f"Error decoding base64 image ({type(e).__name__})")
        return None


def extract_face_features(cv_img):
    """
    Extract normalized feature vector from facial ROI using OpenCV.
    Converts face to 64x64 grayscale, applies CLAHE contrast equalization, 
    and returns a normalized float vector.
    """
    if cv_img is None:
        return None
    
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    
    # Simple Haar Cascade detection or center ROI if cascade not loaded
    faces = []
    if hasattr(cv2, 'CascadeClassifier') and hasattr(cv2, 'data'):
        try:
            cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
            face_cascade = cv2.CascadeClassifier(cascade_path)
            min_box_size = getattr(Config, 'FACE_MIN_BOUNDING_BOX_SIZE', 60)
            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(min_box_size, min_box_size))
        except Exception as e:
            logger.warning(f"Haar cascade detection skipped: {e}")
    
    if len(faces) > 0:
        # Pick largest face
        x, y, w, h = max(faces, key=lambda rect: rect[2] * rect[3])
        face_roi = gray[y:y+h, x:x+w]
    else:
        # Fallback to full crop centered
        face_roi = gray

    # Resize to standard 64x64
    resized = cv2.resize(face_roi, (64, 64))
    
    # Histogram equalization for lighting invariance
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    equalized = clahe.apply(resized)
    
    # Flatten and normalize vector
    flat = equalized.astype(np.float32).flatten()
    norm = np.linalg.norm(flat)
    if norm > 0:
        flat = flat / norm
        
    return flat.tolist()


def compare_feature_vectors(vec1, vec2):
    """Compute cosine similarity score between two feature vectors with strict dimension validation."""
    if not vec1 or not vec2:
        return 0.0
    v1 = np.array(vec1, dtype=np.float32)
    v2 = np.array(vec2, dtype=np.float32)
    
    # Dimension mismatch returns 0.0 rather than truncating vectors
    if len(v1) != len(v2):
        logger.warning(f"Vector dimension mismatch: {len(v1)} vs {len(v2)}")
        return 0.0
        
    dot_product = np.dot(v1, v2)
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
        
    similarity = dot_product / (norm1 * norm2)
    return float(similarity)


def identify_student_from_face(image_b64, active_trip_id=None, institution_id=None):
    """
    Given a camera capture image base64, identify matching student in DB.
    Enforces strict tenant isolation and explicit parental biometric consent:
    - Fails closed if institution context cannot be resolved.
    - Matches ONLY profiles with parental_consent_given=True and is_archived=False.
    """
    # Resolve and validate target institution_id BEFORE decoding/searching
    target_inst_id = institution_id
    if active_trip_id is not None:
        trip = db.session.get(Trip, active_trip_id)
        if not trip:
            return {'status': 'ERROR', 'message': 'Invalid trip reference.'}
        if target_inst_id is not None and trip.institution_id != target_inst_id:
            return {'status': 'ERROR', 'message': 'Trip institution mismatch.'}
        if target_inst_id is None:
            target_inst_id = trip.institution_id

    if not target_inst_id:
        return {'status': 'ERROR', 'message': 'Institution context is required for face identification.'}

    cv_img = decode_image_base64(image_b64)
    if cv_img is None:
        return {'status': 'ERROR', 'message': 'Invalid camera frame'}

    query_vector = extract_face_features(cv_img)
    if query_vector is None:
        return {'status': 'NO_FACE', 'message': 'No facial features detected. Please align camera.'}

    # Fetch face profiles strictly scoped to institution with active parental consent
    profiles = (
        FaceProfile.query
        .join(Student, FaceProfile.student_id == Student.id)
        .filter(
            FaceProfile.parental_consent_given == True,
            FaceProfile.is_archived == False,
            Student.institution_id == target_inst_id,
        )
        .all()
    )
    if not profiles:
        return {'status': 'NO_PROFILES', 'message': 'No consented student face profiles available for this institution.'}

    best_match_profile = None
    best_match_student = None
    highest_similarity = 0.0

    for prof in profiles:
        stored_vec = prof.get_vector()
        sim = compare_feature_vectors(query_vector, stored_vec)
        if sim > highest_similarity:
            highest_similarity = sim
            best_match_profile = prof
            best_match_student = prof.student

    # Synchronized Threshold comparison from Config + final consent/tenant verification
    threshold = getattr(Config, 'FACE_MATCH_SIMILARITY_THRESHOLD', 0.55)
    if (
        best_match_profile
        and best_match_student
        and best_match_profile.parental_consent_given is True
        and best_match_profile.is_archived is False
        and best_match_student.institution_id == target_inst_id
        and highest_similarity >= threshold
    ):
        confidence = min(99.0, round(highest_similarity * 100, 1))
        return {
            'status': 'MATCHED',
            'student': best_match_student,
            'confidence': confidence,
            'similarity': highest_similarity
        }
    else:
        return {
            'status': 'UNRECOGNIZED',
            'message': 'Student face not recognized with sufficient confidence.',
            'best_similarity': float(highest_similarity) if best_match_student else 0.0
        }


def check_bus_assignment(student_id, current_bus_id):
    """
    Verify if student belongs on current_bus_id.
    Includes check for valid active temporary bus reassignments.
    Returns (is_valid, reason, assigned_bus_code).
    """
    student = db.session.get(Student, student_id)
    if not student:
        return False, "Student record not found", "N/A"

    assigned_bus = db.session.get(Bus, student.assigned_bus_id) if student.assigned_bus_id else None
    assigned_code = assigned_bus.bus_code if assigned_bus else "UNASSIGNED"

    # 1. Direct assigned bus match
    if student.assigned_bus_id == current_bus_id:
        return True, "Assigned bus match", assigned_code

    # 2. Check temporary reassignment
    today_str = date.today().isoformat()
    reassignment = TemporaryBusReassignment.query.filter(
        TemporaryBusReassignment.student_id == student_id,
        TemporaryBusReassignment.temporary_bus_id == current_bus_id,
        TemporaryBusReassignment.is_active == True,
        TemporaryBusReassignment.start_date <= today_str,
        TemporaryBusReassignment.end_date >= today_str
    ).first()

    if reassignment:
        return True, f"Approved temporary reassignment ({reassignment.reason})", assigned_code

    # Mismatch! Wrong Bus!
    current_bus = db.session.get(Bus, current_bus_id)
    current_code = current_bus.bus_code if current_bus else f"BUS-{current_bus_id}"
    return False, f"Assigned to {assigned_code}, but detected boarding {current_code}", assigned_code


def calculate_explainable_risk_score(trip_id=None, student_id=None, bus_id=None):
    """
    Calculates explainable risk score (0 to 100) with detailed reasoning breakdown.
    Rules:
    - Wrong Bus Detection: +40
    - Route Deviation Detected: +30
    - Bus Delay > 15m: +20
    - Boarding at Unusual Time/Location: +10
    - SOS Emergency Alert Active: +50
    Returns dict: {'score': int, 'level': 'LOW'|'MEDIUM'|'HIGH', 'breakdown': list of str}
    """
    score = 0
    breakdown = []

    # Check emergency SOS
    query_bus_id = bus_id
    if trip_id and not query_bus_id:
        t = db.session.get(Trip, trip_id)
        if t:
            query_bus_id = t.bus_id

    if query_bus_id:
        active_sos = Alert.query.filter_by(bus_id=query_bus_id, alert_type='SOS_EMERGENCY', is_resolved=False).first()
        if active_sos:
            score += 50
            breakdown.append("CRITICAL: Active Emergency/SOS Alert (+50)")

        wrong_bus_alerts = Alert.query.filter_by(bus_id=query_bus_id, alert_type='WRONG_BUS', is_resolved=False).count()
        if wrong_bus_alerts > 0:
            score += 40
            breakdown.append(f"HIGH RISK: {wrong_bus_alerts} Wrong Bus boarding alerts (+40)")

        deviation_alerts = Alert.query.filter_by(bus_id=query_bus_id, alert_type='ROUTE_DEVIATION', is_resolved=False).count()
        if deviation_alerts > 0:
            score += 30
            breakdown.append(f"WARNING: {deviation_alerts} Route Deviation alerts (+30)")

    if trip_id:
        tr = db.session.get(Trip, trip_id)
        if tr and tr.delay_minutes > 15:
            score += 20
            breakdown.append(f"DELAY: Bus delayed by {tr.delay_minutes} minutes (+20)")

    score = min(100, score)
    level = 'LOW'
    if score >= 60:
        level = 'CRITICAL'
    elif score >= 35:
        level = 'HIGH'
    elif score >= 15:
        level = 'MEDIUM'

    return {
        'score': score,
        'level': level,
        'breakdown': breakdown if breakdown else ["Normal operation — No security alerts active."]
    }
