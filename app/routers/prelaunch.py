from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
import os
from datetime import datetime, timezone

from app.core.config import logger


def _get_firestore():
    """Init and return Firestore client (firebase_admin), using flexible credential sources."""
    import json, base64
    import firebase_admin
    from firebase_admin import credentials, firestore

    # Reuse existing app if already initialized
    try:
        firebase_admin.get_app()
    except ValueError:
        cred = None
        json_inline = (
            os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
            or os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON")
            or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        )
        b64_creds = os.getenv("GSPREAD_SERVICE_ACCOUNT_BASE64")
        json_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

        if json_inline:
            info = json.loads(json_inline)
            cred = credentials.Certificate(info)
        elif b64_creds:
            raw = base64.b64decode(b64_creds)
            info = json.loads(raw)
            cred = credentials.Certificate(info)
        elif json_path and os.path.isfile(json_path):
            cred = credentials.Certificate(json_path)
        else:
            raise RuntimeError("Service account credentials not configured for Firebase")

        firebase_admin.initialize_app(cred)

    return firestore.client()


class SubscribePayload(BaseModel):
    email: EmailStr
    name: str | None = None


router = APIRouter(prefix="/prelaunch", tags=["prelaunch"])  # e.g. POST /prelaunch/subscribe


@router.post("/subscribe")
async def subscribe(payload: SubscribePayload, request: Request):
    # Collect useful context
    ts = datetime.now(timezone.utc).isoformat()
    email = payload.email.strip().lower()
    name = (payload.name or "").strip()
    ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else None)
    ua = request.headers.get("user-agent", "")

    try:
        db = _get_firestore()
        doc = {
            "timestamp": ts,
            "email": email,
            "name": name,
            "ip": ip or "",
            "user_agent": ua,
        }
        # Use email as doc id to dedupe; last write wins
        db.collection("pre-launchers").document(email).set(doc)
        return {"ok": True}
    except Exception:
        logger.exception("prelaunch subscribe failed (firestore)")
        return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)