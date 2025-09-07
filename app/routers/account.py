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

# Firestore client via centralized helper
try:
    from firebase_admin import firestore as fb_fs  # type: ignore
except Exception:
    fb_fs = None  # type: ignore
from app.core.auth import get_fs_client as _get_fs_client

router = APIRouter(prefix="/api/account", tags=["account"]) 

# Create/update Firestore users/{uid} on signup/login
@router.post("/users/sync")
async def users_sync(request: Request, payload: Optional[dict] = Body(default=None)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db = _get_fs_client()
    if db is None:
        return JSONResponse({"error": "Firestore unavailable"}, status_code=500)

    # Gather name/email from client or Firebase Auth (best-effort)
    name_client = str(((payload or {}).get("name") if payload else "") or "").strip()
    email_client = str(((payload or {}).get("email") if payload else "") or "").strip()

    name_auth = ""
    email_auth = ""
    user = None
    if firebase_enabled and fb_auth:
        try:
            user = fb_auth.get_user(uid)
            name_auth = (getattr(user, "display_name", None) or "").strip()
            email_auth = (getattr(user, "email", None) or "").strip()
        except Exception as ex:
            logger.warning(f"users/sync get_user failed for {uid}: {ex}")

    name = name_client or name_auth
    email = email_client or email_auth

    # Derive a fallback name from email for email/password signups
    if not name and email:
        try:
            name = email.split("@")[0]
        except Exception:
            name = ""

    # Backfill Firebase display_name if missing (common for email/password accounts)
    if firebase_enabled and fb_auth and user and name and not name_auth:
        try:
            fb_auth.update_user(uid, display_name=name)
        except Exception as ex:
            logger.debug(f"users/sync update display_name skipped for {uid}: {ex}")

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
    """Return whether the current user has an active paid entitlement and plan.
    Anonymous users get { isPaid: false, plan: "free" }.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return {"isPaid": False, "plan": "free"}
    try:
        plan = "free"
        rec = read_json_key(_entitlement_key(uid)) or {}
        is_paid = bool(rec.get("isPaid") or False)
        plan = str(rec.get("plan") or plan)
        # Fallback to Firestore mirror if available
        try:
            db = _get_fs_client()
            if db is not None and (not is_paid or plan == "free"):
                snap = db.collection('users').document(uid).get()
                if snap.exists:
                    data = snap.to_dict() or {}
                    is_paid = bool(data.get('isPaid') or is_paid)
                    plan = str(data.get('plan') or plan)
        except Exception:
            pass
        return {"isPaid": bool(is_paid), "plan": plan}
    except Exception as ex:
        logger.warning(f"entitlement check failed for {uid}: {ex}")
        return {"isPaid": False, "plan": "free"}


@router.post("/webhooks/dodo")
async def dodo_webhook(request: Request):
    """
    Webhook receiver for Dodo payment events. On payment.succeeded, updates the
    user's Firestore doc with the new plan and sets isPaid = true immediately.

    Security: If env DODO_WEBHOOK_SECRET is set, require matching X-Dodo-Secret header.
    Expected payload shape (flexible):
      {
        "type": "payment.succeeded",
        "uid": "<firebase-uid>",               # optional if provided in metadata
        "plan": "pro",                          # desired plan
        "data": { "object": { "metadata": { "uid": "...", "plan": "pro" }, "email": "..." } }
      }
    """
    # Verify shared secret if configured
    try:
        secret_expected = os.environ.get("DODO_WEBHOOK_SECRET", "")
        secret_provided = request.headers.get("X-Dodo-Secret") or request.headers.get("x-dodo-secret") or ""
        if secret_expected and secret_provided != secret_expected:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    except Exception:
        # Do not block webhook if header parsing fails unexpectedly
        pass

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    evt_type = str((payload.get("type") or payload.get("event") or "")).lower()

    if evt_type != "payment.succeeded":
        # Acknowledge other events without action
        return {"ok": True}

    # Extract uid robustly from common locations
    def _dig(dct, *keys, default=None):
        cur = dct
        try:
            for k in keys:
                if cur is None:
                    return default
                cur = cur.get(k)
            return cur if cur is not None else default
        except Exception:
            return default

    uid = (
        payload.get("uid")
        or _dig(payload, "data", default={}).get("uid")
        or _dig(payload, "data", "object", "metadata", default={}).get("uid")
        or _dig(payload, "metadata", default={}).get("uid")
        or payload.get("user_id")
    )

    # Plan from payload or metadata (default to "pro")
    plan = (
        str(payload.get("plan") or "").strip()
        or str(_dig(payload, "data", "object", "plan") or "").strip()
        or str(_dig(payload, "data", "object", "metadata", "plan") or "").strip()
        or "pro"
    )

    # Fallback: try resolve uid by email if provided
    if not uid:
        email = (
            _dig(payload, "email")
            or _dig(payload, "customer", "email")
            or _dig(payload, "data", "object", "email")
            or _dig(payload, "data", "object", "customer_email")
        )
        if email and firebase_enabled and fb_auth:
            try:
                user = fb_auth.get_user_by_email(str(email))
                uid = user.uid
            except Exception as ex:
                logger.warning(f"dodo_webhook: could not resolve uid for email {email}: {ex}")

    if not uid:
        logger.warning("dodo_webhook: missing uid in payment.succeeded payload")
        return JSONResponse({"error": "missing uid"}, status_code=400)

    # Update Firestore and local entitlement mirror
    db = _get_fs_client()
    if db is None or fb_fs is None:
        logger.error("dodo_webhook: Firestore unavailable")
        return JSONResponse({"error": "Firestore unavailable"}, status_code=500)

    try:
        doc_ref = db.collection('users').document(uid)
        update_payload = {
            "plan": plan,
            "isPaid": True,
            "updatedAt": fb_fs.SERVER_TIMESTAMP,
            "paidAt": fb_fs.SERVER_TIMESTAMP,
            "lastPaymentProvider": "dodo",
        }
        # Merge update so we don't overwrite other fields
        doc_ref.set(update_payload, merge=True)

        # Mirror entitlement for fast checks (best-effort)
        now = datetime.utcnow().isoformat()
        ent = read_json_key(_entitlement_key(uid)) or {}
        ent.update({"isPaid": True, "plan": plan, "source": "dodo", "updatedAt": now})
        write_json_key(_entitlement_key(uid), ent)

        return {"ok": True}
    except Exception as ex:
        logger.exception(f"dodo_webhook: failed to update user {uid} plan: {ex}")
        return JSONResponse({"error": "failed to update plan"}, status_code=500)


def _email_change_key(uid: str) -> str:
    return f"auth/email_change/{uid}.json"


def _password_change_key(uid: str) -> str:
    return f"auth/password_change/{uid}.json"


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


@router.post("/password/change/init")
async def password_change_init(request: Request):
    """
    Start password change (or set) with OTP verification. Sends a 6-digit code to the
    user's current email. Client should perform reauthentication for email/password
    accounts before calling this (frontend enforced), but server verifies via OTP.
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password change unavailable"}, status_code=500)

    try:
        # Get current email to deliver the code
        user = fb_auth.get_user(uid)
        current_email = (getattr(user, "email", None) or "").strip()
        if not current_email:
            return JSONResponse({"error": "current email unavailable"}, status_code=400)

        code = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.utcnow()
        rec = {
            "code": code,
            "sent_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=15)).isoformat(),
            "attempts": 0,
        }
        write_json_key(_password_change_key(uid), rec)

        subject = "Verify your password change"
        intro = (
            "We received a request to change the password on your account. "
            f"Use this verification code to confirm: <b>{code}</b><br><br>"
            "This code expires in 15 minutes. If you didn't request this, you can ignore this email."
        )
        html = render_email(
            "email_basic.html",
            title="Confirm your password change",
            intro=intro,
            footer_note=f"Request time (UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        ok = send_email_smtp(current_email, subject, html)
        if not ok:
            return JSONResponse({"error": "failed to send verification email"}, status_code=500)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"password change init failed for {uid}: {ex}")
        return JSONResponse({"error": "Failed to start password change"}, status_code=400)


@router.post("/password/change/confirm")
async def password_change_confirm(request: Request, payload: dict = Body(...)):
    """
    Confirm password change with OTP code and set the new password.
    Body: { "code": str, "new_password": str }
    Returns: { ok: true }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "password change unavailable"}, status_code=500)

    code = str((payload or {}).get("code") or "").strip()
    new_password = str((payload or {}).get("new_password") or "")
    if not code:
        return JSONResponse({"error": "verification code required"}, status_code=400)
    if not new_password or len(new_password) < 6:
        return JSONResponse({"error": "Password must be at least 6 characters"}, status_code=400)

    try:
        rec = read_json_key(_password_change_key(uid)) or {}
        saved_code = str(rec.get("code") or "").strip()
        attempts = int(rec.get("attempts") or 0)
        exp_str = rec.get("expires_at")

        if not saved_code or not exp_str:
            return JSONResponse({"error": "no pending password change"}, status_code=400)

        try:
            exp = datetime.fromisoformat(exp_str)
        except Exception:
            exp = datetime.utcnow() - timedelta(seconds=1)
        if datetime.utcnow() > exp:
            write_json_key(_password_change_key(uid), {})
            return JSONResponse({"error": "verification code expired"}, status_code=400)

        if code != saved_code:
            attempts += 1
            rec["attempts"] = attempts
            if attempts >= 5:
                write_json_key(_password_change_key(uid), {})
                return JSONResponse({"error": "too many invalid attempts"}, status_code=429)
            write_json_key(_password_change_key(uid), rec)
            return JSONResponse({"error": "invalid verification code"}, status_code=400)

        # Update password
        try:
            fb_auth.update_user(uid, password=new_password)
        except Exception as ex:
            logger.warning(f"password change confirm failed for {uid}: {ex}")
            return JSONResponse({"error": "Failed to update password"}, status_code=400)

        # Clear pending request
        write_json_key(_password_change_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"password change confirm (otp) error for {uid}: {ex}")
        return JSONResponse({"error": "Failed to confirm password change"}, status_code=400)


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
