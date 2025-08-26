from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from app.core.auth import firebase_enabled, fb_auth  # type: ignore
from app.core.config import logger

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
        return {"ok": True, "uid": uid}
    except Exception as ex:
        logger.warning(f"register-signup failed for {email}: {ex}")
        # Firebase Admin raises various exceptions (email exists, invalid email, etc.)
        return JSONResponse({"error": str(ex)}, status_code=400)