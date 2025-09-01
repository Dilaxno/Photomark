from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from app.core.auth import firebase_enabled, fb_auth, get_uid_from_request  # type: ignore
from app.core.config import logger
from datetime import datetime
try:
    from firebase_admin import firestore as fb_fs  # type: ignore
except Exception:
    fb_fs = None  # type: ignore
from app.core.auth import get_fs_client

router = APIRouter(prefix="/api/auth/ip", tags=["auth-ip"]) 


@router.post("/register-signup")
async def register_signup(payload: dict = Body(...)):
    """
    Create a new user via Firebase Admin.
    Expected JSON body: { "email": str, "password": str, "display_name"?: str }
    Returns: { ok: true, uid: str } on success
    """
    email = str((payload or {}).get("email") or "").strip()
    password = str((payload or {}).get("password") or "").strip()
    display_name = str((payload or {}).get("display_name") or "").strip() or None

    if not email or "@" not in email:
        return JSONResponse({"error": "valid email required"}, status_code=400)
    if not password or len(password) < 6:
        return JSONResponse({"error": "password must be at least 6 characters"}, status_code=400)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "auth unavailable"}, status_code=500)

    try:
        user = fb_auth.create_user(email=email, password=password, display_name=display_name)
        uid = getattr(user, "uid", None)
        # Create/update Firestore user profile
        try:
            if firebase_enabled and fb_fs and uid:
                _fs = fb_fs.client()
                if _fs:
                    _fs.collection('users').document(uid).set({
                        'uid': uid,
                        'email': email.lower(),
                        'name': (display_name or '').strip(),
                        'plan': 'free',
                        'createdAt': fb_fs.SERVER_TIMESTAMP,
                        'updatedAt': fb_fs.SERVER_TIMESTAMP,
                        'lastLogin': None,
                    }, merge=True)
        except Exception:
            pass
        return {"ok": True, "uid": uid}
    except Exception as ex:
        logger.warning(f"register-signup failed for {email}: {ex}")
        # Map duplicate email to a user-friendly message
        msg = (getattr(ex, "message", None) or str(ex) or "").lower()
        if any(s in msg for s in ("email already exists", "email-already-in-use", "email already in use", "email_exists", "email exists")):
            return JSONResponse({"error": "This email is already used by another account"}, status_code=400)
        return JSONResponse({"error": "Failed to create account"}, status_code=400)


@router.post("/last-login")
async def last_login(request: Request):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        email = None
        if firebase_enabled and fb_auth:
            try:
                user = fb_auth.get_user(uid)
                email = (getattr(user, "email", None) or "").lower()
            except Exception:
                pass
        if firebase_enabled and fb_fs:
            _fs = fb_fs.client()
            if _fs:
                doc = {
                    'uid': uid,
                    'updatedAt': fb_fs.SERVER_TIMESTAMP,
                    'lastLogin': fb_fs.SERVER_TIMESTAMP,
                }
                if email:
                    doc['email'] = email
                _fs.collection('users').document(uid).set(doc, merge=True)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"last-login failed for {uid}: {ex}")
        return {"ok": False}