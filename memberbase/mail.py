import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

log = logging.getLogger(__name__)


def send(to: str, subject: str, body: str) -> bool:
    """Send a plain-text notification. Failures are logged, not raised: the
    change it reports has already happened."""
    cfg = current_app.config
    if not cfg["SMTP_HOST"]:
        log.warning("SMTP_HOST not set, notification not sent")
        return False
    msg = EmailMessage()
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=10) as smtp:
            if cfg["SMTP_STARTTLS"]:
                smtp.starttls()
            if cfg["SMTP_USER"]:
                smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("notification not sent: %s", type(exc).__name__)
        return False
    return True
