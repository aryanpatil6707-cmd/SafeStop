import base64
import json
import logging
import re
from datetime import datetime, timedelta
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from app import create_app
from app.ai.engine import extract_face_features, identify_student_from_face
from app.models import (
    Alert,
    Attendance,
    Bus,
    Driver,
    EmergencyEvent,
    FaceProfile,
    Institution,
    Notification,
    Parent,
    Route,
    Student,
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
    from types import SimpleNamespace
    orphan_driver = SimpleNamespace(
        id=phase1b_app["driver_a_id"],
        user_id=phase1b_app["driver_a_u_id"],
        institution_id=None,
        assigned_bus_id=1,
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
        {"trip_id": phase1b_app["trip_a_id"], "latitude": 95.0, "longitude": 72.0},
        {"trip_id": phase1b_app["trip_a_id"], "latitude": 19.0, "longitude": -181.0},
        {"trip_id": phase1b_app["trip_a_id"], "latitude": 19.0},
        {"trip_id": phase1b_app["trip_a_id"], "latitude": True, "longitude": 72.0},
        {"trip_id": phase1b_app["trip_a_id"], "latitude": "not-a-num", "longitude": 72.0},
        {"trip_id": phase1b_app["trip_a_id"], "triggered_at": "invalid-date-string"},
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
