import json
import os
import secrets
from datetime import datetime, timedelta
import numpy as np
from app import create_app
from app.models import (
    db, Institution, User, Parent, Student, Driver, DriverTraining,
    Bus, Route, Stop, Trip, Attendance, Alert, Notification,
    FaceProfile
)


def _dev_password(env_var):
    """Return an explicitly supplied dev password or generate a random one-time password."""
    return os.environ.get(env_var) or secrets.token_urlsafe(12)


def seed_database():
    """
    Seed synthetic demonstration data for local development and testing ONLY.
    Refuses to run in production environments.
    """
    env = (
        os.environ.get('FLASK_ENV')
        or os.environ.get('APP_ENV')
        or ('production' if os.environ.get('RENDER', '').lower() == 'true' else 'development')
    ).lower()
    if env == 'production':
        raise RuntimeError("SECURITY BLOCK: Demonstration data seeding is disabled in production.")

    app = create_app()
    if str(app.config.get('ENV', 'development')).lower() == 'production':
        raise RuntimeError("SECURITY BLOCK: Demonstration data seeding is disabled in production.")

    with app.app_context():
        if Institution.query.count() > 0:
            print("Database already seeded. Skipping.")
            return

        admin_pass = _dev_password('DEV_DEMO_ADMIN_PASSWORD')
        driver_pass = _dev_password('DEV_DEMO_DRIVER_PASSWORD')
        parent_pass = _dev_password('DEV_DEMO_PARENT_PASSWORD')

        print("Seeding synthetic multi-tenant demonstration institutions...")
        inst_alpha = Institution(
            name='Demo Academy Alpha',
            slug='demo-alpha',
            contact_email='contact@demo-alpha.example.com',
            plan_tier='ENTERPRISE',
            invite_code='DEMO-ALPHA-2026',
            is_active=True
        )
        inst_beta = Institution(
            name='Demo Academy Beta',
            slug='demo-beta',
            contact_email='contact@demo-beta.example.com',
            plan_tier='STARTER',
            invite_code='DEMO-BETA-2026',
            is_active=True
        )
        db.session.add_all([inst_alpha, inst_beta])
        db.session.flush()

        # Synthetic Admin User for Demo Academy Alpha
        admin_user = User(
            institution_id=inst_alpha.id,
            email='admin.alpha@example.com',
            role='ADMIN',
            full_name='Demo Transport Admin Alpha',
            phone='+1-555-0100'
        )
        admin_user.set_password(admin_pass)
        db.session.add(admin_user)

        # Synthetic Driver Users
        driver_specs = [
            ('driver1.alpha@example.com', 'Demo Driver 01', '+1-555-0101', 40, 'DEMO-LIC-001', '2029-12-31', True, True),
            ('driver2.alpha@example.com', 'Demo Driver 02', '+1-555-0102', 38, 'DEMO-LIC-002', '2030-05-15', True, False),
            ('driver3.alpha@example.com', 'Demo Driver 03', '+1-555-0103', 44, 'DEMO-LIC-003', '2028-08-20', True, False),
        ]
        driver_users = []
        for email, name, phone, *_ in driver_specs:
            u = User(institution_id=inst_alpha.id, email=email, role='DRIVER', full_name=name, phone=phone)
            u.set_password(driver_pass)
            db.session.add(u)
            driver_users.append(u)

        # Synthetic Parent Users
        parent_models = []
        parent_users = []
        for idx in range(1, 7):
            email = f'parent{idx}.alpha@example.com'
            name = f'Demo Parent {idx:02d}'
            phone = f'+1-555-01{10 + idx:02d}'
            address = f'{100 + idx} Sample Street, Demo District, 000001'
            rel = 'Guardian' if idx % 2 == 0 else 'Parent'
            u = User(institution_id=inst_alpha.id, email=email, role='PARENT', full_name=name, phone=phone)
            u.set_password(parent_pass)
            db.session.add(u)
            parent_users.append((u, address, phone, rel))

        db.session.flush()

        for u, address, phone, rel in parent_users:
            p = Parent(institution_id=inst_alpha.id, user_id=u.id, address=address, emergency_contact=phone, relationship=rel)
            db.session.add(p)
            parent_models.append(p)

        db.session.flush()

        # Synthetic Routes & Stops (generic coordinates)
        r_a = Route(
            institution_id=inst_alpha.id,
            name='Demo Route A — North Loop',
            description='Synthetic demonstration route A',
            distance_km=12.5,
            estimated_duration_mins=35
        )
        r_b = Route(
            institution_id=inst_alpha.id,
            name='Demo Route B — South Loop',
            description='Synthetic demonstration route B',
            distance_km=16.8,
            estimated_duration_mins=45
        )
        db.session.add_all([r_a, r_b])
        db.session.flush()

        st_a1 = Stop(institution_id=inst_alpha.id, route_id=r_a.id, stop_name='Demo Stop A1', sequence_order=1, latitude=19.0645, longitude=72.9976, scheduled_pickup_time='07:15 AM', scheduled_drop_time='03:30 PM')
        st_a2 = Stop(institution_id=inst_alpha.id, route_id=r_a.id, stop_name='Demo Stop A2', sequence_order=2, latitude=19.0720, longitude=72.9930, scheduled_pickup_time='07:30 AM', scheduled_drop_time='03:45 PM')
        st_a3 = Stop(institution_id=inst_alpha.id, route_id=r_a.id, stop_name='Demo Campus Gate', sequence_order=3, latitude=19.0772, longitude=72.9984, scheduled_pickup_time='08:15 AM', scheduled_drop_time='03:15 PM')

        st_b1 = Stop(institution_id=inst_alpha.id, route_id=r_b.id, stop_name='Demo Stop B1', sequence_order=1, latitude=19.0180, longitude=73.0400, scheduled_pickup_time='07:10 AM', scheduled_drop_time='03:35 PM')
        st_b2 = Stop(institution_id=inst_alpha.id, route_id=r_b.id, stop_name='Demo Stop B2', sequence_order=2, latitude=19.0335, longitude=73.0185, scheduled_pickup_time='07:25 AM', scheduled_drop_time='03:50 PM')
        st_b3 = Stop(institution_id=inst_alpha.id, route_id=r_b.id, stop_name='Demo Campus Gate', sequence_order=3, latitude=19.0772, longitude=72.9984, scheduled_pickup_time='08:20 AM', scheduled_drop_time='03:15 PM')

        db.session.add_all([st_a1, st_a2, st_a3, st_b1, st_b2, st_b3])
        db.session.flush()

        d1 = Driver(
            institution_id=inst_alpha.id,
            user_id=driver_users[0].id,
            full_name='Demo Driver 01',
            age=40,
            phone='+1-555-0101',
            address='Garage Depot 1, Sample City 000001',
            license_number='DEMO-LIC-001',
            license_expiry='2029-12-31',
            is_verified=True,
            is_online=True,
            current_lat=19.0720,
            current_lng=72.9930,
            assigned_route_id=r_a.id
        )
        d2 = Driver(
            institution_id=inst_alpha.id,
            user_id=driver_users[1].id,
            full_name='Demo Driver 02',
            age=38,
            phone='+1-555-0102',
            address='Garage Depot 2, Sample City 000001',
            license_number='DEMO-LIC-002',
            license_expiry='2030-05-15',
            is_verified=True,
            is_online=False,
            assigned_route_id=r_b.id
        )
        d3 = Driver(
            institution_id=inst_alpha.id,
            user_id=driver_users[2].id,
            full_name='Demo Driver 03',
            age=44,
            phone='+1-555-0103',
            address='Garage Depot 3, Sample City 000001',
            license_number='DEMO-LIC-003',
            license_expiry='2028-08-20',
            is_verified=True,
            is_online=False
        )
        db.session.add_all([d1, d2, d3])
        db.session.flush()

        dt1 = DriverTraining(driver_id=d1.id, training_name='First Aid & Emergency Medical Care', training_date='2026-01-15', expiry_date='2027-01-15', status='COMPLETED')
        dt2 = DriverTraining(driver_id=d2.id, training_name='Defensive Driving & Safety', training_date='2025-11-20', expiry_date='2026-11-20', status='COMPLETED')
        db.session.add_all([dt1, dt2])

        bus_01 = Bus(institution_id=inst_alpha.id, registration_number='DEMO-REG-001', bus_code='DEMO-BUS-01', capacity=40, status='ACTIVE', current_lat=19.0720, current_lng=72.9930, driver_id=d1.id, route_id=r_a.id)
        bus_02 = Bus(institution_id=inst_alpha.id, registration_number='DEMO-REG-002', bus_code='DEMO-BUS-02', capacity=40, status='INACTIVE', driver_id=d2.id, route_id=r_b.id)
        bus_03 = Bus(institution_id=inst_alpha.id, registration_number='DEMO-REG-003', bus_code='DEMO-BUS-03', capacity=35, status='MAINTENANCE', driver_id=d3.id)
        db.session.add_all([bus_01, bus_02, bus_03])
        db.session.flush()

        d1.assigned_bus_id = bus_01.id
        d2.assigned_bus_id = bus_02.id
        d3.assigned_bus_id = bus_03.id

        students_data = [
            ('Demo Student 01', 'DEMO-STU-001', 'Grade 10-A', parent_models[0].id, bus_01.id, r_a.id, st_a1.id, st_a3.id),
            ('Demo Student 02', 'DEMO-STU-002', 'Grade 10-B', parent_models[1].id, bus_01.id, r_a.id, st_a2.id, st_a3.id),
            ('Demo Student 03', 'DEMO-STU-003', 'Grade 9-A', parent_models[2].id, bus_01.id, r_a.id, st_a1.id, st_a3.id),
            ('Demo Student 04', 'DEMO-STU-004', 'Grade 8-A', parent_models[3].id, bus_02.id, r_b.id, st_b1.id, st_b3.id),
            ('Demo Student 05', 'DEMO-STU-005', 'Grade 8-B', parent_models[4].id, bus_02.id, r_b.id, st_b2.id, st_b3.id),
            ('Demo Student 06', 'DEMO-STU-006', 'Grade 7-A', parent_models[5].id, bus_02.id, r_b.id, st_b1.id, st_b3.id),
        ]

        created_students = []
        for name, roll, grade, p_id, b_id, r_id, pick_id, drop_id in students_data:
            st = Student(
                institution_id=inst_alpha.id,
                full_name=name,
                roll_number=roll,
                grade_section=grade,
                parent_id=p_id,
                assigned_bus_id=b_id,
                assigned_route_id=r_id,
                pickup_stop_id=pick_id,
                drop_stop_id=drop_id,
                photo_filename='default_student.png'
            )
            db.session.add(st)
            created_students.append(st)

        db.session.flush()

        for st in created_students:
            v = np.zeros(64 * 64, dtype=np.float32)
            v[st.id * 25:(st.id + 1) * 25] = 0.5
            norm = np.linalg.norm(v)
            if norm > 0:
                v = v / norm
            fp = FaceProfile(student_id=st.id, feature_vector_json=json.dumps(v.tolist()), image_path=st.photo_filename)
            db.session.add(fp)

        t_active = Trip(
            institution_id=inst_alpha.id,
            bus_id=bus_01.id,
            driver_id=d1.id,
            route_id=r_a.id,
            trip_type='MORNING_PICKUP',
            status='IN_PROGRESS',
            start_time=datetime.utcnow() - timedelta(minutes=25),
            current_lat=19.0720,
            current_lng=72.9930
        )
        db.session.add(t_active)
        db.session.flush()

        for st in created_students[:2]:
            att = Attendance(
                student_id=st.id,
                trip_id=t_active.id,
                bus_id=bus_01.id,
                verification_status='VERIFIED',
                verified_by_driver_id=d1.id,
                notes=f'Synthetic demo verification for {st.full_name}'
            )
            db.session.add(att)
            n = Notification(
                user_id=st.parent.user.id,
                title='🚌 Boarding Confirmed',
                message=f'Demo student {st.full_name} boarded DEMO-BUS-01.',
                category='BOARDING'
            )
            db.session.add(n)

        al1 = Alert(
            institution_id=inst_alpha.id,
            bus_id=bus_01.id,
            trip_id=t_active.id,
            alert_type='BUS_DELAY',
            risk_score=20,
            severity='WARNING',
            description='DEMO-BUS-01 simulated 8-minute delay on Demo Route A.'
        )
        db.session.add(al1)

        # Secondary synthetic institution (Demo Academy Beta)
        admin_beta = User(
            institution_id=inst_beta.id,
            email='admin.beta@example.com',
            role='ADMIN',
            full_name='Demo Transport Admin Beta',
            phone='+1-555-0200'
        )
        admin_beta.set_password(admin_pass)
        db.session.add(admin_beta)

        driver_beta_u = User(
            institution_id=inst_beta.id,
            email='driver.beta@example.com',
            role='DRIVER',
            full_name='Demo Driver Beta',
            phone='+1-555-0201'
        )
        driver_beta_u.set_password(driver_pass)
        db.session.add(driver_beta_u)
        db.session.flush()

        driver_beta = Driver(
            institution_id=inst_beta.id,
            user_id=driver_beta_u.id,
            full_name='Demo Driver Beta',
            phone='+1-555-0201',
            license_number='DEMO-LIC-BETA-01',
            license_expiry='2031-12-31'
        )
        db.session.add(driver_beta)

        parent_beta_u = User(
            institution_id=inst_beta.id,
            email='parent.beta@example.com',
            role='PARENT',
            full_name='Demo Parent Beta',
            phone='+1-555-0202'
        )
        parent_beta_u.set_password(parent_pass)
        db.session.add(parent_beta_u)
        db.session.flush()

        parent_beta = Parent(institution_id=inst_beta.id, user_id=parent_beta_u.id, address='200 Sample Ave, Demo District, 000002')
        db.session.add(parent_beta)
        db.session.flush()

        route_beta = Route(institution_id=inst_beta.id, name='Demo Beta Route 1', distance_km=8.0)
        db.session.add(route_beta)
        db.session.flush()

        bus_beta = Bus(institution_id=inst_beta.id, registration_number='DEMO-BETA-101', bus_code='DEMO-BETA-01', driver_id=driver_beta.id, route_id=route_beta.id)
        db.session.add(bus_beta)
        db.session.flush()
        driver_beta.assigned_bus_id = bus_beta.id

        student_beta = Student(
            institution_id=inst_beta.id,
            full_name='Demo Student Beta',
            roll_number='DEMO-BETA-STU-001',
            grade_section='Grade 6-A',
            parent_id=parent_beta.id,
            assigned_bus_id=bus_beta.id,
            assigned_route_id=route_beta.id
        )
        db.session.add(student_beta)
        db.session.flush()

        v_beta = np.zeros(64 * 64, dtype=np.float32)
        v_beta[0:100] = 0.8
        norm_beta = np.linalg.norm(v_beta)
        if norm_beta > 0:
            v_beta = v_beta / norm_beta

        fp_beta = FaceProfile(student_id=student_beta.id, feature_vector_json=json.dumps(v_beta.tolist()), image_path='default_student.png')
        db.session.add(fp_beta)

        db.session.commit()
        print("Development database seeded with synthetic multi-tenant data (Demo Academy Alpha & Beta).")
        print("Generated one-time dev credentials (not stored in source):")
        print(f"  Admin  (admin.alpha@example.com / admin.beta@example.com): {admin_pass}")
        print(f"  Driver (driver1.alpha@example.com / driver.beta@example.com): {driver_pass}")
        print(f"  Parent (parent1.alpha@example.com / parent.beta@example.com): {parent_pass}")


if __name__ == '__main__':
    seed_database()
