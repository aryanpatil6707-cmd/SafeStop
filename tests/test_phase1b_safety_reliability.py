import base64
import json
import logging
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.ai.engine import extract_face_features, identify_student_from_face
from app.models import (
    Alert,
    Attendance,
    AuditLog,
    Bus,
    Driver,
    EmergencyEvent,
    FaceProfile,
    Institution,
    Notification,
    Parent,
    Route,
    Student,
    TemporaryBusReassignment,
    Trip,
    User,
    db,
)


def _make_patterned_face_b64(pattern_kind="horizontal"):
    """Build a deterministic synthetic 128x128 BGR image encoded as JPEG data URL."""
    img = np.zeros((128, 128, 3), dtype=np.uint8)
    if pattern_kind == "horizontal":
        for y in range(128):
            val = int((y / 127.0) * 255)
            img[y, :, :] = (val, 255 - val, (val * 2) % 256)
    elif pattern_kind == "vertical":
        # Inverted top-to-bottom + alternating stripes to keep similarity low vs horizontal
        for y in range(128):
            for x in range(128):
                val = 255 - int((y / 127.0) * 255) if (x % 4 < 2) else 0
                img[y, x, :] = (val, val, val)
    else:
        for y in range(128):
            for x in range(128):
                img[y, x, :] = (255 if ((x // 16 + y // 16) % 2 == 0) else 20)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    return img, f"data:image/jpeg;base64,{b64}"


@pytest.fixture
def phase1b_app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["NOTIFICATION_PROVIDER"] = "console"
    app.config["MAX_OFFLINE_QUEUE_ITEMS"] = 5
    app.config["MAX_OFFLINE_IMAGE_SIZE_BYTES"] = 50000
    app.config["OFFLINE_QUEUE_EXPIRY_HOURS"] = 24

    with app.test_client() as client:
        with app.app_context():
            db.drop_all()
            db.create_all()

            inst_a = Institution(name="Phase1B School A", slug="p1b-school-a", plan_tier="ENTERPRISE")
            inst_b = Institution(name="Phase1B School B", slug="p1b-school-b", plan_tier="ENTERPRISE")
            db.session.add_all([inst_a, inst_b])
            db.session.flush()

            admin_a = User(institution_id=inst_a.id, email="p1b_admin_a@example.com", role="ADMIN", full_name="Admin A")
            admin_a.set_password("StrongPassA!1")
            admin_b = User(institution_id=inst_b.id, email="p1b_admin_b@example.com", role="ADMIN", full_name="Admin B")
            admin_b.set_password("StrongPassB!1")

            driver_a_u = User(institution_id=inst_a.id, email="p1b_driver_a@example.com", role="DRIVER", full_name="Driver A")
            driver_a_u.set_password("StrongPassA!2")
            driver_b_u = User(institution_id=inst_b.id, email="p1b_driver_b@example.com", role="DRIVER", full_name="Driver B")
            driver_b_u.set_password("StrongPassB!2")

            parent_a_u = User(institution_id=inst_a.id, email="p1b_parent_a@example.com", role="PARENT", full_name="Parent A")
            parent_a_u.set_password("StrongPassA!3")
            parent_b_u = User(institution_id=inst_b.id, email="p1b_parent_b@example.com", role="PARENT", full_name="Parent B")
            parent_b_u.set_password("StrongPassB!3")

            db.session.add_all([admin_a, admin_b, driver_a_u, driver_b_u, parent_a_u, parent_b_u])
            db.session.flush()

            parent_a = Parent(institution_id=inst_a.id, user_id=parent_a_u.id)
            parent_b = Parent(institution_id=inst_b.id, user_id=parent_b_u.id)
            driver_a = Driver(institution_id=inst_a.id, user_id=driver_a_u.id, full_name="Driver A", phone="+1-555-0101", license_number="LIC-P1B-A")
            driver_b = Driver(institution_id=inst_b.id, user_id=driver_b_u.id, full_name="Driver B", phone="+1-555-0201", license_number="LIC-P1B-B")
            db.session.add_all([parent_a, parent_b, driver_a, driver_b])
            db.session.flush()

            route_a = Route(institution_id=inst_a.id, name="Route A")
            bus_a1 = Bus(institution_id=inst_a.id, registration_number="REG-P1B-A1", bus_code="BUS-P1B-A1", driver_id=driver_a.id)
            bus_a2 = Bus(institution_id=inst_a.id, registration_number="REG-P1B-A2", bus_code="BUS-P1B-A2")
            route_b = Route(institution_id=inst_b.id, name="Route B")
            bus_b1 = Bus(institution_id=inst_b.id, registration_number="REG-P1B-B1", bus_code="BUS-P1B-B1", driver_id=driver_b.id)
            db.session.add_all([route_a, bus_a1, bus_a2, route_b, bus_b1])
            db.session.flush()

            driver_a.assigned_bus_id = bus_a1.id
            driver_a.assigned_route_id = route_a.id
            bus_a1.route_id = route_a.id

            driver_b.assigned_bus_id = bus_b1.id
            driver_b.assigned_route_id = route_b.id
            bus_b1.route_id = route_b.id

            student_a1 = Student(
                institution_id=inst_a.id,
                full_name="Student A1",
                roll_number="P1B-A1",
                parent_id=parent_a.id,
                assigned_bus_id=bus_a1.id,
                assigned_route_id=route_a.id,
            )
            student_a2_other_bus = Student(
                institution_id=inst_a.id,
                full_name="Student A2 OtherBus",
                roll_number="P1B-A2",
                parent_id=parent_a.id,
                assigned_bus_id=bus_a2.id,
                assigned_route_id=route_a.id,
            )
            student_b1 = Student(
                institution_id=inst_b.id,
                full_name="Student B1",
                roll_number="P1B-B1",
                parent_id=parent_b.id,
                assigned_bus_id=bus_b1.id,
                assigned_route_id=route_b.id,
            )
            db.session.add_all([student_a1, student_a2_other_bus, student_b1])
            db.session.flush()

            img_a1, b64_a1 = _make_patterned_face_b64("horizontal")
            img_a2, b64_a2 = _make_patterned_face_b64("vertical")
            img_b1, b64_b1 = _make_patterned_face_b64("checker")

            fp_a1 = FaceProfile(student_id=student_a1.id, image_path="a1.jpg")
            fp_a1.set_vector(extract_face_features(img_a1))
            fp_a1.grant_consent(user_id=parent_a_u.id)

            fp_a2 = FaceProfile(student_id=student_a2_other_bus.id, image_path="a2.jpg")
            fp_a2.set_vector(extract_face_features(img_a2))
            fp_a2.grant_consent(user_id=parent_a_u.id)

            fp_b1 = FaceProfile(student_id=student_b1.id, image_path="b1.jpg")
            fp_b1.set_vector(extract_face_features(img_b1))
            fp_b1.grant_consent(user_id=parent_b_u.id)

            db.session.add_all([fp_a1, fp_a2, fp_b1])

            trip_a = Trip(
                institution_id=inst_a.id,
                bus_id=bus_a1.id,
                driver_id=driver_a.id,
                route_id=route_a.id,
                trip_type="MORNING_PICKUP",
                status="IN_PROGRESS",
                start_time=datetime.utcnow(),
            )
            trip_b = Trip(
                institution_id=inst_b.id,
                bus_id=bus_b1.id,
                driver_id=driver_b.id,
                route_id=route_b.id,
                trip_type="MORNING_PICKUP",
                status="IN_PROGRESS",
                start_time=datetime.utcnow(),
            )
            db.session.add_all([trip_a, trip_b])
            db.session.commit()

            ctx = {
                "app": app,
                "client": client,
                "inst_a_id": inst_a.id,
                "inst_b_id": inst_b.id,
                "admin_a_id": admin_a.id,
                "admin_b_id": admin_b.id,
                "driver_a_u_id": driver_a_u.id,
                "driver_b_u_id": driver_b_u.id,
                "driver_a_id": driver_a.id,
                "driver_b_id": driver_b.id,
                "bus_a1_id": bus_a1.id,
                "bus_a2_id": bus_a2.id,
                "bus_b1_id": bus_b1.id,
                "route_a_id": route_a.id,
                "parent_a_id": parent_a.id,
                "parent_a_u_id": parent_a_u.id,
                "student_a1_id": student_a1.id,
                "student_a2_id": student_a2_other_bus.id,
                "student_b1_id": student_b1.id,
                "fp_a1_id": fp_a1.id,
                "fp_b1_id": fp_b1.id,
                "trip_a_id": trip_a.id,
                "trip_b_id": trip_b.id,
                "b64_a1": b64_a1,
                "b64_a2": b64_a2,
                "b64_b1": b64_b1,
            }
        yield ctx


def _login_as(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


# ----------------
# Part 1: SOS Reliability Tests
# ----------------

def test_sos_valid_online_and_duplicate_idempotency(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    payload = {
        "client_event_id": "sos-evt-001",
        "trip_id": phase1b_app["trip_a_id"],
        "latitude": 0.0,
        "longitude": 0.0,
        "description": "Flat tire on highway",
        # Spoofed fields must be ignored in favor of server-side relationships
        "institution_id": phase1b_app["inst_b_id"],
        "bus_id": 999999,
        "driver_id": phase1b_app["driver_b_id"],
    }

    res1 = client.post("/driver/api/trigger-sos", json=payload)
    assert res1.status_code == 200
    d1 = res1.get_json()
    assert d1["status"] == "SUCCESS"
    assert d1["result"] == "accepted"

    res2 = client.post("/driver/api/trigger-sos", json=payload)
    assert res2.status_code == 200
    d2 = res2.get_json()
    assert d2["result"] == "duplicate"
    assert d2["event_id"] == d1["event_id"]

    with app.app_context():
        assert EmergencyEvent.query.filter_by(client_event_id="sos-evt-001").count() == 1
        ev = EmergencyEvent.query.filter_by(client_event_id="sos-evt-001").first()
        assert ev.latitude == 0.0
        assert ev.longitude == 0.0
        assert ev.institution_id == phase1b_app["inst_a_id"]
        assert Alert.query.filter_by(alert_type="SOS_EMERGENCY", institution_id=phase1b_app["inst_a_id"]).count() == 1
        assert Alert.query.filter_by(alert_type="SOS_EMERGENCY", institution_id=phase1b_app["inst_b_id"]).count() == 0
        assert Notification.query.filter_by(user_id=phase1b_app["admin_a_id"]).count() == 1
        assert Notification.query.filter_by(user_id=phase1b_app["admin_b_id"]).count() == 0


def test_sos_invalid_trip_ownership_rejected(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    res_other = client.post("/driver/api/trigger-sos", json={
        "client_event_id": "sos-bad-owner",
        "trip_id": phase1b_app["trip_b_id"],
    })
    assert res_other.status_code == 403
    assert res_other.get_json()["result"] == "rejected"

    res_missing_trip = client.post("/driver/api/trigger-sos", json={
        "client_event_id": "sos-missing-trip",
        "trip_id": 999999,
    })
    assert res_missing_trip.status_code == 400
    assert res_missing_trip.get_json()["result"] == "rejected"

    with app.app_context():
        assert EmergencyEvent.query.count() == 0
        assert Alert.query.count() == 0


def test_sos_missing_institution_fails_closed(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]

    with app.app_context():
        u = db.session.get(User, phase1b_app["driver_a_u_id"])
        u.institution_id = None
        db.session.commit()

    _login_as(client, phase1b_app["driver_a_u_id"])

    # Simulate a driver record lacking any institution_id (no trip supplied)
    orphan_driver = SimpleNamespace(
        id=phase1b_app["driver_a_id"],
        user_id=phase1b_app["driver_a_u_id"],
        institution_id=None,
        assigned_bus_id=phase1b_app["bus_a1_id"],
        assigned_bus=None,
    )
    with patch("app.driver.routes.Driver") as mock_driver_cls:
        mock_driver_cls.query.filter_by.return_value.first.return_value = orphan_driver
        res = client.post("/driver/api/trigger-sos", json={"client_event_id": "sos-no-inst"})

    assert res.status_code == 400
    assert res.get_json()["result"] == "rejected"

    with app.app_context():
        assert EmergencyEvent.query.count() == 0
        assert Alert.query.count() == 0


def test_sos_invalid_coordinates_and_timestamps_rejected(phase1b_app):
    client = phase1b_app["client"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    bad_payloads = [
        {"client_event_id": "coord-1", "trip_id": phase1b_app["trip_a_id"], "latitude": 95.0, "longitude": 72.0},
        {"client_event_id": "coord-2", "trip_id": phase1b_app["trip_a_id"], "latitude": 19.0, "longitude": -181.0},
        {"client_event_id": "coord-3", "trip_id": phase1b_app["trip_a_id"], "latitude": 19.0},
        {"client_event_id": "coord-4", "trip_id": phase1b_app["trip_a_id"], "latitude": True, "longitude": 72.0},
        {"client_event_id": "coord-5", "trip_id": phase1b_app["trip_a_id"], "latitude": "not-a-num", "longitude": 72.0},
        {"client_event_id": "coord-6", "trip_id": phase1b_app["trip_a_id"], "triggered_at": "invalid-date-string"},
    ]
    for payload in bad_payloads:
        res = client.post("/driver/api/trigger-sos", json=payload)
        assert res.status_code == 400
        assert res.get_json()["result"] == "rejected"


def test_offline_sos_duplicate_retry_idempotent(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    sync_body = {
        "sos_queue": [
            {
                "client_event_id": "offline-sos-evt-1",
                "trip_id": phase1b_app["trip_a_id"],
                "latitude": 19.076,
                "longitude": 72.877,
                "description": "Offline engine failure",
                "triggered_at": "2026-11-27T10:00:00Z",
            }
        ]
    }
    res1 = client.post("/driver/api/sync-offline-queue", json=sync_body)
    assert res1.status_code == 200
    d1 = res1.get_json()
    assert d1["status"] == "SUCCESS"
    assert d1["sos_synced_count"] == 1
    assert d1["sos_results"][0]["status"] == "accepted"

    res2 = client.post("/driver/api/sync-offline-queue", json=sync_body)
    assert res2.status_code == 200
    d2 = res2.get_json()
    assert d2["status"] == "SUCCESS"
    assert d2["sos_synced_count"] == 0
    assert d2["already_processed_count"] == 1
    assert d2["sos_results"][0]["status"] == "already_processed"

    with app.app_context():
        assert EmergencyEvent.query.filter_by(client_event_id="offline-sos-evt-1").count() == 1
        assert Alert.query.filter_by(alert_type="SOS_EMERGENCY").count() == 1


def test_sos_database_failure_returns_retryable_failure(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    with patch("app.driver.routes.db.session.commit", side_effect=RuntimeError("DB write timeout")):
        res_online = client.post("/driver/api/trigger-sos", json={
            "client_event_id": "sos-db-fail-online",
            "trip_id": phase1b_app["trip_a_id"],
        })
    assert res_online.status_code == 500
    assert res_online.get_json()["result"] == "retryable_failure"

    with patch("app.driver.routes.db.session.commit", side_effect=RuntimeError("DB write timeout")):
        res_offline = client.post("/driver/api/sync-offline-queue", json={
            "sos_queue": [{"client_event_id": "sos-db-fail-offline", "trip_id": phase1b_app["trip_a_id"]}]
        })
    assert res_offline.status_code == 200
    d_off = res_offline.get_json()
    assert d_off["status"] == "ERROR"
    assert d_off["retryable_failure_count"] == 1
    assert d_off["sos_results"][0]["status"] == "retryable_failure"

    with app.app_context():
        assert EmergencyEvent.query.count() == 0
        assert Alert.query.count() == 0


# ----------------
# Part 2: Offline Attendance Queue Reliability Tests
# ----------------

def test_offline_attendance_board_and_drop_sync(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    # 1. BOARD sync
    res_board = client.post("/driver/api/sync-offline-queue", json={
        "queue": [{
            "id": "att-board-1",
            "trip_id": phase1b_app["trip_a_id"],
            "image": phase1b_app["b64_a1"],
            "scan_action": "BOARD",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    })
    assert res_board.status_code == 200
    db_res = res_board.get_json()
    assert db_res["status"] == "SUCCESS"
    assert db_res["synced_count"] == 1
    assert db_res["attendance_results"][0]["status"] == "accepted"
    assert db_res["attendance_results"][0]["scan_action"] == "BOARD"

    with app.app_context():
        att = Attendance.query.filter_by(student_id=phase1b_app["student_a1_id"], trip_id=phase1b_app["trip_a_id"]).first()
        assert att is not None
        assert att.scan_type == "BOARD"
        assert att.is_dropped_off is False

    # 2. DROP sync
    res_drop = client.post("/driver/api/sync-offline-queue", json={
        "queue": [{
            "id": "att-drop-1",
            "trip_id": phase1b_app["trip_a_id"],
            "image": phase1b_app["b64_a1"],
            "scan_action": "DROP",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    })
    assert res_drop.status_code == 200
    dd_res = res_drop.get_json()
    assert dd_res["status"] == "SUCCESS"
    assert dd_res["synced_count"] == 1
    assert dd_res["attendance_results"][0]["status"] == "accepted"
    assert dd_res["attendance_results"][0]["scan_action"] == "DROP"

    with app.app_context():
        att = Attendance.query.filter_by(student_id=phase1b_app["student_a1_id"], trip_id=phase1b_app["trip_a_id"]).first()
        assert att.scan_type == "DROP"
        assert att.is_dropped_off is True
        assert att.drop_verification_time is not None


def test_offline_attendance_duplicate_sync_idempotent(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    item = {
        "id": "att-dup-1",
        "trip_id": phase1b_app["trip_a_id"],
        "image": phase1b_app["b64_a1"],
        "scan_action": "BOARD",
    }
    r1 = client.post("/driver/api/sync-offline-queue", json={"queue": [item]})
    assert r1.get_json()["attendance_results"][0]["status"] == "accepted"

    # Exact retry with same id
    r2 = client.post("/driver/api/sync-offline-queue", json={"queue": [item]})
    d2 = r2.get_json()
    assert d2["status"] == "SUCCESS"
    assert d2["synced_count"] == 0
    assert d2["already_processed_count"] == 1
    assert d2["attendance_results"][0]["status"] == "already_processed"

    with app.app_context():
        assert Attendance.query.filter_by(student_id=phase1b_app["student_a1_id"], trip_id=phase1b_app["trip_a_id"]).count() == 1


def test_offline_attendance_invalid_trip_ownership(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    res = client.post("/driver/api/sync-offline-queue", json={
        "queue": [{
            "id": "att-wrong-owner",
            "trip_id": phase1b_app["trip_b_id"],
            "image": phase1b_app["b64_a1"],
            "scan_action": "BOARD",
        }]
    })
    assert res.status_code == 200
    d = res.get_json()
    assert d["status"] == "ERROR"
    assert d["rejected_count"] == 1
    assert d["attendance_results"][0]["status"] == "rejected"

    with app.app_context():
        assert Attendance.query.count() == 0


def test_offline_attendance_wrong_bus_detection(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    # Student A2 is assigned to Bus A2, scanned during Bus A1's trip
    res = client.post("/driver/api/sync-offline-queue", json={
        "queue": [{
            "id": "att-wrong-bus-1",
            "trip_id": phase1b_app["trip_a_id"],
            "image": phase1b_app["b64_a2"],
            "scan_action": "BOARD",
        }]
    })
    assert res.status_code == 200
    d = res.get_json()
    assert d["rejected_count"] == 1
    assert d["attendance_results"][0]["status"] == "rejected"

    with app.app_context():
        assert Attendance.query.count() == 0
        wb_alert = Alert.query.filter_by(alert_type="WRONG_BUS", institution_id=phase1b_app["inst_a_id"]).first()
        assert wb_alert is not None
        assert wb_alert.student_id == phase1b_app["student_a2_id"]


def test_offline_attendance_partial_batch_and_retryable_failure(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    real_commit = db.session.commit
    call_counter = {"n": 0}

    def commit_fail_on_second():
        call_counter["n"] += 1
        if call_counter["n"] == 2:
            raise RuntimeError("Transient DB lock")
        return real_commit()

    # Item 1: valid -> accepted (commit #1)
    # Item 2: invalid trip -> rejected (no commit)
    # Item 3: valid DROP -> commit #2 fails -> retryable_failure
    with patch("app.driver.routes.db.session.commit", side_effect=commit_fail_on_second):
        res = client.post("/driver/api/sync-offline-queue", json={
            "queue": [
                {"id": "batch-ok-1", "trip_id": phase1b_app["trip_a_id"], "image": phase1b_app["b64_a1"], "scan_action": "BOARD"},
                {"id": "batch-bad-2", "trip_id": phase1b_app["trip_b_id"], "image": phase1b_app["b64_a1"], "scan_action": "BOARD"},
                {"id": "batch-retry-3", "trip_id": phase1b_app["trip_a_id"], "image": phase1b_app["b64_a1"], "scan_action": "DROP"},
            ]
        })

    assert res.status_code == 200
    d = res.get_json()
    assert d["status"] == "PARTIAL"
    assert d["accepted_count"] == 1
    assert d["rejected_count"] == 1
    assert d["retryable_failure_count"] == 1

    by_id = {r["id"]: r["status"] for r in d["results"]}
    assert by_id == {
        "batch-ok-1": "accepted",
        "batch-bad-2": "rejected",
        "batch-retry-3": "retryable_failure",
    }

    # Retry only the retained retryable item -> now succeeds
    res_retry = client.post("/driver/api/sync-offline-queue", json={
        "queue": [{"id": "batch-retry-3", "trip_id": phase1b_app["trip_a_id"], "image": phase1b_app["b64_a1"], "scan_action": "DROP"}]
    })
    assert res_retry.status_code == 200
    assert res_retry.get_json()["attendance_results"][0]["status"] == "accepted"


def test_offline_attendance_malformed_payload_and_limits(phase1b_app, caplog):
    client = phase1b_app["client"]
    caplog.set_level(logging.DEBUG)
    _login_as(client, phase1b_app["driver_a_u_id"])

    # Malformed JSON body
    res_bad_json = client.post("/driver/api/sync-offline-queue", data="{not-json", content_type="application/json")
    assert res_bad_json.status_code == 400

    # Non-list queue
    res_bad_type = client.post("/driver/api/sync-offline-queue", json={"queue": "bad"})
    assert res_bad_type.status_code == 400

    # Queue size limit (>5 configured in fixture)
    oversized_queue = [{"id": f"item-{i}", "trip_id": phase1b_app["trip_a_id"], "image": "abc"} for i in range(6)]
    res_too_many = client.post("/driver/api/sync-offline-queue", json={"queue": oversized_queue})
    assert res_too_many.status_code == 400

    # Oversized image, expired timestamp, and malformed item inside queue
    huge_b64 = "data:image/jpeg;base64," + ("A" * 100000)
    expired_ts = (datetime.utcnow() - timedelta(hours=30)).isoformat() + "Z"
    res_items = client.post("/driver/api/sync-offline-queue", json={
        "queue": [
            "not-a-dict",
            {"id": "too-big", "trip_id": phase1b_app["trip_a_id"], "image": huge_b64},
            {"id": "too-old", "trip_id": phase1b_app["trip_a_id"], "image": phase1b_app["b64_a1"], "timestamp": expired_ts},
        ]
    })
    assert res_items.status_code == 200
    d = res_items.get_json()
    assert d["rejected_count"] == 3
    assert "AAAA" not in json.dumps(d)
    assert "AAAA" not in caplog.text


# ----------------
# Part 3: Biometric Consent & Tenant Isolation Tests
# ----------------

def test_biometric_consent_enforcement_and_isolation(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    with app.app_context():
        # 1. Missing institution context fails closed
        res_no_inst = identify_student_from_face(phase1b_app["b64_a1"], active_trip_id=None, institution_id=None)
        assert res_no_inst["status"] == "ERROR"

        # 2. Valid consent in Institution A matches Student A1
        res_valid = identify_student_from_face(phase1b_app["b64_a1"], active_trip_id=phase1b_app["trip_a_id"])
        assert res_valid["status"] == "MATCHED"
        assert res_valid["student"].id == phase1b_app["student_a1_id"]

        # 3. Cross-institution isolation: Institution B's face cannot match on Institution A's trip
        res_cross = identify_student_from_face(phase1b_app["b64_b1"], active_trip_id=phase1b_app["trip_a_id"])
        assert res_cross["status"] != "MATCHED" or res_cross["student"].institution_id == phase1b_app["inst_a_id"]
        if res_cross["status"] == "MATCHED":
            assert res_cross["student"].id != phase1b_app["student_b1_id"]

        # 4. Revoked consent immediately blocks matching Student A1
        fp_a1 = db.session.get(FaceProfile, phase1b_app["fp_a1_id"])
        fp_a1.revoke_consent()
        db.session.commit()

        res_revoked = identify_student_from_face(phase1b_app["b64_a1"], active_trip_id=phase1b_app["trip_a_id"])
        assert res_revoked["status"] != "MATCHED" or res_revoked["student"].id != phase1b_app["student_a1_id"]

        # With Student A2 also unconsented, Institution A has zero consented profiles
        fp_a2 = FaceProfile.query.filter_by(student_id=phase1b_app["student_a2_id"]).first()
        fp_a2.revoke_consent()
        db.session.commit()

        res_none_consented = identify_student_from_face(phase1b_app["b64_a1"], active_trip_id=phase1b_app["trip_a_id"])
        assert res_none_consented["status"] == "NO_PROFILES"

        # 5. Archived profile (even if consent boolean were True) never matches
        fp_a1.parental_consent_given = True
        fp_a1.is_archived = True
        db.session.commit()

        res_archived = identify_student_from_face(phase1b_app["b64_a1"], active_trip_id=phase1b_app["trip_a_id"])
        assert res_archived["status"] == "NO_PROFILES"

    # 6. /driver/api/verify-face cannot record attendance when consent is not active
    res_http = client.post("/driver/api/verify-face", json={
        "trip_id": phase1b_app["trip_a_id"],
        "image": phase1b_app["b64_a1"],
        "scan_action": "BOARD",
    })
    assert res_http.status_code == 200
    assert res_http.get_json()["status"] not in ("VERIFIED", "DROP_VERIFIED")
    with app.app_context():
        assert Attendance.query.count() == 0


# ----------------
# Part 4: CSRF Protection Enforcement Tests
# ----------------

def test_csrf_enforced_on_driver_camera_sync_and_sos_endpoints(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    app.config["WTF_CSRF_ENABLED"] = True
    _login_as(client, phase1b_app["driver_a_u_id"])

    # Missing CSRF token -> 400
    for endpoint, payload in (
        ("/driver/api/verify-face", {"trip_id": phase1b_app["trip_a_id"], "image": phase1b_app["b64_a1"]}),
        ("/driver/api/sync-offline-queue", {"queue": []}),
        ("/driver/api/trigger-sos", {"trip_id": phase1b_app["trip_a_id"], "client_event_id": "csrf-sos-1"}),
    ):
        res_no_csrf = client.post(endpoint, json=payload)
        assert res_no_csrf.status_code == 400

    # Extract CSRF token from rendered camera_verify page meta tag
    page = client.get("/driver/camera-verify")
    assert page.status_code == 200
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.get_data(as_text=True))
    assert match is not None
    csrf_token = match.group(1)

    headers = {"X-CSRFToken": csrf_token}
    res_sync_ok = client.post("/driver/api/sync-offline-queue", json={"queue": []}, headers=headers)
    assert res_sync_ok.status_code == 200

    res_sos_ok = client.post(
        "/driver/api/trigger-sos",
        json={"trip_id": phase1b_app["trip_a_id"], "client_event_id": "csrf-sos-ok"},
        headers=headers,
    )
    assert res_sos_ok.status_code == 200
    assert res_sos_ok.get_json()["result"] == "accepted"


# ----------------
# Phase 1B Correction Round Tests
# ----------------

def test_required_event_ids_and_conflict_detection(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    # 1. Missing or blank client_event_id rejected on online SOS
    for bad_body in (
        {"trip_id": phase1b_app["trip_a_id"], "description": "No ID"},
        {"trip_id": phase1b_app["trip_a_id"], "client_event_id": "   "},
        {"trip_id": phase1b_app["trip_a_id"], "client_event_id": 12345},
    ):
        res_bad = client.post("/driver/api/trigger-sos", json=bad_body)
        assert res_bad.status_code == 400
        assert res_bad.get_json()["result"] == "rejected"

    # 2. Missing client_event_id rejected on offline SOS & offline attendance queues
    res_sync_missing = client.post("/driver/api/sync-offline-queue", json={
        "queue": [{"trip_id": phase1b_app["trip_a_id"], "image": phase1b_app["b64_a1"], "scan_action": "BOARD"}],
        "sos_queue": [{"trip_id": phase1b_app["trip_a_id"], "description": "Offline missing ID"}],
    })
    assert res_sync_missing.status_code == 200
    d_miss = res_sync_missing.get_json()
    assert d_miss["rejected_count"] == 2
    assert d_miss["attendance_results"][0]["status"] == "rejected"
    assert d_miss["sos_results"][0]["status"] == "rejected"

    # 3. Online SOS conflict when same client_event_id reused with different payload/trip
    res_ok = client.post("/driver/api/trigger-sos", json={
        "client_event_id": "evt-conflict-sos-1",
        "trip_id": phase1b_app["trip_a_id"],
        "latitude": 19.01,
        "longitude": 72.91,
        "description": "Original SOS",
    })
    assert res_ok.status_code == 200
    assert res_ok.get_json()["result"] == "accepted"

    res_conflict_desc = client.post("/driver/api/trigger-sos", json={
        "client_event_id": "evt-conflict-sos-1",
        "trip_id": phase1b_app["trip_a_id"],
        "latitude": 19.01,
        "longitude": 72.91,
        "description": "Different SOS payload",
    })
    assert res_conflict_desc.status_code == 409
    assert res_conflict_desc.get_json()["result"] == "rejected"
    assert res_conflict_desc.get_json()["conflict"] is True

    res_conflict_no_trip = client.post("/driver/api/trigger-sos", json={
        "client_event_id": "evt-conflict-sos-1",
        "description": "Original SOS",
    })
    assert res_conflict_no_trip.status_code == 409
    assert res_conflict_no_trip.get_json()["conflict"] is True

    # 4. Offline SOS conflict on reused client_event_id with different description
    res_off_conflict = client.post("/driver/api/sync-offline-queue", json={
        "sos_queue": [{
            "client_event_id": "evt-conflict-sos-1",
            "trip_id": phase1b_app["trip_a_id"],
            "description": "Altered offline SOS",
        }]
    })
    assert res_off_conflict.status_code == 200
    assert res_off_conflict.get_json()["sos_results"][0]["status"] == "rejected"
    assert res_off_conflict.get_json()["sos_results"][0]["conflict"] is True

    # 5. Offline attendance conflict when same ID reused for different scan_action or student
    res_att_ok = client.post("/driver/api/sync-offline-queue", json={
        "queue": [{
            "id": "evt-att-conflict-1",
            "trip_id": phase1b_app["trip_a_id"],
            "image": phase1b_app["b64_a1"],
            "scan_action": "BOARD",
        }]
    })
    assert res_att_ok.get_json()["attendance_results"][0]["status"] == "accepted"

    res_att_conflict_action = client.post("/driver/api/sync-offline-queue", json={
        "queue": [{
            "id": "evt-att-conflict-1",
            "trip_id": phase1b_app["trip_a_id"],
            "image": phase1b_app["b64_a1"],
            "scan_action": "DROP",
        }]
    })
    assert res_att_conflict_action.get_json()["attendance_results"][0]["status"] == "rejected"
    assert res_att_conflict_action.get_json()["attendance_results"][0]["conflict"] is True

    # 6. Duplicate-key IntegrityError race resolves cleanly to duplicate on matching payload
    real_commit = db.session.commit
    with patch("app.driver.routes.db.session.commit", side_effect=[IntegrityError("stmt", "params", "orig"), real_commit]):
        res_race = client.post("/driver/api/trigger-sos", json={
            "client_event_id": "evt-conflict-sos-1",
            "trip_id": phase1b_app["trip_a_id"],
            "latitude": 19.01,
            "longitude": 72.91,
            "description": "Original SOS",
        })
    assert res_race.status_code == 200
    assert res_race.get_json()["result"] == "duplicate"


def test_sos_strict_institution_consistency_and_completed_offline_trip(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    # 1. Completed trip offline SOS is preserved and accepted
    with app.app_context():
        completed_trip = Trip(
            institution_id=phase1b_app["inst_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            driver_id=phase1b_app["driver_a_id"],
            route_id=phase1b_app["route_a_id"],
            trip_type="MORNING_PICKUP",
            status="COMPLETED",
            start_time=datetime.utcnow() - timedelta(hours=1),
            end_time=datetime.utcnow(),
        )
        db.session.add(completed_trip)
        db.session.commit()
        completed_trip_id = completed_trip.id

    res_completed = client.post("/driver/api/sync-offline-queue", json={
        "sos_queue": [{
            "client_event_id": "sos-completed-trip-1",
            "trip_id": completed_trip_id,
            "description": "Delayed sync after trip ended",
        }]
    })
    assert res_completed.status_code == 200
    assert res_completed.get_json()["sos_results"][0]["status"] == "accepted"

    # 2. Online SOS rejects COMPLETED trip
    res_online_completed = client.post("/driver/api/trigger-sos", json={
        "client_event_id": "sos-online-completed",
        "trip_id": completed_trip_id,
    })
    assert res_online_completed.status_code == 400
    assert res_online_completed.get_json()["result"] == "rejected"

    # 3. User vs driver institution mismatch fails closed
    with app.app_context():
        u = db.session.get(User, phase1b_app["driver_a_u_id"])
        u.institution_id = phase1b_app["inst_b_id"]
        db.session.commit()

    res_user_mismatch = client.post("/driver/api/trigger-sos", json={
        "client_event_id": "sos-user-mismatch",
        "trip_id": phase1b_app["trip_a_id"],
    })
    assert res_user_mismatch.status_code == 403
    assert res_user_mismatch.get_json()["result"] == "rejected"

    # Restore user institution, test assigned bus institution mismatch (when no trip supplied)
    with app.app_context():
        u = db.session.get(User, phase1b_app["driver_a_u_id"])
        u.institution_id = phase1b_app["inst_a_id"]
        bus_a1 = db.session.get(Bus, phase1b_app["bus_a1_id"])
        bus_a1.institution_id = phase1b_app["inst_b_id"]
        db.session.commit()

    res_bus_mismatch = client.post("/driver/api/trigger-sos", json={
        "client_event_id": "sos-bus-mismatch",
    })
    assert res_bus_mismatch.status_code == 403
    assert res_bus_mismatch.get_json()["result"] == "rejected"

    # Trip institution missing fails closed
    with app.app_context():
        bus_a1 = db.session.get(Bus, phase1b_app["bus_a1_id"])
        bus_a1.institution_id = phase1b_app["inst_a_id"]
        db.session.commit()

    bad_trip = SimpleNamespace(
        id=phase1b_app["trip_a_id"],
        driver_id=phase1b_app["driver_a_id"],
        bus_id=phase1b_app["bus_a1_id"],
        status="IN_PROGRESS",
        institution_id=None,
    )
    real_get = db.session.get

    def get_with_null_trip_inst(model, pk):
        if model is Trip:
            return bad_trip
        return real_get(model, pk)

    with patch("app.driver.routes.db.session.get", side_effect=get_with_null_trip_inst):
        res_trip_no_inst = client.post("/driver/api/trigger-sos", json={
            "client_event_id": "sos-trip-no-inst",
            "trip_id": phase1b_app["trip_a_id"],
        })
    assert res_trip_no_inst.status_code == 400
    assert res_trip_no_inst.get_json()["result"] == "rejected"


def test_manual_verify_hardening(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]

    # 1. Wrong driver cannot manually verify on Driver A's trip
    _login_as(client, phase1b_app["driver_b_u_id"])
    res_wrong_drv = client.post("/driver/manual-verify", data={
        "student_id": phase1b_app["student_a1_id"],
        "trip_id": phase1b_app["trip_a_id"],
        "reason": "Wrong driver test",
    }, follow_redirects=True)
    assert b"Unauthorized access" in res_wrong_drv.data

    # 2. Switch to Driver A: cross-institution student rejected
    _login_as(client, phase1b_app["driver_a_u_id"])
    res_cross = client.post("/driver/manual-verify", data={
        "student_id": phase1b_app["student_b1_id"],
        "trip_id": phase1b_app["trip_a_id"],
        "reason": "Cross inst test",
    }, follow_redirects=True)
    assert b"Institution context missing or mismatched" in res_cross.data

    # 3. Student assigned to another bus (Bus A2) rejected without reassignment
    res_wrong_bus = client.post("/driver/manual-verify", data={
        "student_id": phase1b_app["student_a2_id"],
        "trip_id": phase1b_app["trip_a_id"],
        "reason": "Other bus student",
    }, follow_redirects=True)
    assert b"not assigned to this bus" in res_wrong_bus.data

    # 4. Active TemporaryBusReassignment authorizes Student A2 on Bus A1
    with app.app_context():
        today_str = date.today().isoformat()
        reassignment = TemporaryBusReassignment(
            student_id=phase1b_app["student_a2_id"],
            original_bus_id=phase1b_app["bus_a2_id"],
            temporary_bus_id=phase1b_app["bus_a1_id"],
            start_date=today_str,
            end_date=today_str,
            approved_by_user_id=phase1b_app["admin_a_id"],
            reason="Approved afternoon swap",
            is_active=True,
        )
        db.session.add(reassignment)
        db.session.commit()

    res_reassigned = client.post("/driver/manual-verify", data={
        "student_id": phase1b_app["student_a2_id"],
        "trip_id": phase1b_app["trip_a_id"],
        "reason": "Reassigned bus override",
    }, follow_redirects=True)
    assert b"Manual verification recorded for Student A2 OtherBus" in res_reassigned.data

    # 5. Database commit failure rolls back and does NOT report success
    with patch("app.driver.routes.db.session.commit", side_effect=RuntimeError("Commit failed")):
        res_db_err = client.post("/driver/manual-verify", data={
            "student_id": phase1b_app["student_a1_id"],
            "trip_id": phase1b_app["trip_a_id"],
            "reason": "DB fail test",
        }, follow_redirects=True)
    assert b"Database error recording manual verification" in res_db_err.data
    assert b"Manual verification recorded for Student A1" not in res_db_err.data

    with app.app_context():
        assert Attendance.query.filter_by(student_id=phase1b_app["student_a1_id"], trip_id=phase1b_app["trip_a_id"]).count() == 0

    # 6. Missing institution fails closed
    with app.app_context():
        u = db.session.get(User, phase1b_app["driver_a_u_id"])
        u.institution_id = None
        db.session.commit()
    res_no_inst = client.post("/driver/manual-verify", data={
        "student_id": phase1b_app["student_a1_id"],
        "trip_id": phase1b_app["trip_a_id"],
        "reason": "No inst test",
    }, follow_redirects=True)
    assert b"Institution context missing or mismatched" in res_no_inst.data

    # 7. Completed trip rejected; valid IN_PROGRESS trip succeeds and creates MANUAL_OVERRIDE + AuditLog
    with app.app_context():
        u = db.session.get(User, phase1b_app["driver_a_u_id"])
        u.institution_id = phase1b_app["inst_a_id"]
        completed_trip = Trip(
            institution_id=phase1b_app["inst_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            driver_id=phase1b_app["driver_a_id"],
            route_id=phase1b_app["route_a_id"],
            trip_type="MORNING_PICKUP",
            status="COMPLETED",
        )
        db.session.add(completed_trip)
        db.session.commit()
        completed_id = completed_trip.id

    res_completed = client.post("/driver/manual-verify", data={
        "student_id": phase1b_app["student_a1_id"],
        "trip_id": completed_id,
        "reason": "Completed trip test",
    }, follow_redirects=True)
    assert b"Trip is not active" in res_completed.data

    res_valid = client.post("/driver/manual-verify", data={
        "student_id": phase1b_app["student_a1_id"],
        "trip_id": phase1b_app["trip_a_id"],
        "reason": "Cap worn",
    }, follow_redirects=True)
    assert b"Manual verification recorded for Student A1" in res_valid.data

    with app.app_context():
        att = Attendance.query.filter_by(student_id=phase1b_app["student_a1_id"], trip_id=phase1b_app["trip_a_id"]).first()
        assert att is not None
        assert att.verification_status == "MANUAL_OVERRIDE"
        assert AuditLog.query.filter_by(action="MANUAL_VERIFY_OVERRIDE").count() >= 2


def test_sos_batch_limits_partial_sync_and_dashboard_controls(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    # 1. Server rejects SOS batch exceeding MAX_OFFLINE_QUEUE_ITEMS (5 in fixture)
    too_many = [
        {"client_event_id": f"sos-over-{i}", "trip_id": phase1b_app["trip_a_id"], "description": f"SOS {i}"}
        for i in range(6)
    ]
    res_over = client.post("/driver/api/sync-offline-queue", json={"sos_queue": too_many})
    assert res_over.status_code == 400

    # 2. Partial batch: valid, rejected (bad coords), and retryable DB failure -> exact outcomes
    real_commit = db.session.commit
    call_count = {"n": 0}

    def fail_third_commit():
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("Transient DB error")
        return real_commit()

    with patch("app.driver.routes.db.session.commit", side_effect=fail_third_commit):
        res_partial = client.post("/driver/api/sync-offline-queue", json={
            "sos_queue": [
                {"client_event_id": "sos-batch-1", "trip_id": phase1b_app["trip_a_id"], "description": "Valid SOS 1"},
                {"client_event_id": "sos-batch-bad", "trip_id": phase1b_app["trip_a_id"], "latitude": 120.0, "longitude": 10.0},
                {"client_event_id": "sos-batch-retry", "trip_id": phase1b_app["trip_a_id"], "description": "Retry SOS 3"},
            ]
        })

    assert res_partial.status_code == 200
    dp = res_partial.get_json()
    assert dp["status"] == "PARTIAL"
    by_id = {r["id"]: r["status"] for r in dp["sos_results"]}
    assert by_id == {
        "sos-batch-1": "accepted",
        "sos-batch-bad": "rejected",
        "sos-batch-retry": "retryable_failure",
    }

    # 3. Retrying batch containing accepted + retryable creates no duplicate for sos-batch-1 and accepts sos-batch-retry
    res_retry = client.post("/driver/api/sync-offline-queue", json={
        "sos_queue": [
            {"client_event_id": "sos-batch-1", "trip_id": phase1b_app["trip_a_id"], "description": "Valid SOS 1"},
            {"client_event_id": "sos-batch-retry", "trip_id": phase1b_app["trip_a_id"], "description": "Retry SOS 3"},
        ]
    })
    assert res_retry.status_code == 200
    dr = res_retry.get_json()
    by_id_retry = {r["id"]: r["status"] for r in dr["sos_results"]}
    assert by_id_retry == {
        "sos-batch-1": "already_processed",
        "sos-batch-retry": "accepted",
    }

    with app.app_context():
        assert EmergencyEvent.query.filter_by(client_event_id="sos-batch-1").count() == 1
        assert EmergencyEvent.query.filter_by(client_event_id="sos-batch-retry").count() == 1

    # 4. Dashboard template includes bounded batch config, concurrency guard, and rejected SOS review UI
    dash = client.get("/driver/dashboard")
    assert dash.status_code == 200
    html = dash.get_data(as_text=True)
    assert 'id="sos-sync-panel"' in html
    assert 'data-max-batch-size="5"' in html
    assert 'isSosSyncInProgress' in html
    assert 'MAX_LOCAL_SOS_QUEUE_SIZE' in html
    assert 'dismissRejectedSos' in html


def test_drop_attendance_integrity_error_race_handling(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    with app.app_context():
        trip_a2 = Trip(
            institution_id=phase1b_app["inst_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            route_id=phase1b_app["route_a_id"],
            driver_id=phase1b_app["driver_a_id"],
            trip_type="AFTERNOON",
            status="COMPLETED",
        )
        student_a3 = Student(
            institution_id=phase1b_app["inst_a_id"],
            full_name="Student A3 SameBus",
            roll_number="P1B-A3",
            parent_id=phase1b_app["parent_a_id"],
            assigned_bus_id=phase1b_app["bus_a1_id"],
            assigned_route_id=phase1b_app["route_a_id"],
        )
        db.session.add_all([trip_a2, student_a3])
        db.session.commit()
        trip_a2_id = trip_a2.id
        student_a3_id = student_a3.id

    def _run_raced_drop(event_id, racing_factory):
        real_commit = db.session.commit
        commit_attempts = {"count": 0}

        def raced_commit():
            commit_attempts["count"] += 1
            if commit_attempts["count"] == 1:
                # Discard uncommitted pending unit of work, persist real racing DB state (if any),
                # then attempt the duplicate/conflicting INSERT so SQLite raises IntegrityError.
                db.session.rollback()
                if racing_factory is not None:
                    competing = racing_factory()
                    if competing is not None:
                        db.session.add(competing)
                        real_commit()
                        db.session.add(Attendance(
                            student_id=phase1b_app["student_a1_id"],
                            trip_id=phase1b_app["trip_a_id"],
                            bus_id=phase1b_app["bus_a1_id"],
                            client_event_id=event_id,
                            verification_status="VERIFIED",
                            scan_type="DROP",
                            is_dropped_off=True,
                            verified_by_driver_id=phase1b_app["driver_a_id"],
                        ))
                        return real_commit()
                raise IntegrityError("INSERT attendances", {}, Exception("Simulated integrity failure without matching row"))
            return real_commit()

        with patch("app.driver.routes.db.session.commit", side_effect=raced_commit):
            res = client.post("/driver/api/sync-offline-queue", json={
                "queue": [{
                    "client_event_id": event_id,
                    "trip_id": phase1b_app["trip_a_id"],
                    "image": phase1b_app["b64_a1"],
                    "scan_action": "DROP",
                }]
            })
        assert commit_attempts["count"] == 1
        assert res.status_code == 200
        return res.get_json()

    # 1. Matching student, trip, and DROP scan action -> already_processed
    d_match = _run_raced_drop(
        "evt-drop-race-match",
        lambda: Attendance(
            student_id=phase1b_app["student_a1_id"],
            trip_id=phase1b_app["trip_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-drop-race-match",
            verification_status="VERIFIED",
            scan_type="DROP",
            is_dropped_off=True,
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert d_match["already_processed_count"] == 1
    assert d_match["attendance_results"][0]["status"] == "already_processed"

    with app.app_context():
        Attendance.query.delete()
        db.session.commit()

    # 2. Same event ID belongs to a different student -> conflict (rejected)
    d_diff_student = _run_raced_drop(
        "evt-drop-race-diff-student",
        lambda: Attendance(
            student_id=student_a3_id,
            trip_id=phase1b_app["trip_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-drop-race-diff-student",
            verification_status="VERIFIED",
            scan_type="DROP",
            is_dropped_off=True,
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert d_diff_student["rejected_count"] == 1
    assert d_diff_student["already_processed_count"] == 0
    assert d_diff_student["attendance_results"][0]["status"] == "rejected"
    assert d_diff_student["attendance_results"][0]["conflict"] is True

    with app.app_context():
        Attendance.query.delete()
        db.session.commit()

    # 3a. Same event ID belongs to a different trip -> conflict (rejected)
    d_diff_trip = _run_raced_drop(
        "evt-drop-race-diff-trip",
        lambda: Attendance(
            student_id=phase1b_app["student_a1_id"],
            trip_id=trip_a2_id,
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-drop-race-diff-trip",
            verification_status="VERIFIED",
            scan_type="DROP",
            is_dropped_off=True,
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert d_diff_trip["rejected_count"] == 1
    assert d_diff_trip["attendance_results"][0]["status"] == "rejected"
    assert d_diff_trip["attendance_results"][0]["conflict"] is True

    with app.app_context():
        Attendance.query.delete()
        db.session.commit()

    # 3b. Same student and trip, but event ID belongs to a different scan_type (BOARD) -> conflict (rejected)
    d_diff_action = _run_raced_drop(
        "evt-drop-race-diff-action",
        lambda: Attendance(
            student_id=phase1b_app["student_a1_id"],
            trip_id=phase1b_app["trip_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-drop-race-diff-action",
            verification_status="VERIFIED",
            scan_type="BOARD",
            is_dropped_off=False,
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert d_diff_action["rejected_count"] == 1
    assert d_diff_action["attendance_results"][0]["status"] == "rejected"
    assert d_diff_action["attendance_results"][0]["conflict"] is True

    with app.app_context():
        Attendance.query.delete()
        db.session.commit()

    # 4a. Student/trip constraint race where competing row has a DIFFERENT client_event_id -> retryable_failure
    d_no_match_student_trip = _run_raced_drop(
        "evt-drop-race-no-match",
        lambda: Attendance(
            student_id=phase1b_app["student_a1_id"],
            trip_id=phase1b_app["trip_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-other-board-row",
            verification_status="VERIFIED",
            scan_type="BOARD",
            is_dropped_off=False,
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert d_no_match_student_trip["retryable_failure_count"] == 1
    assert d_no_match_student_trip["already_processed_count"] == 0
    assert d_no_match_student_trip["attendance_results"][0]["status"] == "retryable_failure"

    with app.app_context():
        row = Attendance.query.filter_by(student_id=phase1b_app["student_a1_id"], trip_id=phase1b_app["trip_a_id"]).first()
        assert row is not None and row.is_dropped_off is False
        Attendance.query.delete()
        db.session.commit()

    # 4b. IntegrityError with zero matching records in DB -> retryable_failure
    d_empty = _run_raced_drop("evt-drop-race-empty", None)
    assert d_empty["retryable_failure_count"] == 1
    assert d_empty["already_processed_count"] == 0
    assert d_empty["attendance_results"][0]["status"] == "retryable_failure"
    with app.app_context():
        assert Attendance.query.filter_by(client_event_id="evt-drop-race-empty").count() == 0


def test_board_attendance_integrity_error_race_handling(phase1b_app):
    client = phase1b_app["client"]
    app = phase1b_app["app"]
    _login_as(client, phase1b_app["driver_a_u_id"])

    with app.app_context():
        trip_a2 = Trip(
            institution_id=phase1b_app["inst_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            route_id=phase1b_app["route_a_id"],
            driver_id=phase1b_app["driver_a_id"],
            trip_type="AFTERNOON",
            status="COMPLETED",
        )
        student_a3 = Student(
            institution_id=phase1b_app["inst_a_id"],
            full_name="Student A3 BoardRace",
            roll_number="P1B-A3-B",
            parent_id=phase1b_app["parent_a_id"],
            assigned_bus_id=phase1b_app["bus_a1_id"],
            assigned_route_id=phase1b_app["route_a_id"],
        )
        db.session.add_all([trip_a2, student_a3])
        db.session.commit()
        trip_a2_id = trip_a2.id
        student_a3_id = student_a3.id

    def _run_raced_board(event_id, racing_factory):
        real_commit = db.session.commit
        commit_attempts = {"count": 0}

        def raced_commit():
            commit_attempts["count"] += 1
            if commit_attempts["count"] == 1:
                db.session.rollback()
                if racing_factory is not None:
                    competing = racing_factory()
                    if competing is not None:
                        db.session.add(competing)
                        real_commit()
                        db.session.add(Attendance(
                            student_id=phase1b_app["student_a1_id"],
                            trip_id=phase1b_app["trip_a_id"],
                            bus_id=phase1b_app["bus_a1_id"],
                            client_event_id=event_id,
                            verification_status="VERIFIED",
                            scan_type="BOARD",
                            verified_by_driver_id=phase1b_app["driver_a_id"],
                        ))
                        return real_commit()
                raise IntegrityError("INSERT attendances", {}, Exception("Unrelated integrity failure"))
            return real_commit()

        with patch("app.driver.routes.db.session.commit", side_effect=raced_commit):
            res = client.post("/driver/api/sync-offline-queue", json={
                "queue": [{
                    "client_event_id": event_id,
                    "trip_id": phase1b_app["trip_a_id"],
                    "image": phase1b_app["b64_a1"],
                    "scan_action": "BOARD",
                }]
            })
        assert commit_attempts["count"] == 1
        assert res.status_code == 200
        return res.get_json()

    # 1. Matching event ID and matching BOARD event -> already_processed
    b_match = _run_raced_board(
        "evt-board-race-match",
        lambda: Attendance(
            student_id=phase1b_app["student_a1_id"],
            trip_id=phase1b_app["trip_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-board-race-match",
            verification_status="VERIFIED",
            scan_type="BOARD",
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert b_match["already_processed_count"] == 1
    assert b_match["attendance_results"][0]["status"] == "already_processed"

    with app.app_context():
        Attendance.query.delete()
        db.session.commit()

    # 2a. Same event ID reused for a different student -> conflict (rejected)
    b_diff_student = _run_raced_board(
        "evt-board-race-diff-student",
        lambda: Attendance(
            student_id=student_a3_id,
            trip_id=phase1b_app["trip_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-board-race-diff-student",
            verification_status="VERIFIED",
            scan_type="BOARD",
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert b_diff_student["rejected_count"] == 1
    assert b_diff_student["attendance_results"][0]["status"] == "rejected"
    assert b_diff_student["attendance_results"][0]["conflict"] is True

    with app.app_context():
        Attendance.query.delete()
        db.session.commit()

    # 2b. Same event ID reused for a different trip -> conflict (rejected)
    b_diff_trip = _run_raced_board(
        "evt-board-race-diff-trip",
        lambda: Attendance(
            student_id=phase1b_app["student_a1_id"],
            trip_id=trip_a2_id,
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-board-race-diff-trip",
            verification_status="VERIFIED",
            scan_type="BOARD",
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert b_diff_trip["rejected_count"] == 1
    assert b_diff_trip["attendance_results"][0]["status"] == "rejected"
    assert b_diff_trip["attendance_results"][0]["conflict"] is True

    with app.app_context():
        Attendance.query.delete()
        db.session.commit()

    # 2c. Same event ID reused for a different scan_type (DROP) -> conflict (rejected)
    b_diff_action = _run_raced_board(
        "evt-board-race-diff-action",
        lambda: Attendance(
            student_id=phase1b_app["student_a1_id"],
            trip_id=phase1b_app["trip_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-board-race-diff-action",
            verification_status="VERIFIED",
            scan_type="DROP",
            is_dropped_off=True,
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert b_diff_action["rejected_count"] == 1
    assert b_diff_action["attendance_results"][0]["status"] == "rejected"
    assert b_diff_action["attendance_results"][0]["conflict"] is True

    with app.app_context():
        Attendance.query.delete()
        db.session.commit()

    # 3. IntegrityError with no matching event ID and no matching student/trip attendance -> retryable_failure
    b_empty = _run_raced_board("evt-board-race-empty", None)
    assert b_empty["retryable_failure_count"] == 1
    assert b_empty["already_processed_count"] == 0
    assert b_empty["accepted_count"] == 0
    assert b_empty["attendance_results"][0]["status"] == "retryable_failure"
    with app.app_context():
        assert Attendance.query.filter_by(student_id=phase1b_app["student_a1_id"], trip_id=phase1b_app["trip_a_id"]).count() == 0

    # 4a. Concurrent BOARD attendance record for same student and trip (different event ID) -> already_processed
    b_concurrent_board = _run_raced_board(
        "evt-board-race-concurrent",
        lambda: Attendance(
            student_id=phase1b_app["student_a1_id"],
            trip_id=phase1b_app["trip_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-other-concurrent-board",
            verification_status="VERIFIED",
            scan_type="BOARD",
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert b_concurrent_board["already_processed_count"] == 1
    assert b_concurrent_board["attendance_results"][0]["status"] == "already_processed"

    with app.app_context():
        Attendance.query.delete()
        db.session.commit()

    # 4b. Existing (student, trip) row that is NOT a confirmed BOARD (ABSENT status) -> retryable_failure
    b_absent_row = _run_raced_board(
        "evt-board-race-absent",
        lambda: Attendance(
            student_id=phase1b_app["student_a1_id"],
            trip_id=phase1b_app["trip_a_id"],
            bus_id=phase1b_app["bus_a1_id"],
            client_event_id="evt-other-absent-row",
            verification_status="ABSENT",
            scan_type="BOARD",
            verified_by_driver_id=phase1b_app["driver_a_id"],
        ),
    )
    assert b_absent_row["retryable_failure_count"] == 1
    assert b_absent_row["already_processed_count"] == 0
    assert b_absent_row["attendance_results"][0]["status"] == "retryable_failure"

    with app.app_context():
        Attendance.query.delete()
        db.session.commit()

    # 5. Unrelated IntegrityError on online /driver/api/verify-face BOARD must fail with 500 retryable_failure, not success
    commit_calls = {"count": 0}

    def unrelated_integrity_fail():
        commit_calls["count"] += 1
        raise IntegrityError("INSERT notifications", {}, Exception("FK constraint failed on unrelated table"))

    with patch("app.driver.routes.db.session.commit", side_effect=unrelated_integrity_fail):
        res_online = client.post("/driver/api/verify-face", json={
            "trip_id": phase1b_app["trip_a_id"],
            "image": phase1b_app["b64_a1"],
            "scan_action": "BOARD",
            "client_event_id": "evt-board-online-unrelated-err",
        })
    assert commit_calls["count"] == 1
    assert res_online.status_code == 500
    assert res_online.get_json()["result"] == "retryable_failure"
    with app.app_context():
        assert Attendance.query.filter_by(student_id=phase1b_app["student_a1_id"], trip_id=phase1b_app["trip_a_id"]).count() == 0
