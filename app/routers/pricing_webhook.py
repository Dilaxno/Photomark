from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import os
from typing import Optional
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from standardwebhooks import Webhook
from app.core.config import logger
from app.core.auth import get_fs_client as _get_fs_client
from app.utils.storage import write_json_key

try:
    from firebase_admin import firestore as fb_fs  # type: ignore
except Exception:
    fb_fs = None  # type: ignore

router = APIRouter(prefix="/api/pricing", tags=["pricing"])


# --- Robust plan normalization ---
def _normalize_plan(plan: Optional[str]) -> str:
    try:
        p = str(plan or "").strip().lower()
        if not p:
            return ""
        p = p.replace("_", " ").replace("-", " ")
        if p.endswith(" plan"):
            p = p[:-5]
        if p.endswith(" plans"):
            p = p[:-6]
        p = p.strip()
        if "photograph" in p or p in ("photo", "pg", "p"):
            return "photographers"
        if "agenc" in p or p in ("ag",):
            return "agencies"
        return ""
    except Exception as ex:
        logger.warning(f"[pricing.webhook] _normalize_plan failed for input {plan!r}: {ex}")
        return ""


def _allowed_plans() -> set[str]:
    raw = (os.getenv("PRICING_ALLOWED_PLANS") or os.getenv("ALLOWED_PLANS") or "").strip()
    out: set[str] = set()
    for tok in raw.split(","):
        slug = _normalize_plan(tok)
        if slug:
            out.add(slug)
    return out or {"photographers", "agencies"}


def _plan_from_products(obj: dict) -> str:
    allowed = _allowed_plans()
    pid_phot = (os.getenv("DODO_PHOTOGRAPHERS_PRODUCT_ID") or "").strip()
    pid_ag = (os.getenv("DODO_AGENCIES_PRODUCT_ID") or "").strip()
    found_phot = found_ag = False

    items = obj.get("product_cart") or []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            pid = str((it.get("product_id") or it.get("id") or "")).strip()
            if pid_phot and pid == pid_phot:
                found_phot = True
            if pid_ag and pid == pid_ag:
                found_ag = True

    if found_ag:
        return "agencies"
    if found_phot:
        return "photographers"
    return ""


def send_congratulations_email(to_email: str, user_name: str, plan_name: str, amount: int):
    SMTP_HOST = os.getenv("SMTP_HOST")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER = os.getenv("SMTP_USER")
    SMTP_PASS = os.getenv("SMTP_PASS")
    MAIL_FROM = os.getenv("MAIL_FROM")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎉 Congratulations on upgrading to {plan_name}!"
    msg["From"] = MAIL_FROM
    msg["To"] = to_email

    html_content = f"""
    <p>Hi {user_name},</p>
    <p>Your payment of ${amount/100:.2f} succeeded and your plan has been upgraded to <strong>{plan_name}</strong>.</p>
    <p>Thank you for using our service!</p>
    """
    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(MAIL_FROM, to_email, msg.as_string())
            logger.info(f"[pricing.webhook] Sent congratulations email to {to_email}")
    except Exception as ex:
        logger.warning(f"[pricing.webhook] Failed to send email to {to_email}: {ex}")


@router.post("/webhook")
async def pricing_webhook(request: Request):
    logger.info("[pricing.webhook] Received webhook")
    payload = None

    # Step 1: Verify secret
    secret_raw = (
        os.getenv("PRICING_WEBHOOK_SECRET")
        or os.getenv("DODO_PAYMENTS_WEBHOOK_KEY")
        or os.getenv("DODO_WEBHOOK_SECRET")
        or ""
    ).strip()
    try:
        if secret_raw.startswith("whsec_"):
            raw_body = await request.body()
            headers = {
                "webhook-id": request.headers.get("webhook-id") or request.headers.get("Webhook-Id") or "",
                "webhook-timestamp": request.headers.get("webhook-timestamp") or request.headers.get("Webhook-Timestamp") or "",
                "webhook-signature": request.headers.get("webhook-signature") or request.headers.get("Webhook-Signature") or "",
            }
            payload = Webhook(secret_raw).verify(data=raw_body, headers=headers)
        else:
            secret_provided = request.headers.get("X-Pricing-Secret") or request.headers.get("x-pricing-secret") or ""
            if secret_provided != secret_raw:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
    except Exception:
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    # Step 2: Parse payload if not verified yet
    if payload is None:
        try:
            payload = await request.json()
        except Exception as ex:
            logger.warning(f"[pricing.webhook] Invalid JSON: {ex}")
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

    # Step 3: Check event type
    evt_type = str((payload.get("type") or payload.get("event") or "")).strip().lower()
    if evt_type != "payment.succeeded":
        return {"ok": True, "ignored": True, "reason": "unexpected_event_type", "event_type": evt_type}

    # Step 4: Extract object & metadata
    obj = payload.get("data", {}).get("object") or {}
    meta = obj.get("metadata") or {}

    # Step 5: Resolve UID from metadata
    uid = str(meta.get("user_uid") or meta.get("uid") or "").strip()
    if not uid:
        logger.warning("[pricing.webhook] missing metadata UID; cannot upgrade")
        return {"ok": True, "skipped": True, "reason": "missing_metadata_uid"}

    # Step 6: Resolve plan
    plan_raw = str(meta.get("plan") or "").strip()
    plan = _normalize_plan(plan_raw) or _plan_from_products(obj)
    if plan not in _allowed_plans():
        allowed = sorted(list(_allowed_plans()))
        return {"ok": True, "skipped": True, "reason": "unsupported_plan", "plan_raw": plan_raw, "normalized": plan, "allowed": allowed}

    # Step 7: Update Firestore
    db = _get_fs_client()
    if not db or not fb_fs:
        logger.error("[pricing.webhook] Firestore unavailable")
        return {"ok": True, "skipped": True, "reason": "firestore_unavailable"}

    try:
        user_ref = db.collection("users").document(uid)
        user_snap = user_ref.get()
        old_plan = None
        if user_snap.exists:
            old_plan = user_snap.to_dict().get("plan", "free")

        if old_plan != plan:
            user_ref.set(
                {
                    "uid": uid,
                    "plan": plan,
                    "isPaid": True,
                    "planStatus": "paid",
                    "lastPaymentProvider": "dodo",
                    "updatedAt": fb_fs.SERVER_TIMESTAMP,
                    "paidAt": fb_fs.SERVER_TIMESTAMP,
                },
                merge=True,
            )
            logger.info(f"[pricing.webhook] Upgraded user {uid}: {old_plan} → {plan}")

            # Send email
            email = meta.get("email") or (user_snap.to_dict().get("email") if user_snap.exists else None)
            name = user_snap.to_dict().get("name", "there") if user_snap.exists else "there"
            if email:
                send_congratulations_email(email, name, plan, obj.get("total_amount", 0))
    except Exception as ex:
        logger.warning(f"[pricing.webhook] Failed to persist plan for {uid}: {ex}")
        return {"ok": True, "skipped": True, "reason": "firestore_write_failed", "error": str(ex)}
# Step 8: Mirror entitlement locally
    try:
        write_json_key(f"users/{uid}/billing/entitlement.json", {
            "isPaid": True,
            "plan": plan,
            "updatedAt": obj.get("created_at") or obj.get("paid_at") or obj.get("timestamp") or None
        })
    except Exception:
        pass

    logger.info(f"[pricing.webhook] Completed upgrade: uid={uid}, plan={plan}")
    return {"ok": True, "upgraded": True, "uid": uid, "plan": plan}
