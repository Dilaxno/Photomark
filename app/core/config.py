import os
import logging
from dotenv import load_dotenv
from botocore.client import Config as BotoConfig
import boto3

# Load .env from project root
try:
    load_dotenv(dotenv_path=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".env")))
except Exception:
    try:
        load_dotenv()
    except Exception:
        pass

# Environment
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.getenv("R2_BUCKET", "")
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL", "")
MAX_FILES = int(os.getenv("MAX_FILES", "100"))

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

MAIL_FROM = os.getenv("MAIL_FROM", "Your App <no-reply@your-domain.com>")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

NEW_DEVICE_ALERT_COOLDOWN_SEC = int(os.getenv("NEW_DEVICE_ALERT_COOLDOWN_SEC", "7200"))
GEOIP_LOOKUP_URL = os.getenv("GEOIP_LOOKUP_URL", "")

ADMIN_ALLOWLIST_IPS = [ip.strip() for ip in (os.getenv("ADMIN_ALLOWLIST_IPS", "").split(",") if os.getenv("ADMIN_ALLOWLIST_IPS") else []) if ip.strip()]
ADMIN_EMAILS = [e.strip().lower() for e in (os.getenv("ADMIN_EMAILS", "").split(",") if os.getenv("ADMIN_EMAILS") else []) if e.strip()]

# Collaborator auth config
COLLAB_JWT_SECRET = os.getenv("COLLAB_JWT_SECRET", "").strip()
COLLAB_JWT_TTL_DAYS = int(os.getenv("COLLAB_JWT_TTL_DAYS", "30"))

# Collaboration send limits and validation
COLLAB_MAX_IMAGE_MB = int(os.getenv("COLLAB_MAX_IMAGE_MB", "25"))
COLLAB_ALLOWED_EXTS = [e.strip().lower() for e in (os.getenv("COLLAB_ALLOWED_EXTS", ".jpg,.jpeg,.png,.webp,.heic,.tif,.tiff").split(",") if os.getenv("COLLAB_ALLOWED_EXTS") else [".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff"]) if e.strip()]
COLLAB_RATE_LIMIT_WINDOW_SEC = int(os.getenv("COLLAB_RATE_LIMIT_WINDOW_SEC", "3600"))
COLLAB_RATE_LIMIT_MAX_ACTIONS = int(os.getenv("COLLAB_RATE_LIMIT_MAX_ACTIONS", "200"))  # actions ~ images sent
COLLAB_MAX_RECIPIENTS = int(os.getenv("COLLAB_MAX_RECIPIENTS", "10"))

# RapidAPI Camera DB
RAPIDAPI_CAMERA_DB_KEY = os.getenv("RAPIDAPI_CAMERA_DB_KEY", "").strip()
RAPIDAPI_CAMERA_DB_HOST = os.getenv("RAPIDAPI_CAMERA_DB_HOST", "camera-database.p.rapidapi.com").strip()
RAPIDAPI_CAMERA_DB_BASE = os.getenv("RAPIDAPI_CAMERA_DB_BASE", f"https://{RAPIDAPI_CAMERA_DB_HOST}").strip()

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("photomark")

# Static dir helper
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
STATIC_DIR = os.path.abspath(STATIC_DIR)

# S3/R2 client
s3 = None
if R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY:
    s3 = boto3.resource(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )