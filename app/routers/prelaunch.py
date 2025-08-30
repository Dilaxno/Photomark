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
    folder_id = os.getenv("PRELAUNCH_DRIVE_FOLDER_ID", "").strip()

    # Collect useful context
    ts = datetime.now(timezone.utc).isoformat()
    email = payload.email.strip().lower()
    name = (payload.name or "").strip()
    ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else None)
    ua = request.headers.get("user-agent", "")

    # If Drive folder is configured, save to CSV in that folder; otherwise fall back to Sheets (if configured)
    if folder_id:
        try:
            from google.oauth2.service_account import Credentials
            from google.auth.transport.requests import AuthorizedSession
            import json, base64, io, csv

            scopes = ["https://www.googleapis.com/auth/drive"]

            # Load service account credentials from env (inline, base64, or file path)
            creds = None
            json_inline = (
                os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON")
                or os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
                or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
            )
            b64_creds = os.getenv("GSPREAD_SERVICE_ACCOUNT_BASE64")
            json_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_PATH") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

            if json_inline:
                info = json.loads(json_inline)
                creds = Credentials.from_service_account_info(info, scopes=scopes)
            elif b64_creds:
                raw = base64.b64decode(b64_creds)
                info = json.loads(raw)
                creds = Credentials.from_service_account_info(info, scopes=scopes)
            elif json_path and os.path.isfile(json_path):
                creds = Credentials.from_service_account_file(json_path, scopes=scopes)
            else:
                raise RuntimeError("Service account credentials not configured for Drive access")

            session = AuthorizedSession(creds)

            # Find existing CSV in the target folder
            files_url = "https://www.googleapis.com/drive/v3/files"
            query = f"'{folder_id}' in parents and trashed = false and name = 'prelaunch_subscribers.csv'"
            params = {
                "q": query,
                "fields": "files(id,name)",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
                "pageSize": 1,
            }
            r = session.get(files_url, params=params)
            if r.status_code != 200:
                logger.error(f"Drive list error: {r.status_code} {r.text}")
                return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)
            items = r.json().get("files", [])
            file_id = items[0]["id"] if items else None

            # Prepare CSV row
            buf = io.StringIO(newline="")
            writer = csv.writer(buf)
            writer.writerow([ts, email, name, ip or "", ua])
            new_row = buf.getvalue()

            if not file_id:
                # Create new CSV with header and first row
                content = "timestamp,email,name,ip,user_agent\n" + new_row
                meta = {"name": "prelaunch_subscribers.csv", "parents": [folder_id], "mimeType": "text/csv"}
                create_r = session.post(
                    "https://www.googleapis.com/drive/v3/files",
                    json=meta,
                    params={"supportsAllDrives": "true"},
                )
                if create_r.status_code not in (200, 201):
                    logger.error(f"Drive create metadata error: {create_r.status_code} {create_r.text}")
                    return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)
                file_id = create_r.json().get("id")
                if not file_id:
                    logger.error("Drive create did not return file id")
                    return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)
                upload_url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}"
                upload_r = session.patch(
                    upload_url,
                    params={"uploadType": "media", "supportsAllDrives": "true"},
                    headers={"Content-Type": "text/csv"},
                    data=content.encode("utf-8"),
                )
                if upload_r.status_code not in (200, 201):
                    logger.error(f"Drive upload error: {upload_r.status_code} {upload_r.text}")
                    return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)
                return {"ok": True}
            else:
                # Download, append row, and update
                download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
                get_r = session.get(download_url, params={"alt": "media", "supportsAllDrives": "true"})
                if get_r.status_code != 200:
                    logger.error(f"Drive download error: {get_r.status_code} {get_r.text}")
                    return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)
                current = get_r.content.decode("utf-8", errors="replace")
                if not current.endswith("\n"):
                    current += "\n"
                updated = current + new_row
                upload_url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}"
                put_r = session.patch(
                    upload_url,
                    params={"uploadType": "media", "supportsAllDrives": "true"},
                    headers={"Content-Type": "text/csv"},
                    data=updated.encode("utf-8"),
                )
                if put_r.status_code not in (200, 201):
                    logger.error(f"Drive update error: {put_r.status_code} {put_r.text}")
                    return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)
                return {"ok": True}
        except Exception:
            logger.exception("prelaunch subscribe failed (drive)")
            return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)

    # Fallback to Google Sheets if Drive folder not configured
    sheet_id = os.getenv("PRELAUNCH_SHEET_ID")
    sheet_tab = os.getenv("PRELAUNCH_SHEET_TAB", "Prelaunch")
    if not sheet_id:
        return JSONResponse({"ok": False, "error": "Server not configured"}, status_code=500)

    try:
        gc = _get_gspread_client()
        sh = gc.open_by_key(sheet_id)
        try:
            ws = sh.worksheet(sheet_tab)
        except Exception:
            ws = sh.sheet1
        ws.append_row([ts, email, name, ip or "", ua], value_input_option="RAW", insert_data_option="INSERT_ROWS")
        return {"ok": True}
    except Exception:
        logger.exception("prelaunch subscribe failed (sheets)")
        return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)