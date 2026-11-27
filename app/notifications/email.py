import logging
import smtplib
from email.message import EmailMessage
from flask import current_app

logger = logging.getLogger(__name__)


def is_email_configured(cfg=None):
    """Return True only when an outbound SMTP email provider is explicitly configured."""
    config_obj = cfg or (current_app.config if current_app else {})
    provider = str(config_obj.get('EMAIL_PROVIDER', 'none')).lower()
    if provider != 'smtp':
        return False
    return bool(config_obj.get('SMTP_HOST') and config_obj.get('MAIL_FROM_ADDRESS'))


def send_password_reset_email(recipient_email, reset_url):
    """
    Dispatch a password reset email via configured SMTP provider.
    Fails closed when no email provider is configured and never logs reset tokens or URLs.
    """
    if not is_email_configured():
        logger.warning("Password reset delivery aborted: outbound email provider is not configured (fail-closed).")
        return False

    cfg = current_app.config
    host = cfg.get('SMTP_HOST')
    port = int(cfg.get('SMTP_PORT', 587))
    username = cfg.get('SMTP_USERNAME')
    password = cfg.get('SMTP_PASSWORD')
    use_tls = bool(cfg.get('SMTP_USE_TLS', True))
    from_addr = cfg.get('MAIL_FROM_ADDRESS')

    msg = EmailMessage()
    msg['Subject'] = 'SafeStop Password Reset Request'
    msg['From'] = from_addr
    msg['To'] = recipient_email
    msg.set_content(
        "A password reset was requested for your SafeStop account.\n\n"
        f"Use the following link within 30 minutes to reset your password:\n{reset_url}\n\n"
        "If you did not request this reset, you can safely ignore this message."
    )

    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            if use_tls:
                server.starttls()
            if username and password:
                server.login(username, password)
            server.send_message(msg)
        logger.info("Password reset email dispatched via SMTP.")
        return True
    except Exception as exc:
        # Log exception type only to avoid inadvertently leaking sensitive payloads
        logger.error(f"Password reset email delivery failed ({type(exc).__name__}).")
        return False
