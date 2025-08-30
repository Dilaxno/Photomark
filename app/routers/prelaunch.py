from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
import os
from datetime import datetime, timezone

from app.core.config import logger

# Lazy load gspread to keep import time light
_gspread_client = None


def _get_gspread_client():
    """Create and cache a gspread client using flexible credential sources.

    Supports:
    - Inline JSON in env: GSPREAD_SERVICE_ACCOUNT_JSON | FIREBASE_SERVICE_ACCOUNT_JSON | GOOGLE_SERVICE_ACCOUNT_JSON
    - Base64 JSON in env: GSPREAD_SERVICE_ACCOUNT_BASE64
    - File path in env: FIREBASE_SERVICE_ACCOUNT_JSON_PATH | GOOGLE_APPLICATION_CREDENTIALS
    """
    global _gspread_client
    if _gspread_client is not None:
        return _gspread_client

    sheet_scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    try:
        from google.oauth2.service_account import Credentials
        import gspread, json, base64

        # 1) Inline JSON (plain text)
        json_inline = (
            os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON")
            or os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
            or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        )
        if json_inline:
            try:
                info = json.loads(json_inline)
                creds = Credentials.from_service_account_info(info, scopes=sheet_scopes)
                _gspread_client = gspread.authorize(creds)
                return _gspread_client
            except Exception:
                logger.exception("Failed parsing inline service account JSON from env")
                raise

        # 2) Base64-encoded JSON
        b64_creds = os.getenv("GSPREAD_SERVICE_ACCOUNT_BASE64")
        if b64_creds:
            try:
                raw = base64.b64decode(b64_creds)
                info = json.loads(raw)
                creds = Credentials.from_service_account_info(info, scopes=sheet_scopes)
                _gspread_client = gspread.authorize(creds)
                return _gspread_client
            except Exception:
                logger.exception("Failed decoding/parsing base64 service account JSON from env")
                raise

        # 3) File path
        json_path = (
            os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_PATH")
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        )
        if json_path and os.path.isfile(json_path):
            creds = Credentials.from_service_account_file(json_path, scopes=sheet_scopes)
            _gspread_client = gspread.authorize(creds)
            return _gspread_client

        raise RuntimeError(
            "Service account credentials not found. Provide inline JSON (GSPREAD_SERVICE_ACCOUNT_JSON | FIREBASE_SERVICE_ACCOUNT_JSON), "
            "base64 (GSPREAD_SERVICE_ACCOUNT_BASE64), or a valid file path (FIREBASE_SERVICE_ACCOUNT_JSON_PATH | GOOGLE_APPLICATION_CREDENTIALS)."
        )
    except Exception:
        logger.exception("Failed to init gspread client")
        raise


class SubscribePayload(BaseModel):
    email: EmailStr
    name: str | None = None


router = APIRouter(prefix="/prelaunch", tags=["prelaunch"])  # e.g. POST /prelaunch/subscribe


@router.post("/subscribe")
async def subscribe(payload: SubscribePayload, request: Request):
    sheet_id = os.getenv("PRELAUNCH_SHEET_ID")
    sheet_tab = os.getenv("PRELAUNCH_SHEET_TAB", "Prelaunch")
    if not sheet_id:
        return JSONResponse({"ok": False, "error": "Server not configured"}, status_code=500)

    # Collect useful context
    ts = datetime.now(timezone.utc).isoformat()
    email = payload.email.strip().lower()
    name = (payload.name or "").strip()
    ip = request.headers.get("x-forwarded-for") or request.client.host if request.client else None
    ua = request.headers.get("user-agent", "")

    try:
        gc = _get_gspread_client()
        sh = gc.open_by_key(sheet_id)
        # Prefer a specific tab; fall back to first sheet if it doesn't exist
        try:
            ws = sh.worksheet(sheet_tab)
        except Exception:
            ws = sh.sheet1
        ws.append_row([ts, email, name, ip or "", ua], value_input_option="RAW", insert_data_option="INSERT_ROWS")
        return {"ok": True}
    except Exception:
        logger.exception("prelaunch subscribe failed")
        return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)