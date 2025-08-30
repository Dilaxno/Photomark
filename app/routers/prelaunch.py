from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
import os
from datetime import datetime, timezone

from app.core.config import logger

# Lazy load gspread to keep import time light
_gspread_client = None


def _get_gspread_client():
    global _gspread_client
    if _gspread_client is not None:
        return _gspread_client

    sheet_scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    # Use the same service account JSON as Firebase Admin (already present in .env)
    json_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_PATH")
    if not json_path or not os.path.isfile(json_path):
        raise RuntimeError("Service account JSON not found. Set FIREBASE_SERVICE_ACCOUNT_JSON_PATH")

    try:
        from google.oauth2.service_account import Credentials
        import gspread
        creds = Credentials.from_service_account_file(json_path, scopes=sheet_scopes)
        _gspread_client = gspread.authorize(creds)
        return _gspread_client
    except Exception as ex:
        logger.error(f"Failed to init gspread client: {ex}")
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
    except Exception as ex:
        logger.error(f"prelaunch subscribe failed: {ex}")
        return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)