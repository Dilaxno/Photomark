from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
import os
import secrets
from datetime import datetime, timedelta

from app.core.auth import get_uid_from_request, firebase_enabled, fb_auth  # type: ignore
from app.core.config import logger
from app.utils.emailing import render_email, send_email_smtp
from app.utils.storage import write_json_key, read_json_key

router = APIRouter(prefix="/api", tags=["auth"])


# ---- Helpers (key structures) ----

def _pw_reset_key(token: str) -> str:
    return f"auth/password_resets/{token}.json"


def _user_meta_key(uid: str) -> str:
    return f"users/{uid}/meta.json"


def _email_verification_key(token: str) -> str:
    return f"auth/email_verifications/{token}.json"


# ---- Password reset flow ----

@router.post("/auth/password/reset")
async def auth_password_reset(request: Request, payload: dict = Body(...)):
    email = (payload or {}).get("email", "").strip()
    if not email:
        return JSONResponse({"error": "email required"}, status_code=400)
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password reset unavailable"}, status_code=500)

    try:
        # Resolve user uid from email
        user = fb_auth.get_user_by_email(email)
        uid = getattr(user, "uid", None)
        if not uid:
            return JSONResponse({"error": "account not found"}, status_code=404)

        # Generate token and persist (1 hour)
        token = secrets.token_urlsafe(32)
        now = datetime.utcnow()
        exp = now + timedelta(hours=1)
        rec = {
            "token": token,
            "uid": uid,
            "email": email,
            "created_at": now.isoformat(),
            "expires_at": exp.isoformat(),
            "used": False,
        }
        write_json_key(_pw_reset_key(token), rec)

        # Build link to frontend handler
        origin = request.headers.get("origin") or ""
        link = (origin.rstrip("/") + f"/#newpassword?token={token}") if origin else f"/#newpassword?token={token}"

        subject = "Reset your password"
        html = render_email(
            "email_basic.html",
            title="Reset your password",
            intro="Click the button below to reset your password.",
            button_label="Reset password",
            button_url=link,
            footer_note="If you did not request this, you can ignore this email.",
        )
        text = f"Open this link to reset your password: {link}"

        sent = send_email_smtp(email, subject, html, text)
        if not sent:
            return JSONResponse({"error": "Failed to send email"}, status_code=500)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Password reset init failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/auth/password/validate")
async def auth_password_validate(token: str):
    if not token or len(token) < 10:
        return JSONResponse({"error": "invalid token"}, status_code=400)
    rec = read_json_key(_pw_reset_key(token))
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        exp = datetime.fromisoformat(str(rec.get("expires_at", "")))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)
    if rec.get("used"):
        return JSONResponse({"error": "consumed"}, status_code=410)
    return {"email": rec.get("email") or ""}


@router.post("/auth/password/confirm")
async def auth_password_confirm(payload: dict = Body(...)):
    token = str((payload or {}).get("token") or "").strip()
    password = str((payload or {}).get("password") or "").strip()
    if not token or not password:
        return JSONResponse({"error": "token and password required"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"error": "password too short"}, status_code=400)
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password reset unavailable"}, status_code=500)

    rec = read_json_key(_pw_reset_key(token))
    if not rec:
        return JSONResponse({"error": "invalid token"}, status_code=400)
    try:
        exp = datetime.fromisoformat(str(rec.get("expires_at", "")))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)
    if rec.get("used"):
        return JSONResponse({"error": "consumed"}, status_code=410)

    uid = rec.get("uid") or ""
    try:
        fb_auth.update_user(uid, password=password)
        rec["used"] = True
        write_json_key(_pw_reset_key(token), rec)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Password update failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


# ---- Welcome email ----

@router.post("/email/welcome")
async def email_welcome(request: Request):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "Email not available"}, status_code=500)
    try:
        user = fb_auth.get_user(uid)
        email = getattr(user, "email", None)
        if not email:
            return {"ok": False}
        meta = read_json_key(_user_meta_key(uid)) or {}
        if meta.get("welcome_sent"):
            return {"ok": True}
        app_name = os.getenv("APP_NAME", "Photomark")
        subject = f"Welcome to {app_name}"
        link = (os.getenv('FRONTEND_ORIGIN', '').split(',')[0].rstrip('/') or '') + '#software'
        html = render_email(
            "email_basic.html",
            title=f"Welcome to {app_name}",
            intro=f"Hi {getattr(user, 'display_name', '') or ''},<br>Welcome! You're all set to watermark, convert, and style your photos.",
            button_label="Get started",
            button_url=link,
            footer_note="Happy creating!",
        )
        text = f"Welcome to {app_name}! Get started by uploading your first photos."
        send_email_smtp(email, subject, html, text)
        meta["welcome_sent"] = True
        write_json_key(_user_meta_key(uid), meta)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Welcome email failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


# ---- Email verification ----

@router.post("/auth/email/verification/send")
async def auth_email_verification_send(request: Request):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "Email verification unavailable"}, status_code=500)
    try:
        user = fb_auth.get_user(uid)
        email = getattr(user, "email", None)
        if not email:
            return JSONResponse({"error": "No email on account"}, status_code=400)
        if getattr(user, "email_verified", False):
            return {"ok": True, "already_verified": True}

        # Throttle using user meta
        meta = read_json_key(_user_meta_key(uid)) or {}
        now = datetime.utcnow()
        last_sent_iso = meta.get("verify_sent_at")
        if last_sent_iso:
            try:
                last = datetime.fromisoformat(str(last_sent_iso))
                if (now - last).total_seconds() < 60:
                    return {"ok": True, "throttled": True}
            except Exception:
                pass

        token = secrets.token_urlsafe(32)
        exp = now + timedelta(hours=48)
        rec = {
            "token": token,
            "uid": uid,
            "email": email,
            "created_at": now.isoformat(),
            "expires_at": exp.isoformat(),
            "used": False,
        }
        write_json_key(_email_verification_key(token), rec)

        scheme = request.url.scheme or "https"
        host = request.headers.get("host") or ""
        base = f"{scheme}://{host}".rstrip("/") if host else ""
        link = f"{base}/api/auth/email/verification/confirm?token={token}" if base else f"/api/auth/email/verification/confirm?token={token}"

        subject = "Verify your email"
        html = render_email(
            "email_basic.html",
            title="Verify your email",
            intro="Please verify your email address for your account.",
            button_label="Verify email",
            button_url=link,
            footer_note="If you did not create this account, you can ignore this email.",
        )
        text = f"Verify your email by opening this link: {link}"

        sent = send_email_smtp(email, subject, html, text)
        if not sent:
            return JSONResponse({"error": "Failed to send email"}, status_code=500)

        meta["verify_sent_at"] = now.isoformat()
        write_json_key(_user_meta_key(uid), meta)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Email verification send failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/auth/email/verification/confirm")
async def auth_email_verification_confirm(token: str, request: Request):
    if not token or len(token) < 10:
        return JSONResponse({"error": "invalid token"}, status_code=400)
    rec = read_json_key(_email_verification_key(token))
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        exp = datetime.fromisoformat(str(rec.get("expires_at", "")))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    uid = rec.get("uid") or ""
    try:
        # Mark verified in Firebase
        if firebase_enabled and fb_auth:
            fb_auth.update_user(uid, email_verified=True)
        rec["used"] = True
        write_json_key(_email_verification_key(token), rec)
    except Exception as ex:
        logger.exception(f"Set email verified failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)

    # Mint a Firebase custom token to allow auto-login on frontend
    custom_jwt = None
    try:
        if firebase_enabled and fb_auth:
            ct_bytes = fb_auth.create_custom_token(uid)
            custom_jwt = ct_bytes.decode("utf-8") if isinstance(ct_bytes, (bytes, bytearray)) else str(ct_bytes)
    except Exception as ex:
        logger.warning(f"Custom token creation failed: {ex}")

    fe = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "").rstrip("/")
    if fe and custom_jwt:
        return RedirectResponse(url=f"{fe}/#verify-success?ct={custom_jwt}", status_code=302)

    # Fallback success page
    body = "<h1>Email verified</h1><p>Your email has been verified successfully.</p>"
    if fe:
        body += f"<p><a href=\"{fe}\">Continue to app</a></p>"
    html_page = f"<!doctype html><html><head><meta charset='utf-8'><title>Verified</title></head><body>{body}</body></html>"
    return HTMLResponse(content=html_page, status_code=200)