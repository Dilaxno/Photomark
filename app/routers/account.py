from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from typing import Optional
from datetime import datetime, timedelta
import os
import secrets

from app.core.auth import get_uid_from_request, firebase_enabled, fb_auth  # type: ignore
from app.core.config import logger
from app.utils.storage import write_json_key, read_json_key
from app.utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api/account", tags=["account"]) 


def _email_change_key(uid: str) -> str:
    return f"auth/email_change/{uid}.json"


@router.post("/email/change/init")
async def email_change_init(request: Request, payload: dict = Body(...)):
    """
    Immediately update the authenticated user's email in Firebase.
    Body: { "new_email": str }
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    new_email = str((payload or {}).get("new_email") or "").strip()
    if not new_email or "@" not in new_email:
        return JSONResponse({"error": "valid new_email required"}, status_code=400)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "email change unavailable"}, status_code=500)

    try:
        fb_auth.update_user(uid, email=new_email, email_verified=False)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"email change init (immediate) failed for {uid}: {ex}")
        # Map duplicate email to a user-friendly message
        msg = (getattr(ex, "message", None) or str(ex) or "").lower()
        if any(s in msg for s in ("email already exists", "email-already-in-use", "email already in use", "email_exists", "email exists")):
            return JSONResponse({"error": "This email is already used by another account"}, status_code=400)
        return JSONResponse({"error": "Failed to update email"}, status_code=400)


@router.post("/email/change/confirm")
async def email_change_confirm(request: Request, payload: dict = Body(...)):
    """
    Immediately update the authenticated user's email in Firebase (compat endpoint).
    Body: { "new_email": str, "code"?: str }
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    new_email = str((payload or {}).get("new_email") or "").strip()
    if not new_email or "@" not in new_email:
        return JSONResponse({"error": "valid new_email required"}, status_code=400)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "email change unavailable"}, status_code=500)

    try:
        fb_auth.update_user(uid, email=new_email, email_verified=False)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"email change confirm (immediate) failed for {uid}: {ex}")
        # Map duplicate email to a user-friendly message
        msg = (getattr(ex, "message", None) or str(ex) or "").lower()
        if any(s in msg for s in ("email already exists", "email-already-in-use", "email already in use", "email_exists", "email exists")):
            return JSONResponse({"error": "This email is already used by another account"}, status_code=400)
        return JSONResponse({"error": "Failed to update email"}, status_code=400)