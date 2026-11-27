from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app import create_app
from app.models import (
    Bus,
    Driver,
    GpsLocation,
    Institution,
    Route,
    Trip,
    User,
    db,
)


@pytest.fixture
def phase2a_app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    with app.test_client() as client:
        with app.app_context():
            db.drop_all()
            db.create_all()

            inst_a = Institution(name="Phase2A School A", slug="p2a-school-a", plan_tier="ENTERPRISE")
            inst_b = Institution(name="Phase2A School B", slug="p2a-school-b", plan_tier="ENTERPRISE")
            db.session.add_all([inst_a, inst_b])
            db.session.flush()

            driver_a_u = User(institution_id=inst_a.id, email="p2a_driver_a@example.com", role="DRIVER", full_name="P2A Driver A")
            driver_a_u.set_password("StrongPass!1")
            driver_a2_u = User(institution_id=inst_a.id, email="p2a_driver_a2@example.com", role="DRIVER", full_name="P2A Driver A2")
            driver_a2_u.set_password("StrongPass!2")
            driver_b_u = User(institution_id=inst_b.id, email="p2a_driver_b@example.com", role="DRIVER", full_name="P2A Driver B")
            driver_b_u.set_password("StrongPass!3")
            parent_a_u = User(institution_id=inst_a.id, email="p2a_parent_a@example.com", role="PARENT", full_name="P2A Parent A")
            parent_a_u.set_password("StrongPass!4")
            db.session.add_all([driver_a_u, driver_a2_u, driver_b_u, parent_a_u])
            db.session.flush()

            route_a = Route(institution_id=inst_a.id, name="P2A Route A")
            route_b = Route(institution_id=inst_b.id, name="P2A Route B")
            db.session.add_all([route_a, route_b])
            db.session.flush()

            driver_a = Driver(institution_id=inst_a.id, user_id=driver_a_u.id, full_name="P2A Driver A", phone="+1-555-2001", license_number="LIC-2A-1", is_online=False)
            driver_a2 = Driver(institution_id=inst_a.id, user_id=driver_a2_u.id, full_name="P2A Driver A2", phone="+1-555-2002", license_number="LIC-2A-2", is_online=False)
            driver_b = Driver(institution_id=inst_b.id, user_id=driver_b_u.id, full_name="P2A Driver B", phone="+1-555-2003", license_number="LIC-2A-3", is_online=False)
            db.session.add_all([driver_a, driver_a2, driver_b])
            db.session.flush()

            bus_a1 = Bus(institution_id=inst_a.id, registration_number="REG-2A-A1", bus_code="BUS-2A-A1", driver_id=driver_a.id, route_id=route_a.id, status="INACTIVE")
            bus_a2 = Bus(institution_id=inst_a.id, registration_number="REG-2A-A2", bus_code="BUS-2A-A2", driver_id=driver_a2.id, route_id=route_a.id, status="INACTIVE")
            bus_b1 = Bus(institution_id=inst_b.id, registration_number="REG-2A-B1", bus_code="BUS-2A-B1", driver_id=driver_b.id, route_id=route_b.id, status="INACTIVE")
            db.session.add_all([bus_a1, bus_a2, bus_b1])
            db.session.flush()

            driver_a.assigned_bus_id = bus_a1.id
            driver_a.assigned_route_id = route_a.id
            driver_a2.assigned_bus_id = bus_a2.id
            driver_a2.assigned_route_id = route_a.id
            driver_b.assigned_bus_id = bus_b1.id
            driver_b.assigned_route_id = route_b.id

            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            trip_a_active = Trip(
                institution_id=inst_a.id,
                bus_id=bus_a1.id,
                driver_id=driver_a.id,
                route_id=route_a.id,
                trip_type="MORNING_PICKUP",
                status="IN_PROGRESS",
                start_time=now_utc,
            )
            trip_a_completed = Trip(
                institution_id=inst_a.id,
                bus_id=bus_a1.id,
                driver_id=driver_a.id,
                route_id=route_a.id,
                trip_type="EVENING_DROP",
                status="COMPLETED",
                start_time=now_utc,
            )
            trip_a2_active = Trip(
                institution_id=inst_a.id,
                bus_id=bus_a2.id,
                driver_id=driver_a2.id,
                route_id=route_a.id,
                trip_type="MORNING_PICKUP",
                status="IN_PROGRESS",
                start_time=now_utc,
            )
            trip_b_active = Trip(
                institution_id=inst_b.id,
                bus_id=bus_b1.id,
                driver_id=driver_b.id,
                route_id=route_b.id,
                trip_type="MORNING_PICKUP",
                status="IN_PROGRESS",
                start_time=now_utc,
            )
            db.session.add_all([trip_a_active, trip_a_completed, trip_a2_active, trip_b_active])
            db.session.commit()

            ctx = {
                "app": app,
                "client": client,
                "inst_a_id": inst_a.id,
                "inst_b_id": inst_b.id,
                "driver_a_u_id": driver_a_u.id,
                "driver_a2_u_id": driver_a2_u.id,
                "driver_b_u_id": driver_b_u.id,
                "parent_a_u_id": parent_a_u.id,
                "driver_a_id": driver_a.id,
                "driver_a2_id": driver_a2.id,
                "driver_b_id": driver_b.id,
                "bus_a1_id": bus_a1.id,
                "bus_a2_id": bus_a2.id,
                "bus_b1_id": bus_b1.id,
                "route_a_id": route_a.id,
                "trip_a_active_id": trip_a_active.id,
                "trip_a_completed_id": trip_a_completed.id,
                "trip_a2_active_id": trip_a2_active.id,
                "trip_b_active_id": trip_b_active.id,
            }
        yield ctx


def _login_as(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def test_gps_update_valid_active_trip_and_idle_tracking(phase2a_app):
    client = phase2a_app["client"]
    app = phase2a_app["app"]
    _login_as(client, phase2a_app["driver_a_u_id"])

    # 1. Idle online tracking without a trip
    res_idle = client.post("/driver/api/gps-update", json={
        "latitude": 19.0760,
        "longitude": 72.8777,
    })
    assert res_idle.status_code == 200
    d_idle = res_idle.get_json()
    assert d_idle["status"] == "SUCCESS"
    assert d_idle["driver_is_online"] is True
    assert d_idle["bus_status"] == "ACTIVE"

    with app.app_context():
        driver = db.session.get(Driver, phase2a_app["driver_a_id"])
        bus = db.session.get(Bus, phase2a_app["bus_a1_id"])
        assert driver.current_lat == pytest.approx(19.0760)
        assert bus.current_lng == pytest.approx(72.8777)
        assert GpsLocation.query.count() == 0

    # 2. Valid active trip GPS update
    res_trip = client.post("/driver/api/gps-update", json={
        "latitude": -90.0,
        "longitude": 180.0,
        "trip_id": phase2a_app["trip_a_active_id"],
    })
    assert res_trip.status_code == 200
    d_trip = res_trip.get_json()
    assert d_trip["status"] == "SUCCESS"
    assert d_trip["trip_id"] == phase2a_app["trip_a_active_id"]

    with app.app_context():
        trip = db.session.get(Trip, phase2a_app["trip_a_active_id"])
        assert trip.current_lat == pytest.approx(-90.0)
        assert trip.current_lng == pytest.approx(180.0)
        assert GpsLocation.query.filter_by(trip_id=trip.id).count() == 1


@pytest.mark.parametrize("payload", [
    {"latitude": -90.01, "longitude": 72.8},
    {"latitude": 90.01, "longitude": 72.8},
    {"latitude": 19.0, "longitude": -180.01},
    {"latitude": 19.0, "longitude": 180.01},
    {"latitude": "NaN", "longitude": 72.8},
    {"latitude": 19.0, "longitude": "inf"},
    {"latitude": "-Infinity", "longitude": 72.8},
    {"latitude": True, "longitude": 72.8},
    {"latitude": 19.0, "longitude": False},
    {"latitude": 19.0},
    {"longitude": 72.8},
    {"latitude": "", "longitude": 72.8},
    {"latitude": "not-a-number", "longitude": 72.8},
])
def test_gps_update_rejects_invalid_coordinates_without_mutating_db(phase2a_app, payload):
    client = phase2a_app["client"]
    app = phase2a_app["app"]
    _login_as(client, phase2a_app["driver_a_u_id"])

    payload_with_trip = {**payload, "trip_id": phase2a_app["trip_a_active_id"]}
    res = client.post("/driver/api/gps-update", json=payload_with_trip)
    assert res.status_code == 400
    assert res.get_json()["status"] == "ERROR"

    with app.app_context():
        driver = db.session.get(Driver, phase2a_app["driver_a_id"])
        bus = db.session.get(Bus, phase2a_app["bus_a1_id"])
        trip = db.session.get(Trip, phase2a_app["trip_a_active_id"])
        assert driver.current_lat is None
        assert driver.is_online is False
        assert bus.current_lat is None
        assert bus.status == "INACTIVE"
        assert trip.current_lat is None
        assert GpsLocation.query.count() == 0


def test_gps_update_rejects_invalid_or_unauthorized_trips(phase2a_app):
    client = phase2a_app["client"]
    app = phase2a_app["app"]
    _login_as(client, phase2a_app["driver_a_u_id"])

    # Malformed trip IDs
    for bad_trip_id in ("abc", True, 1.5, -3):
        res = client.post("/driver/api/gps-update", json={
            "latitude": 19.1,
            "longitude": 72.9,
            "trip_id": bad_trip_id,
        })
        assert res.status_code == 400
        assert res.get_json()["status"] == "ERROR"

    # Nonexistent trip
    res_missing = client.post("/driver/api/gps-update", json={
        "latitude": 19.1,
        "longitude": 72.9,
        "trip_id": 999999,
    })
    assert res_missing.status_code == 404
    assert res_missing.get_json()["status"] == "ERROR"

    # Trip belonging to another driver (same institution)
    res_other_driver = client.post("/driver/api/gps-update", json={
        "latitude": 19.1,
        "longitude": 72.9,
        "trip_id": phase2a_app["trip_a2_active_id"],
    })
    assert res_other_driver.status_code == 403
    assert res_other_driver.get_json()["status"] == "ERROR"

    # Trip belonging to another institution
    res_other_inst = client.post("/driver/api/gps-update", json={
        "latitude": 19.1,
        "longitude": 72.9,
        "trip_id": phase2a_app["trip_b_active_id"],
    })
    assert res_other_inst.status_code == 403

    # Completed trip
    res_completed = client.post("/driver/api/gps-update", json={
        "latitude": 19.1,
        "longitude": 72.9,
        "trip_id": phase2a_app["trip_a_completed_id"],
    })
    assert res_completed.status_code == 400

    # Ensure NONE of those rejected calls silently updated driver_a or bus_a1
    with app.app_context():
        driver = db.session.get(Driver, phase2a_app["driver_a_id"])
        bus = db.session.get(Bus, phase2a_app["bus_a1_id"])
        assert driver.current_lat is None
        assert driver.is_online is False
        assert bus.current_lat is None
        assert bus.status == "INACTIVE"
        assert GpsLocation.query.count() == 0


def test_gps_update_institution_mismatch_and_unauthorized_bus_assignment(phase2a_app):
    client = phase2a_app["client"]
    app = phase2a_app["app"]
    _login_as(client, phase2a_app["driver_a_u_id"])

    # 1. Trip bus doesn't match driver's assigned bus
    with app.app_context():
        mismatched_trip = Trip(
            institution_id=phase2a_app["inst_a_id"],
            bus_id=phase2a_app["bus_a2_id"],
            driver_id=phase2a_app["driver_a_id"],
            route_id=phase2a_app["route_a_id"],
            trip_type="MORNING_PICKUP",
            status="IN_PROGRESS",
        )
        db.session.add(mismatched_trip)
        db.session.commit()
        mismatched_trip_id = mismatched_trip.id

    res_bus_mismatch = client.post("/driver/api/gps-update", json={
        "latitude": 19.2,
        "longitude": 72.9,
        "trip_id": mismatched_trip_id,
    })
    assert res_bus_mismatch.status_code == 403

    # 2. Trip has mismatched institution ID
    with app.app_context():
        trip = db.session.get(Trip, phase2a_app["trip_a_active_id"])
        trip.institution_id = phase2a_app["inst_b_id"]
        db.session.commit()

    res_inst_mismatch = client.post("/driver/api/gps-update", json={
        "latitude": 19.2,
        "longitude": 72.9,
        "trip_id": phase2a_app["trip_a_active_id"],
    })
    assert res_inst_mismatch.status_code == 403

    # Restore trip institution, test user vs driver institution mismatch on idle tracking
    with app.app_context():
        trip = db.session.get(Trip, phase2a_app["trip_a_active_id"])
        trip.institution_id = phase2a_app["inst_a_id"]
        driver_a_u = db.session.get(User, phase2a_app["driver_a_u_id"])
        driver_a_u.institution_id = phase2a_app["inst_b_id"]
        db.session.commit()

    res_user_driver_mismatch = client.post("/driver/api/gps-update", json={
        "latitude": 19.2,
        "longitude": 72.9,
    })
    assert res_user_driver_mismatch.status_code == 403

    with app.app_context():
        driver_a_u = db.session.get(User, phase2a_app["driver_a_u_id"])
        driver_a_u.institution_id = phase2a_app["inst_a_id"]
        driver_a = db.session.get(Driver, phase2a_app["driver_a_id"])
        driver_a.assigned_bus_id = phase2a_app["bus_b1_id"]
        db.session.commit()

    res_idle_cross_tenant_bus = client.post("/driver/api/gps-update", json={
        "latitude": 19.2,
        "longitude": 72.9,
    })
    assert res_idle_cross_tenant_bus.status_code == 403

    # Idle tracking when assigned bus belongs to another driver in same institution
    with app.app_context():
        driver_a = db.session.get(Driver, phase2a_app["driver_a_id"])
        driver_a.assigned_bus_id = phase2a_app["bus_a2_id"]
        db.session.commit()

    res_idle_other_driver_bus = client.post("/driver/api/gps-update", json={
        "latitude": 19.2,
        "longitude": 72.9,
    })
    assert res_idle_other_driver_bus.status_code == 403

    with app.app_context():
        assert db.session.get(Bus, phase2a_app["bus_a2_id"]).current_lat is None
        assert db.session.get(Bus, phase2a_app["bus_b1_id"]).current_lat is None


def test_gps_update_db_failure_rolls_back_and_never_returns_success(phase2a_app):
    client = phase2a_app["client"]
    app = phase2a_app["app"]
    _login_as(client, phase2a_app["driver_a_u_id"])

    with patch("app.driver.routes.db.session.commit", side_effect=RuntimeError("Simulated DB commit failure")):
        res_trip_fail = client.post("/driver/api/gps-update", json={
            "latitude": 19.5,
            "longitude": 72.5,
            "trip_id": phase2a_app["trip_a_active_id"],
        })
    assert res_trip_fail.status_code == 500
    assert res_trip_fail.get_json()["status"] == "ERROR"

    with app.app_context():
        driver = db.session.get(Driver, phase2a_app["driver_a_id"])
        bus = db.session.get(Bus, phase2a_app["bus_a1_id"])
        trip = db.session.get(Trip, phase2a_app["trip_a_active_id"])
        assert driver.current_lat is None
        assert driver.is_online is False
        assert bus.current_lat is None
        assert bus.status == "INACTIVE"
        assert trip.current_lat is None
        assert GpsLocation.query.count() == 0

    with patch("app.driver.routes.db.session.commit", side_effect=RuntimeError("Simulated idle DB failure")):
        res_idle_fail = client.post("/driver/api/gps-update", json={
            "latitude": 19.5,
            "longitude": 72.5,
        })
    assert res_idle_fail.status_code == 500
    assert res_idle_fail.get_json()["status"] == "ERROR"


def test_gps_update_requires_driver_role_and_dashboard_reliability_ui(phase2a_app):
    client = phase2a_app["client"]

    _login_as(client, phase2a_app["parent_a_u_id"])
    res_parent = client.post("/driver/api/gps-update", json={
        "latitude": 19.1,
        "longitude": 72.9,
    })
    assert res_parent.status_code in (302, 401, 403)

    res_parent_follow = client.post("/driver/api/gps-update", json={
        "latitude": 19.1,
        "longitude": 72.9,
    }, follow_redirects=False)
    assert res_parent_follow.status_code in (302, 401, 403)

    _login_as(client, phase2a_app["driver_a_u_id"])
    dash = client.get("/driver/dashboard")
    assert dash.status_code == 200
    html = dash.get_data(as_text=True)

    assert "Last Synced:" in html
    assert "isGpsSyncInProgress" in html
    assert "pendingLatestGps" in html
    assert "parseJsonResponseSafely" in html
    assert "gpsLifecycleListenersRegistered" in html
    assert "temporarily_unavailable" in html
    assert "permission_denied" in html
    assert "server_connection_lost" in html
    assert "background tracking active" not in html.lower()


def test_phase2a_android_webview_security_configuration_invariants():
    root = Path(__file__).resolve().parent.parent
    manifest = (root / "android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    gradle = (root / "android/app/build.gradle").read_text(encoding="utf-8")
    activity = (root / "android/app/src/main/java/ai/safestop/app/MainActivity.kt").read_text(encoding="utf-8")

    # Release cleartext disabled, no premature background/foreground location service in Phase 2A
    assert 'android:usesCleartextTraffic="${usesCleartextTraffic}"' in manifest
    assert 'ACCESS_BACKGROUND_LOCATION' not in manifest
    assert 'FOREGROUND_SERVICE' not in manifest
    assert 'RECORD_AUDIO' not in manifest

    assert 'buildConfigField "String", "PRODUCTION_SERVER_URL"' in gradle
    assert 'buildConfigField "boolean", "ALLOW_DEV_SERVER_URL", "false"' in gradle
    assert 'manifestPlaceholders = [usesCleartextTraffic: "false"]' in gradle

    # WebView hardening & scoped permission invariants in MainActivity.kt
    assert 'settings.allowFileAccess = false' in activity
    assert 'settings.allowContentAccess = false' in activity
    assert 'settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW' in activity
    assert 'cookieManager.setAcceptThirdPartyCookies(webView, false)' in activity
    assert 'handler?.cancel()' in activity
    assert 'addJavascriptInterface' not in activity
    assert 'PermissionRequest.RESOURCE_VIDEO_CAPTURE' in activity
    assert 'request.grant(request.resources)' not in activity
    assert 'callback.invoke(origin, true, false)' in activity
