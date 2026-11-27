import os
import sys
from app import create_app
from app.models import db, User
from app.seed import seed_database

app = create_app()


def _is_production():
    return str(app.config.get('ENV', 'development')).lower() == 'production'


@app.cli.command("init-db")
def init_db_command():
    """Create all database tables without dropping existing data."""
    with app.app_context():
        db.create_all()
        print("Database tables created/verified successfully.")


@app.cli.command("seed-db")
def seed_db_command():
    """Populate database with synthetic development demonstration data (disabled in production)."""
    if _is_production():
        print("ERROR: Demo data seeding is disabled in production.")
        sys.exit(1)
    seed_database()
    print("Development demonstration database seeded successfully.")


if __name__ == '__main__':
    is_prod = _is_production()

    if len(sys.argv) > 1 and sys.argv[1] == 'init-db':
        with app.app_context():
            db.create_all()
            print("Database initialized successfully.")
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == 'seed-db':
        if is_prod:
            print("ERROR: Demo data seeding is disabled in production.")
            sys.exit(1)
        seed_database()
        sys.exit(0)

    if not is_prod:
        with app.app_context():
            # Ensure database tables exist in local development
            db.create_all()

            if User.query.count() == 0:
                if os.environ.get('DEV_AUTO_SEED', '').lower() == 'true':
                    print("DEV_AUTO_SEED=true: Seeding synthetic development demonstration dataset...")
                    seed_database()
                else:
                    print("No existing user accounts found.")
                    print("To load synthetic demo data in development: python manage_db.py seed")
                    print("To create an admin account: python manage_db.py create-admin --email <email> --password <pass>")

    print("\n=======================================================================")
    print(" SAFESTOP -- SMART STUDENT VERIFICATION & TRANSPORT SAFETY SYSTEM")
    print("=======================================================================")
    print(" Management Commands:")
    print(" Initialize Clean DB : python manage_db.py init")
    print(" Create SuperAdmin   : python manage_db.py create-superadmin --email <email> --password <pass>")
    print(" Create Admin        : python manage_db.py create-admin --email <email> --password <pass>")
    print(" Database Status     : python manage_db.py status")
    print("=======================================================================\n")

    port = int(os.environ.get('PORT', 5000))
    debug_mode = False if is_prod else (os.environ.get('FLASK_DEBUG', 'true').lower() in ('1', 'true', 'yes'))
    host = '0.0.0.0' if is_prod else os.environ.get('HOST', '127.0.0.1')
    app.run(host=host, port=port, debug=debug_mode)

