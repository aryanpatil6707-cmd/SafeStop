# 🚌 SafeRide AI — School & College Student Live Bus Tracking & Parent Safety System

**SafeRide AI / SafeStop** is an enterprise-grade, web-based transportation tracking and safety management platform specifically designed for **school and college students and their parents** who use institutional bus services.

> ⚠️ **100% Software-Only Architecture — Zero Hardware Dependency**
> SafeRide AI requires **NO specialized hardware**, NO RFID readers, NO card scanners, NO Arduino/ESP32 microcontrollers, and **NO smartphones or devices for students**. 
> The platform operates strictly through responsive web software running on the driver's standard mobile browser and cloud/Python backend servers.

---

## 🌟 Core Value Proposition for Parents & Students

SafeRide AI gives parents full peace of mind by answering four key questions in real-time:
> **WHO is travelling + WHICH BUS they are travelling in + WHERE the bus is right now + WHETHER your child has safely boarded/arrived.**

---

## 👨‍👩‍👧 Key Features for Parents

1. **Private Live Child Tracking**
   - Parents log in to view **only their child's live location**, assigned bus route, and real-time movement on an interactive map.
   - Includes a custom **`🧒 [Child Name]` Live Pin** that animates along the route.

2. **Real-Time Safety Statuses**
   - **`📍 Waiting at Pickup Stop`**: Child is registered at their designated pickup stop.
   - **`🧒🚌 On Board Bus`**: Facial AI verifies when the student boards the bus; location tracks the live moving vehicle.
   - **`🏠 Safely Dropped`**: Child arrives at their drop destination.

3. **1-Tap Safe Drop Arrival Confirmation**
   - When the bus reaches the student's drop stop, the parent receives an instant notification to confirm receipt (`[ Child Received ]`).

4. **Parent ETA Proximity Alerts & Geo-Fenced Stop Triggers**
   - Live distance & ETA engine monitors bus GPS position in real-time.
   - Automatically dispatches `"🚌 Bus is 500m (~3 mins) away from stop"` alerts as the bus approaches.
   - Customizable proximity alert thresholds (250m, 500m, 1000m).

5. **Parent Leave / Absence Request Portal**
   - Log single or multi-day leave/sick requests directly from the parent dashboard.
   - Automatically marks students as `[ 🤒 ON LEAVE (Parent Verified) ]` on driver rosters.

---

## 🏛️ Multi-Tenancy Architecture (Institutional Isolation)

SafeStop supports multiple school/college institutions natively.
- **Strict Data Scoping**: All database models (`User`, `Student`, `Driver`, `Bus`, `Route`, `Trip`, `Alert`, `AuditLog`) are institution-scoped.
- **Cross-Tenant Guardrails**: Automatic authorization checks reject illegal cross-tenant data requests and enforce tenant-scoped safety notifications.
- **Parent Self-Registration**: Parents can register directly using the unique student invite code issued by their institution administrator.

---

## 📱 Real Outbound Notifications (SMS & WhatsApp Integration)

SafeStop features an extensible notification delivery system:
- **Twilio Provider (`TwilioProvider`)**: Real SMS and WhatsApp messages delivered to parents for high-priority alerts (`BOARDING`, `ARRIVAL`, `SAFE_DROP`, `EMERGENCY`, `WRONG_BUS`).
- **Console Provider (`ConsoleProvider`)**: Zero-dependency development provider logging outbound SMS and WhatsApp payloads to standard output.
- **Fault-Tolerant Delivery**: Outbound API failures log warnings without interrupting critical attendance workflows.

---

## 🔒 Security Hardening & Rate Limiting

- **Production Environment Secrets Enforcement**: Application startup fails safely if default/insecure keys are used in `production`.
- **Rate Limiting**: `Flask-Limiter` protects authentication routes (`/auth/login`, `/auth/forgot-password`, `/auth/reset-password`) against brute-force attacks.
- **Manual Verification Auditing**: All driver manual attendance overrides create immutable `AuditLog` entries (`action='MANUAL_VERIFY_OVERRIDE'`) and surface override rates per driver/bus on the admin analytics dashboard.

---

## 🛡️ Biometric Data Governance & DSAR Compliance

- **Parental Consent Tracking**: Biometric face profile vectors are stored only with explicit parental consent.
- **Data Subject Access Request (DSAR)**: Parents can export full attendance & biometric consent records (`/parent/dsar/export/<student_id>`) or request complete face vector erasure (`/parent/dsar/delete-biometrics/<student_id>`).
- **Automated Data Retention Purge**: Background purge job (`purge_expired_biometric_data(retention_days=365)`) automatically hard-deletes face vectors archived longer than the retention window.

---

## 🚀 Quick Start Guide

### 1. Requirements
- Python 3.10+
- Modern Web Browser (Chrome, Edge, Safari, Firefox)

### 2. Run Application
```bash
python run.py
```
*Note: The application auto-detects and preserves existing user database records across server restarts.*

Open **http://127.0.0.1:5000** in your browser.

### 3. Database Management & Initial Account Setup
SafeStop uses Flask-Migrate (Alembic) for schema migrations:
```bash
flask db upgrade                                  # Apply database migrations to production/local DB
python manage_db.py status                        # View real-time database counts
```

#### Creating a Secure Initial Administrator Account
Production deployments do **not** seed default administrator or driver accounts. Create your initial Super Admin and institutional Admin accounts explicitly via the CLI:
```bash
python manage_db.py create-superadmin --email superadmin@your-domain.com --password "<STRONG_PASSWORD>"
python manage_db.py create-admin --email admin@your-school.edu --password "<STRONG_PASSWORD>" --name "Transport Director" --institution-slug your-school --institution-name "Your School Name"
```

#### Development-Only Synthetic Demo Dataset
For local development testing only (blocked automatically when `FLASK_ENV=production` / `APP_ENV=production`):
```bash
python manage_db.py seed
```
This seeds clearly synthetic demonstration data (`Demo Academy Alpha`, `Demo Academy Beta`) with non-hardcoded credentials printed only to your local terminal (or supplied via `DEV_DEMO_ADMIN_PASSWORD`, `DEV_DEMO_DRIVER_PASSWORD`, `DEV_DEMO_PARENT_PASSWORD`).

### 4. Required Production Environment Variables
When deploying in production (`FLASK_ENV=production`), the application enforces explicit configuration and refuses to boot with development defaults:
- `FLASK_ENV=production` (or `APP_ENV=production`)
- `SECRET_KEY` — Strong random secret key (not a development placeholder)
- `WTF_CSRF_SECRET_KEY` — Strong random CSRF secret key (not a development placeholder)
- `DATABASE_URL` — Production PostgreSQL connection string
- `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` — Live/test Stripe credentials (no mock placeholders; `STRIPE_WEBHOOK_SECRET` is required to accept signed Stripe webhook events)
- `EMAIL_PROVIDER` (`smtp` or `none`, default `none`): Password reset fails closed without exposing reset tokens or URLs unless an outbound SMTP provider is configured (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `MAIL_FROM_ADDRESS`).

### 5. Run Automated Test Suite
Execute unit tests using pytest:
```bash
python -m pytest
```

---

## 🛠️ Software Stack

- **Backend**: Python 3, Flask, SQLAlchemy ORM, Flask-Login, Werkzeug Security
- **Database**: SQLite (Zero-setup local execution) / Relational database schema
- **AI & Facial Recognition**: OpenCV, NumPy, Scikit-Learn (Cosine Similarity Feature Matching)
- **Maps & Live Tracking**: Leaflet.js, OpenStreetMap, HTML5 Geolocation API
- **Frontend & UI**: HTML5, CSS3, JavaScript ES6, Bootstrap 5, Chart.js, Animate.css
