import os
import sys

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

DEV_INSECURE_SECRET_KEY = 'dev-insecure-secret-key-do-not-use-in-production-2026'
DEV_INSECURE_CSRF_KEY = 'dev-insecure-csrf-key-do-not-use-in-production-2026'

_DISALLOWED_PRODUCTION_SECRETS = {
    DEV_INSECURE_SECRET_KEY,
    DEV_INSECURE_CSRF_KEY,
    'default-secret-key',
    'changeme',
    'secret',
}

_DISALLOWED_PRODUCTION_STRIPE_PREFIXES = (
    'sk_test_mock_',
    'pk_test_mock_',
    'whsec_mock_',
)


def _is_insecure_secret(value):
    if not value or not str(value).strip():
        return True
    val = str(value).strip()
    return val in _DISALLOWED_PRODUCTION_SECRETS or val.startswith('dev-insecure-')


def validate_production_config(cfg):
    """Enforce mandatory security configuration when running in production."""
    env = str(cfg.get('ENV', 'development')).lower()
    if env != 'production':
        return

    if _is_insecure_secret(cfg.get('SECRET_KEY')):
        raise ValueError(
            "CRITICAL SECURITY ERROR: 'SECRET_KEY' environment variable MUST be explicitly configured with a secure production value!"
        )

    if _is_insecure_secret(cfg.get('WTF_CSRF_SECRET_KEY')):
        raise ValueError(
            "CRITICAL SECURITY ERROR: 'WTF_CSRF_SECRET_KEY' environment variable MUST be explicitly configured with a secure production value!"
        )

    for stripe_key_name in ('STRIPE_SECRET_KEY', 'STRIPE_PUBLISHABLE_KEY', 'STRIPE_WEBHOOK_SECRET'):
        val = cfg.get(stripe_key_name)
        if val and any(str(val).startswith(prefix) for prefix in _DISALLOWED_PRODUCTION_STRIPE_PREFIXES):
            raise ValueError(
                f"CRITICAL SECURITY ERROR: '{stripe_key_name}' cannot use a placeholder mock value in production!"
            )


class Config:
    # Environment Configuration
    ENV = (
        os.environ.get('FLASK_ENV')
        or os.environ.get('APP_ENV')
        or ('production' if os.environ.get('RENDER', '').lower() == 'true' else 'development')
    ).lower()

    TESTING = False
    DEBUG = False if ENV == 'production' else os.environ.get('FLASK_DEBUG', 'false').lower() in ('1', 'true', 'yes')

    # Security Configuration
    if ENV == 'production':
        _secret = os.environ.get('SECRET_KEY')
        if _is_insecure_secret(_secret):
            raise ValueError(
                "CRITICAL SECURITY ERROR: 'SECRET_KEY' environment variable MUST be explicitly configured in production mode!"
            )
        SECRET_KEY = _secret

        _csrf_secret = os.environ.get('WTF_CSRF_SECRET_KEY')
        if _is_insecure_secret(_csrf_secret):
            raise ValueError(
                "CRITICAL SECURITY ERROR: 'WTF_CSRF_SECRET_KEY' environment variable MUST be explicitly configured in production mode!"
            )
        WTF_CSRF_SECRET_KEY = _csrf_secret
    else:
        SECRET_KEY = os.environ.get('SECRET_KEY') or DEV_INSECURE_SECRET_KEY
        WTF_CSRF_SECRET_KEY = os.environ.get('WTF_CSRF_SECRET_KEY') or DEV_INSECURE_CSRF_KEY

    WTF_CSRF_ENABLED = True

    # Session & Cookie Security (HTTPS-enforced in production)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = (ENV == 'production')
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_SECURE = (ENV == 'production')
    PREFERRED_URL_SCHEME = 'https' if ENV == 'production' else 'http'
    
    # Database Configuration
    _is_testing = ENV == 'testing' or 'pytest' in sys.modules or 'pytest' in sys.argv[0]
    _db_url = os.environ.get('DATABASE_URL')
    if _db_url and _db_url.startswith('postgres://'):
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    
    SQLALCHEMY_DATABASE_URI = _db_url or ('sqlite:///:memory:' if _is_testing else f"sqlite:///{os.path.join(BASE_DIR, 'saferide.db')}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Upload Settings & Security Constraints
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB max upload size
    ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg'}
    ALLOWED_DOCUMENT_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}
    
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
    STUDENT_PHOTOS = os.path.join(UPLOAD_FOLDER, 'students')
    DRIVER_DOCS = os.path.join(UPLOAD_FOLDER, 'drivers')
    ATTENDANCE_SNAPSHOTS = os.path.join(UPLOAD_FOLDER, 'attendance')
    
    # Safety Thresholds
    ROUTE_DEVIATION_THRESHOLD_METERS = float(os.environ.get('ROUTE_DEVIATION_THRESHOLD', 500.0))
    DELAY_THRESHOLD_MINUTES = int(os.environ.get('DELAY_THRESHOLD_MINUTES', 10))
    SAFE_DROP_ESCALATION_MINUTES = int(os.environ.get('SAFE_DROP_ESCALATION_MINUTES', 15))
    ABSENCE_CUTOFF_HOUR = int(os.environ.get('ABSENCE_CUTOFF_HOUR', 7))
    ABSENCE_CUTOFF_MINUTE = int(os.environ.get('ABSENCE_CUTOFF_MINUTE', 30))
    
    # AI Feature Matching Thresholds
    FACE_MATCH_SIMILARITY_THRESHOLD = 0.55  # Synchronized cosine distance threshold
    FACE_MIN_BOUNDING_BOX_SIZE = 60  # Minimum width/height in pixels for valid face region

    # Outbound Email Configuration (Password Reset Delivery)
    EMAIL_PROVIDER = os.environ.get('EMAIL_PROVIDER', 'none').lower()  # 'none' (fail-closed) or 'smtp'
    SMTP_HOST = os.environ.get('SMTP_HOST')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
    SMTP_USERNAME = os.environ.get('SMTP_USERNAME')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
    SMTP_USE_TLS = os.environ.get('SMTP_USE_TLS', 'true').lower() in ('1', 'true', 'yes')
    MAIL_FROM_ADDRESS = os.environ.get('MAIL_FROM_ADDRESS')

    # Outbound Notification Provider Settings
    NOTIFICATION_PROVIDER = os.environ.get('NOTIFICATION_PROVIDER', 'console')  # 'console' or 'twilio'
    TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
    TWILIO_FROM_NUMBER = os.environ.get('TWILIO_FROM_NUMBER') or os.environ.get('TWILIO_PHONE_NUMBER')
    TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER') or TWILIO_FROM_NUMBER
    TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER')

    # Categories default enabled for outbound SMS/WhatsApp dispatch
    OUTBOUND_SMS_CATEGORIES = {'BOARDING', 'ARRIVAL', 'SAFE_DROP', 'EMERGENCY', 'WRONG_BUS', 'NO_SHOW', 'UNSCANNED_DROP'}

    # Stripe Billing Configuration (never use mock placeholders in production)
    if ENV == 'production':
        STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY')
        STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
        STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')
        STRIPE_PRICE_ID_STARTER = os.environ.get('STRIPE_PRICE_ID_STARTER')
        STRIPE_PRICE_ID_PROFESSIONAL = os.environ.get('STRIPE_PRICE_ID_PROFESSIONAL')
        STRIPE_PRICE_ID_ENTERPRISE = os.environ.get('STRIPE_PRICE_ID_ENTERPRISE')

        for _stripe_key, _val in (
            ('STRIPE_SECRET_KEY', STRIPE_SECRET_KEY),
            ('STRIPE_PUBLISHABLE_KEY', STRIPE_PUBLISHABLE_KEY),
            ('STRIPE_WEBHOOK_SECRET', STRIPE_WEBHOOK_SECRET),
        ):
            if _val:
                for _prefix in _DISALLOWED_PRODUCTION_STRIPE_PREFIXES:
                    if str(_val).startswith(_prefix):
                        raise ValueError(
                            f"CRITICAL SECURITY ERROR: '{_stripe_key}' cannot use a placeholder mock value in production!"
                        )
    else:
        STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_mock_secret_key_2026')
        STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_test_mock_publishable_key_2026')
        STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', 'whsec_mock_webhook_secret_2026')
        STRIPE_PRICE_ID_STARTER = os.environ.get('STRIPE_PRICE_ID_STARTER', 'price_starter_tier_2026')
        STRIPE_PRICE_ID_PROFESSIONAL = os.environ.get('STRIPE_PRICE_ID_PROFESSIONAL', 'price_professional_tier_2026')
        STRIPE_PRICE_ID_ENTERPRISE = os.environ.get('STRIPE_PRICE_ID_ENTERPRISE', 'price_enterprise_tier_2026')

