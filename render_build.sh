#!/usr/bin/env bash
# Exit on error
set -o errexit

pip install -r requirements.txt

# Reset the database completely to clear corrupted tables
# We use flask shell to drop tables ignoring migrations
cat << EOF | flask shell
from app.models import db
db.drop_all()
EOF

flask db upgrade

# Re-seed the clean database
python run.py init-db || echo "Skipping init-db"
python run.py seed-db