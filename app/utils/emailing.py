import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from jinja2 import Environment, FileSystemLoader, select_autoescape
import os

from app.core.config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, logger

# Jinja env
_templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
_jinja_env = Environment(
    loader=FileSystemLoader(_templates_dir),
    autoescape=select_autoescape(["html", "xml"]),
)

EMAIL_BRAND_BUTTON_BG = os.getenv("EMAIL_BRAND_BUTTON_BG", "#7AA2F7")
EMAIL_BRAND_BUTTON_TEXT = os.getenv("EMAIL_BRAND_BUTTON_TEXT", "#000000")
EMAIL_BRAND_BG = os.getenv("EMAIL_BRAND_BG", "#0F1115")
APP_NAME = os.getenv("APP_NAME", "Photomark")
_front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "").rstrip("/")
EMAIL_LOGO_URL = os.getenv("EMAIL_LOGO_URL", (_front + "/marklogo.svg") if _front else "")


def render_email(template_name: str, **context) -> str:
    base = {
        "app_name": APP_NAME,
        "brand_bg": EMAIL_BRAND_BG,
        "button_bg": EMAIL_BRAND_BUTTON_BG,
        "button_text": EMAIL_BRAND_BUTTON_TEXT,
        "logo_url": EMAIL_LOGO_URL,
    }
    base.update(context or {})
    return _jinja_env.get_template(template_name).render(**base)


def send_email_smtp(to_addr: str, subject: str, html: str, text: Optional[str] = None) -> bool:
    try:
        if not SMTP_HOST or not SMTP_PASS or not MAIL_FROM:
            logger.error("SMTP not configured; cannot send email")
            return False
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = MAIL_FROM
        msg["To"] = to_addr
        if not text:
            text = "Open this link in an HTML-capable email client."
        msg.attach(MIMEText(text, "plain", _charset="utf-8"))
        msg.attach(MIMEText(html, "html", _charset="utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            if SMTP_USER or SMTP_PASS:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(MAIL_FROM, [to_addr], msg.as_string())
        return True
    except Exception as ex:
        logger.exception(f"SMTP send failed: {ex}")
        return False