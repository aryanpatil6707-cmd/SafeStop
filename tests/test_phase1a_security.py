import importlib
import json
import logging
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import stripe

from app import create_app
from app.gps.tracker import check_and_escalate_pending_safe_drops
from app.models import (
    Bus,
    Driver,
    Institution,
    Notification,
    Parent,
    Route,
    SafeDropConfirmation,
    Stop,
    Student,
    Trip,
    User,
    db,
)
from app.notifications.service import (
    broadcast_admin_notification,
    broadcast_superadmin_notification,
)
from app.security import generate_password_reset_token, verify_password_reset_token


@pytest.fixture
def phase1a_app():
    app = create_app()
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['NOTIFICATION_PROVIDER'] = 'console'

    with app.test_client() as client:
        with app.app_context():
            db.drop_all()
            db.create_all()

            inst_a = Institution(name="Alpha Academy", slug="alpha-academy", plan_tier="ENTERPRISE")
            inst_b = Institution(name="Beta Academy", slug="beta-academy", plan_tier="ENTERPRISE")
            db.session.add_all([inst_a, inst_b])
            db.session.flush()

            super_u = User(institution_id=None, email="super@example.com", role="SUPER_ADMIN", full_name="Super Admin")
            super_u.set_password("StrongSuperPass!1")

            admin_a = User(institution_id=inst_a.id, email="admin_a@example.com", role="ADMIN", full_name="Admin A")
            admin_a.set_password("StrongPassA!1")
            driver_a_u = User(institution_id=inst_a.id, email="driver_a@example.com", role="DRIVER", full_name="Driver A")
            driver_a_u.set_password("StrongPassA!2")
            parent_a_u = User(institution_id=inst_a.id, email="parent_a@example.com", role="PARENT", full_name="Parent A")
            parent_a_u.set_password("StrongPassA!3")

            admin_b = User(institution_id=inst_b.id, email="admin_b@example.com", role="ADMIN", full_name="Admin B")
            admin_b.set_password("StrongPassB!1")
            driver_b_u = User(institution_id=inst_b.id, email="driver_b@example.com", role="DRIVER", full_name="Driver B")
            driver_b_u.set_password("StrongPassB!2")

            db.session.add_all([super_u, admin_a, driver_a_u, parent_a_u, admin_b, driver_b_u])
            db.session.flush()

            parent_a = Parent(institution_id=inst_a.id, user_id=parent_a_u.id)
            driver_a = Driver(institution_id=inst_a.id, user_id=driver_a_u.id, full_name="Driver A", phone="+1-555-0101", license_number="LIC-A1")
            driver_b = Driver(institution_id=inst_b.id, user_id=driver_b_u.id, full_name="Driver B", phone="+1-555-0201", license_number="LIC-B1")
            db.session.add_all([parent_a, driver_a, driver_b])
            db.session.flush()

            route_a = Route(institution_id=inst_a.id, name="Route A")
            bus_a = Bus(institution_id=inst_a.id, registration_number="REG-A1", bus_code="BUS-A1", driver_id=driver_a.id)
            route_b = Route(institution_id=inst_b.id, name="Route B")
            bus_b = Bus(institution_id=inst_b.id, registration_number="REG-B1", bus_code="BUS-B1", driver_id=driver_b.id)
            db.session.add_all([route_a, bus_a, route_b, bus_b])
            db.session.flush()

            driver_a.assigned_bus_id = bus_a.id
            driver_a.assigned_route_id = route_a.id
            bus_a.route_id = route_a.id

            driver_b.assigned_bus_id = bus_b.id
            driver_b.assigned_route_id = route_b.id
            bus_b.route_id = route_b.id

            stop_a = Stop(institution_id=inst_a.id, route_id=route_a.id, stop_name="Stop A", sequence_order=1, latitude=19.0, longitude=72.9)
            db.session.add(stop_a)
            db.session.flush()

            student_a = Student(
                institution_id=inst_a.id,
                full_name="Student A",
                roll_number="STU-A1",
                parent_id=parent_a.id,
                assigned_bus_id=bus_a.id,
                assigned_route_id=route_a.id,
                drop_stop_id=stop_a.id,
            )
            db.session.add(student_a)
            db.session.flush()

            trip_a = Trip(
                institution_id=inst_a.id,
                bus_id=bus_a.id,
                driver_id=driver_a.id,
                route_id=route_a.id,
                trip_type="EVENING_DROP",
                status="IN_PROGRESS",
                start_time=datetime.utcnow(),
            )
            db.session.add(trip_a)
            db.session.commit()

            data = {
                'client': client,
                'app': app,
                'inst_a_id': inst_a.id,
                'inst_b_id': inst_b.id,
                'super_id': super_u.id,
                'admin_a_id': admin_a.id,
                'admin_b_id': admin_b.id,
                'driver_a_u_id': driver_a_u.id,
                'driver_b_u_id': driver_b_u.id,
                'parent_a_id': parent_a.id,
                'student_a_id': student_a.id,
                'stop_a_id': stop_a.id,
                'trip_a_id': trip_a.id,
            }
        yield data


# 1. Public database initialization endpoint unavailable
def test_public_force_seed_endpoint_removed(phase1a_app):
    client = phase1a_app['client']
    res_get = client.get('/force-seed-database-secret-url-2026')
    assert res_get.status_code == 404

    res_post = client.post('/force-seed-database-secret-url-2026')
    assert res_post.status_code == 404


# 2. Password reset does not expose tokens or URLs (response or logs)
def test_forgot_password_does_not_expose_tokens_or_enumerate_users(phase1a_app, caplog):
    client = phase1a_app['client']
    app = phase1a_app['app']
    caplog.set_level(logging.DEBUG)

    # Case A: Fail-closed when email provider is unconfigured
    app.config['EMAIL_PROVIDER'] = 'none'
    res_existing = client.post('/auth/forgot-password', data={'email': 'admin_a@example.com'}, follow_redirects=True)
    res_missing = client.post('/auth/forgot-password', data={'email': 'nobody@example.com'}, follow_redirects=True)

    assert res_existing.status_code == 200
    assert res_missing.status_code == 200
    assert b'/auth/reset-password/' not in res_existing.data
    assert b'reset-password' not in res_existing.data
    assert b'If an account exists with that email, password reset instructions have been sent.' in res_existing.data
    assert b'If an account exists with that email, password reset instructions have been sent.' in res_missing.data

    # Case B: Email provider configured (mocked SMTP) — still generic response and no token in response/logs
    app.config['EMAIL_PROVIDER'] = 'smtp'
    app.config['SMTP_HOST'] = 'smtp.example.com'
    app.config['MAIL_FROM_ADDRESS'] = 'noreply@example.com'
    caplog.clear()

    sent_urls = []

    def fake_send_email(recipient, reset_url):
        sent_urls.append((recipient, reset_url))
        return True

    with patch('app.auth.routes.send_password_reset_email', side_effect=fake_send_email):
        res_smtp = client.post('/auth/forgot-password', data={'email': 'admin_a@example.com'}, follow_redirects=True)

    assert res_smtp.status_code == 200
    assert len(sent_urls) == 1
    dispatched_url = sent_urls[0][1]
    dispatched_token = dispatched_url.rsplit('/', 1)[-1]
    assert dispatched_token not in res_smtp.get_data(as_text=True)
    assert dispatched_url not in res_smtp.get_data(as_text=True)
    assert dispatched_token not in caplog.text
    assert dispatched_url not in caplog.text


# 3. Valid, invalid, and expired password reset tokens
def test_password_reset_token_validation_and_rejection(phase1a_app):
    client = phase1a_app['client']
    app = phase1a_app['app']
    admin_a_id = phase1a_app['admin_a_id']

    with app.app_context():
        valid_token = generate_password_reset_token(admin_a_id)
        assert verify_password_reset_token(valid_token) == admin_a_id
        assert verify_password_reset_token('invalid.token.value') is None
        assert verify_password_reset_token(valid_token + 'tampered') is None
        assert verify_password_reset_token('') is None
        assert verify_password_reset_token(None) is None

        # Simulate expired token
        time.sleep(1.1)
        assert verify_password_reset_token(valid_token, max_age_seconds=0) is None

    # HTTP GET & POST with invalid token should be rejected and redirected
    res_bad_get = client.get('/auth/reset-password/not-a-valid-token', follow_redirects=False)
    assert res_bad_get.status_code == 302
    assert '/auth/forgot-password' in res_bad_get.location

    res_bad_post = client.post(
        '/auth/reset-password/not-a-valid-token',
        data={'password': 'NewStrongPassword1!', 'confirm_password': 'NewStrongPassword1!'},
        follow_redirects=False,
    )
    assert res_bad_post.status_code == 302
    assert '/auth/forgot-password' in res_bad_post.location

    # HTTP POST with valid token updates password
    with app.app_context():
        fresh_token = generate_password_reset_token(admin_a_id)

    res_ok = client.post(
        f'/auth/reset-password/{fresh_token}',
        data={'password': 'NewStrongPassword1!', 'confirm_password': 'NewStrongPassword1!'},
        follow_redirects=False,
    )
    assert res_ok.status_code == 302
    assert '/auth/login' in res_ok.location

    with app.app_context():
        user = db.session.get(User, admin_a_id)
        assert user.check_password('NewStrongPassword1!') is True


# 4 & 5. Stripe webhook rejects missing and invalid signatures in production
def test_stripe_webhook_rejects_missing_and_invalid_signatures_in_production(phase1a_app):
    client = phase1a_app['client']
    app = phase1a_app['app']
    inst_a_id = phase1a_app['inst_a_id']

    app.config['ENV'] = 'production'
    app.config['STRIPE_WEBHOOK_SECRET'] = 'whsec_prod_real_test_secret_123456'

    payload = {
        'type': 'checkout.session.completed',
        'data': {
            'object': {
                'client_reference_id': str(inst_a_id),
                'customer': 'cus_forged',
                'subscription': 'sub_forged',
                'metadata': {'target_plan_tier': 'ENTERPRISE'},
            }
        },
    }

    # Unsigned request in production must be rejected (even with TESTING=True)
    res_unsigned = client.post('/billing/webhook', json=payload)
    assert res_unsigned.status_code == 400
    assert 'Stripe-Signature' in res_unsigned.get_json()['message']

    # Invalid signature in production must be rejected
    res_invalid_sig = client.post(
        '/billing/webhook',
        data=json.dumps(payload),
        content_type='application/json',
        headers={'Stripe-Signature': 't=12345,v1=forgedsignature'},
    )
    assert res_invalid_sig.status_code == 400
    assert res_invalid_sig.get_json()['message'] == 'Invalid signature'

    # Valid signature via stripe.Webhook.construct_event must succeed
    with patch('stripe.Webhook.construct_event', return_value=payload):
        res_valid = client.post(
            '/billing/webhook',
            data=json.dumps(payload),
            content_type='application/json',
            headers={'Stripe-Signature': 't=12345,v1=validsig'},
        )
    assert res_valid.status_code == 200
    assert res_valid.get_json()['status'] == 'success'


# 6. Institutional SOS & safety notifications remain tenant-scoped
def test_sos_and_safety_notifications_are_tenant_scoped(phase1a_app):
    client = phase1a_app['client']
    app = phase1a_app['app']
    admin_a_id = phase1a_app['admin_a_id']
    admin_b_id = phase1a_app['admin_b_id']
    super_id = phase1a_app['super_id']
    driver_a_u_id = phase1a_app['driver_a_u_id']
    trip_a_id = phase1a_app['trip_a_id']

    with client.session_transaction() as sess:
        sess['_user_id'] = str(driver_a_u_id)
        sess['_fresh'] = True

    # Online SOS from Institution A driver
    res_sos = client.post('/driver/api/trigger-sos', json={
        'trip_id': trip_a_id,
        'latitude': 19.01,
        'longitude': 72.91,
        'description': 'SOS in Institution A',
    })
    assert res_sos.status_code == 200

    # Offline SOS sync from Institution A driver
    res_offline_sos = client.post('/driver/api/sync-offline-queue', json={
        'queue': [],
        'sos_queue': [{
            'trip_id': trip_a_id,
            'latitude': 19.02,
            'longitude': 72.92,
            'description': 'Offline SOS in Institution A',
        }],
    })
    assert res_offline_sos.status_code == 200

    # Safe drop escalation for Institution A student
    with app.app_context():
        sd = SafeDropConfirmation(
            student_id=phase1a_app['student_a_id'],
            trip_id=trip_a_id,
            stop_id=phase1a_app['stop_a_id'],
            parent_id=phase1a_app['parent_a_id'],
            bus_arrival_time=datetime.utcnow() - timedelta(minutes=30),
            status='PENDING',
        )
        db.session.add(sd)
        db.session.commit()
        check_and_escalate_pending_safe_drops()

        admin_a_notifs = Notification.query.filter_by(user_id=admin_a_id).all()
        admin_b_notifs = Notification.query.filter_by(user_id=admin_b_id).all()
        assert len(admin_a_notifs) == 3
        assert len(admin_b_notifs) == 0

        # Unscoped broadcast_admin_notification must fail closed
        unscoped = broadcast_admin_notification("Test", "Should not broadcast", institution_id=None)
        assert unscoped == []
        assert Notification.query.filter_by(user_id=admin_b_id).count() == 0

        # Explicit Super Admin broadcast targets only SUPER_ADMIN users
        sa_notifs = broadcast_superadmin_notification("Platform Alert", "SuperAdmin Only")
        assert len(sa_notifs) == 1
        assert Notification.query.filter_by(user_id=super_id).count() == 1
        assert Notification.query.filter_by(user_id=admin_b_id).count() == 0


# 7. Institutional telemetry notifications remain tenant-scoped
def test_telemetry_notifications_are_tenant_scoped(phase1a_app):
    client = phase1a_app['client']
    app = phase1a_app['app']
    admin_a_id = phase1a_app['admin_a_id']
    admin_b_id = phase1a_app['admin_b_id']
    driver_a_u_id = phase1a_app['driver_a_u_id']

    with client.session_transaction() as sess:
        sess['_user_id'] = str(driver_a_u_id)
        sess['_fresh'] = True

    res = client.post('/driver/telemetry/log', json={
        'fatigue_score': 0.96,
        'fatigue_level': 'CRITICAL_DROWSINESS',
        'eye_closure_sec': 3.1,
        'yawn_count': 4,
        'distraction_event': 'EYES_OFF_ROAD',
        'speed_kph': 52.0,
        'overspeed_warning': False,
        'harsh_braking_warning': False,
    })
    assert res.status_code == 200
    assert res.get_json()['alert_created'] is True

    with app.app_context():
        assert Notification.query.filter_by(user_id=admin_a_id).count() == 1
        assert Notification.query.filter_by(user_id=admin_b_id).count() == 0


# 8. Production configuration rejects missing or insecure secrets and sets secure cookies
def test_production_config_safeguards(monkeypatch):
    import config

    try:
        # Missing SECRET_KEY / WTF_CSRF_SECRET_KEY
        monkeypatch.setenv('FLASK_ENV', 'production')
        monkeypatch.delenv('SECRET_KEY', raising=False)
        monkeypatch.delenv('WTF_CSRF_SECRET_KEY', raising=False)
        with pytest.raises(ValueError, match="SECRET_KEY"):
            importlib.reload(config)

        # Dev placeholder secret rejected in production
        monkeypatch.setenv('SECRET_KEY', config.DEV_INSECURE_SECRET_KEY)
        monkeypatch.setenv('WTF_CSRF_SECRET_KEY', 'prod-csrf-secret-key-at-least-16-chars')
        with pytest.raises(ValueError, match="SECRET_KEY"):
            importlib.reload(config)

        # Placeholder Stripe secret rejected in production
        monkeypatch.setenv('SECRET_KEY', 'prod-secret-key-very-strong-random-value')
        monkeypatch.setenv('WTF_CSRF_SECRET_KEY', 'prod-csrf-key-very-strong-random-value')
        monkeypatch.setenv('STRIPE_WEBHOOK_SECRET', 'whsec_mock_webhook_secret_2026')
        with pytest.raises(ValueError, match="STRIPE_WEBHOOK_SECRET"):
            importlib.reload(config)

        # Valid production config enforces non-debug & secure cookie settings
        monkeypatch.delenv('STRIPE_WEBHOOK_SECRET', raising=False)
        monkeypatch.setenv('FLASK_DEBUG', 'true')
        importlib.reload(config)
        assert config.Config.DEBUG is False
        assert config.Config.SESSION_COOKIE_SECURE is True
        assert config.Config.SESSION_COOKIE_HTTPONLY is True
        assert config.Config.SESSION_COOKIE_SAMESITE == 'Lax'
        assert config.Config.REMEMBER_COOKIE_SECURE is True
        assert config.Config.STRIPE_SECRET_KEY is None
        assert config.Config.STRIPE_WEBHOOK_SECRET is None
    finally:
        monkeypatch.setenv('FLASK_ENV', 'development')
        monkeypatch.delenv('SECRET_KEY', raising=False)
        monkeypatch.delenv('WTF_CSRF_SECRET_KEY', raising=False)
        monkeypatch.delenv('STRIPE_WEBHOOK_SECRET', raising=False)
        monkeypatch.delenv('FLASK_DEBUG', raising=False)
        importlib.reload(config)


def test_seed_database_blocked_in_production(monkeypatch):
    from app.seed import seed_database

    monkeypatch.setenv('FLASK_ENV', 'production')
    with pytest.raises(RuntimeError, match="disabled in production"):
        seed_database()
