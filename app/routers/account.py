from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from typing import Optional
from datetime import datetime, timedelta
import os
import secrets
import shutil

from app.core.auth import get_uid_from_request, firebase_enabled, fb_auth  # type: ignore
from app.core.config import logger, STATIC_DIR, s3, R2_BUCKET
from app.utils.storage import write_json_key, read_json_key
from app.utils.emailing import render_email, send_email_smtp

# Optional Firestore client (lazy)
try:
    from firebase_admin import firestore as fb_fs  # type: ignore
    def _get_fs_client():
        try:
            return fb_fs.client()
        except Exception:
            return None
except Exception:
    def _get_fs_client():
        return None

router = APIRouter(prefix="/api/account", tags=["account"]) 

# Create/update Firestore users/{uid} on signup/login
@router.post("/users/sync")
async def users_sync(request: Request):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db = _get_fs_client()
    if db is None:
        return JSONResponse({"error": "Firestore unavailable"}, status_code=500)

    # Gather name/email from Firebase Auth (best-effort)
    name = ""
    email = ""
    if firebase_enabled and fb_auth:
        try:
            user = fb_auth.get_user(uid)
            name = (getattr(user, "display_name", None) or "").strip()
            email = (getattr(user, "email", None) or "").strip()
        except Exception as ex:
            logger.warning(f"users/sync get_user failed for {uid}: {ex}")

    doc_ref = db.collection('users').document(uid)

    try:
        transaction = db.transaction()

        @fb_fs.transactional
        def _apply_txn(txn):
            snap = doc_ref.get(transaction=txn)
            payload = {
                "uid": uid,
                "name": name,
                "email": email,
                "lastLogin": fb_fs.SERVER_TIMESTAMP,
                "updatedAt": fb_fs.SERVER_TIMESTAMP,
            }
            if snap.exists:
                txn.update(doc_ref, payload)
            else:
                payload["plan"] = "free"  # default on create
                payload["createdAt"] = fb_fs.SERVER_TIMESTAMP
                txn.set(doc_ref, payload)

        _apply_txn(transaction)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"users/sync failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to sync user profile"}, status_code=500)


def _entitlement_key(uid: str) -> str:
    return f"users/{uid}/billing/entitlement.json"


@router.get("/entitlement")
async def get_entitlement(request: Request):
    """Return whether the current user has an active paid entitlement.
    Anonymous users get { isPaid: false }.
    """
    uid = get_uid_from_request(request)
    if not uid:
        # Anonymous device gating is handled client-side
        return {"isPaid": False}
    try:
        rec = read_json_key(_entitlement_key(uid)) or {}
        is_paid = bool(rec.get("isPaid") or False)
        # Fallback to Firestore mirror if available
        if not is_paid:
            try:
                db = _get_fs_client()
                if db is not None:
                    snap = db.collection('users').document(uid).get()
                    if snap.exists:
                        data = snap.to_dict() or {}
                        is_paid = bool(data.get('isPaid') or False)
            except Exception:
                pass
        return {"isPaid": bool(is_paid)}
    except Exception as ex:
        logger.warning(f"entitlement check failed for {uid}: {ex}")
        return {"isPaid": False}


def _email_change_key(uid: str) -> str:
    return f"auth/email_change/{uid}.json"


@router.post("/email/change/init")
async def email_change_init(request: Request, payload: dict = Body(...)):
    """
    Start email change with OTP verification. Do NOT change the email yet.
    Body: { "new_email": str }
    Behavior:
      - Generate a 6-digit code
      - Store { new_email, code, expires_at }
      - Send code to the user's CURRENT email via SMTP (Resend-compatible)
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
        # Fetch current email to deliver the code
        user = fb_auth.get_user(uid)
        current_email = (getattr(user, "email", None) or "").strip()
        if not current_email:
            return JSONResponse({"error": "current email unavailable"}, status_code=400)

        # Prepare OTP payload
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.utcnow()
        rec = {
            "new_email": new_email,
            "code": code,
            "sent_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=15)).isoformat(),
            "attempts": 0,
        }
        write_json_key(_email_change_key(uid), rec)

        # Compose email (Resend SMTP works via SMTP_* env vars)
        subject = "Verify your email change"
        intro = (
            "We received a request to change the email on your account. "
            f"Use this verification code to confirm: <b>{code}</b><br><br>"
            "This code expires in 15 minutes. If you didn't request this, you can ignore this email."
        )
        html = render_email(
            "email_basic.html",
            title="Confirm your email change",
            intro=intro,
            footer_note=f"Request time (UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        ok = send_email_smtp(current_email, subject, html)
        if not ok:
            return JSONResponse({"error": "failed to send verification email"}, status_code=500)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"email change init failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to start email change"}, status_code=400)


@router.post("/email/change/confirm")
async def email_change_confirm(request: Request, payload: dict = Body(...)):
    """
    Confirm email change with the OTP code and then update Firebase email.
    Body: { "code": str }
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    code = str((payload or {}).get("code") or "").strip()
    if not code:
        return JSONResponse({"error": "verification code required"}, status_code=400)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "email change unavailable"}, status_code=500)

    try:
        rec = read_json_key(_email_change_key(uid)) or {}
        target_email = str(rec.get("new_email") or "").strip()
        saved_code = str(rec.get("code") or "").strip()
        attempts = int(rec.get("attempts") or 0)
        exp_str = rec.get("expires_at")

        if not target_email or not saved_code or not exp_str:
            return JSONResponse({"error": "no pending email change"}, status_code=400)

        # Expiry check
        try:
            exp = datetime.fromisoformat(exp_str)
        except Exception:
            exp = datetime.utcnow() - timedelta(seconds=1)
        if datetime.utcnow() > exp:
            write_json_key(_email_change_key(uid), {})
            return JSONResponse({"error": "verification code expired"}, status_code=400)

        # Code check
        if code != saved_code:
            attempts += 1
            rec["attempts"] = attempts
            # Optionally lock after too many attempts
            if attempts >= 5:
                write_json_key(_email_change_key(uid), {})
                return JSONResponse({"error": "too many invalid attempts"}, status_code=429)
            write_json_key(_email_change_key(uid), rec)
            return JSONResponse({"error": "invalid verification code"}, status_code=400)

        # Update email now that the code is verified
        try:
            fb_auth.update_user(uid, email=target_email, email_verified=False)
        except Exception as ex:
            logger.warning(f"email change confirm failed for {uid}: {ex}")
            msg = (getattr(ex, "message", None) or str(ex) or "").lower()
            if any(s in msg for s in ("email already exists", "email-already-in-use", "email already in use", "email_exists", "email exists")):
                return JSONResponse({"error": "This email is already used by another account"}, status_code=400)
            return JSONResponse({"error": "Failed to update email"}, status_code=400)

        # Clear pending request
        write_json_key(_email_change_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"email change confirm (otp) error for {uid}: {ex}")
        return JSONResponse({"error": "Failed to confirm email change"}, status_code=400)


@router.post("/delete")
async def delete_account(request: Request):
    """
    Delete the authenticated user's data and account, then sign them out client-side.
    - Deletes Firestore doc(s) owned by user when available (affiliate_profiles/{uid})
    - Deletes static files under users/{uid} (local or R2)
    - Deletes Firebase Auth user
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "account deletion unavailable"}, status_code=500)

    # 1) Firestore cleanup (best-effort)
    try:
        db = _get_fs_client()
        if db is not None:
            db.collection('affiliate_profiles').document(uid).delete()
    except Exception as ex:
        logger.warning(f"delete_account: firestore cleanup failed for {uid}: {ex}")

    # 2) Static files cleanup (local)
    try:
        user_dir = os.path.join(STATIC_DIR, 'users', uid)
        if os.path.isdir(user_dir):
            shutil.rmtree(user_dir, ignore_errors=True)
    except Exception as ex:
        logger.warning(f"delete_account: local static cleanup failed for {uid}: {ex}")

    # 3) R2/S3 cleanup (best-effort, delete all objects with prefix users/{uid}/)
    try:
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            bucket.objects.filter(Prefix=f"users/{uid}/").delete()
    except Exception as ex:
        logger.warning(f"delete_account: R2 cleanup failed for {uid}: {ex}")

    # 4) Delete Auth user
    try:
        fb_auth.delete_user(uid)
    except Exception as ex:
        logger.warning(f"delete_account: failed to delete auth user {uid}: {ex}")
        return JSONResponse({"error": "Failed to delete account"}, status_code=400)

    return {"ok": True}
