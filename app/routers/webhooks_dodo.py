import os
import base64
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Header
from fastapi.responses import JSONResponse
from standardwebhooks import Webhook, WebhookVerificationError

from app.core.config import logger
from app.utils.storage import read_json_key, write_json_key


# Optional Firestore mirroring with lazy client retrieval
try:
    # Importing app.core.auth ensures Firebase Admin app is initialized from env
    from app.core.auth import firebase_enabled  # noqa: F401
    from firebase_admin import firestore as fb_fs
    def _get_fs_client():
        try:
            return fb_fs.client()
        except Exception:
            return None
except Exception:
    def _get_fs_client():
        return None

router = APIRouter(prefix="/api/payments/dodo/webhook", tags=["webhooks"]) 


def _stats_key(affiliate_uid: str) -> str:
    return f"affiliates/{affiliate_uid}/stats.json"


def _attrib_key(user_uid: str) -> str:
    return f"affiliates/attributions/{user_uid}.json"


def _default_payout_rate_cents(gross_cents: int) -> int:
    # Flat 40% payout by default; adjust to tiers if needed
    return int(round(gross_cents * 0.40))


def _get_event_type(payload: dict) -> str:
    t = str(payload.get("type") or payload.get("event") or "").strip()
    return t.lower()


@router.post("")
async def dodo_webhook(
    request: Request,
    webhook_signature: Optional[str] = Header(default=None, alias="webhook-signature"),
    webhook_timestamp: Optional[str] = Header(default=None, alias="webhook-timestamp"),
    webhook_id: Optional[str] = Header(default=None, alias="webhook-id"),
):
    # Grab the secret from systemd env first, fallback to .env
    secret_raw = os.getenv("DODO_PAYMENTS_WEBHOOK_KEY", "").strip()
    if not secret_raw:
        logger.error("[dodo.webhook] webhook secret not configured")
        return JSONResponse({"error": "webhook not configured"}, status_code=401)

    # standardwebhooks expects either a base64 string (optionally prefixed with 'whsec_') or raw bytes
    wh_secret = None
    try:
        if secret_raw.startswith("whsec_"):
            wh_secret = secret_raw
        else:
            # If it's valid base64, keep as str; otherwise pass bytes for backward compatibility
            base64.b64decode(secret_raw)
            wh_secret = secret_raw
    except Exception:
        wh_secret = secret_raw.encode()

    try:
        raw_body = await request.body()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)

    # Verify using Standard Webhooks library (checks id, timestamp, signature with tolerance)
    try:
        wh = Webhook(wh_secret)
        payload = wh.verify(
            data=raw_body,
            headers={
                "webhook-id": webhook_id or "",
                "webhook-timestamp": webhook_timestamp or "",
                "webhook-signature": webhook_signature or "",
            },
        )
    except WebhookVerificationError as ex:
        logger.warning(f"[dodo.webhook] verification failed: {ex}")
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    except Exception as ex:
        logger.warning(f"[dodo.webhook] verification error: {ex}")
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    event_type = _get_event_type(payload)
    data = payload.get("data") or {}

    logger.info(f"[dodo.webhook] type={event_type}")

    # Handle payment succeeded
    if event_type in ("payment.succeeded", "payment_succeeded", "charge.succeeded"):
        user_uid = str(
            data.get("user_uid")
            or data.get("customer_uid")
            or data.get("userId")
            or data.get("customerId")
            or (data.get("customer") or {}).get("id")
            or (data.get("metadata") or {}).get("user_uid")
            or ""
        ).strip()

        try:
            amount_cents = int(
                data.get("amount_cents")
                or data.get("amount")
                or (data.get("amounts") or {}).get("total_cents")
                or 0
            )
        except Exception:
            amount_cents = 0

        currency = str(
            data.get("currency")
            or (data.get("amounts") or {}).get("currency")
            or "usd"
        ).lower()

        # Send purchase confirmation email
        try:
            from app.utils.emailing import render_email, send_email_smtp
            from app.core.auth import get_user_email_from_uid

            user_email = str(
                data.get("email")
                or (data.get("customer") or {}).get("email")
                or (data.get("metadata") or {}).get("email")
                or ""
            ).strip()

            if not user_email and user_uid:
                user_email = (get_user_email_from_uid(user_uid) or "").strip()

            if user_email and amount_cents > 0:
                app_name = os.getenv("APP_NAME", "Photomark")
                symbol = "$" if currency.lower() in ("usd", "cad", "aud", "nzd", "sgd") else ""
                amount_formatted = f"{symbol}{amount_cents/100:.2f} {currency.upper()}".strip()
                subject = f"Thanks for your purchase — {amount_formatted} received"
                intro_html = (
                    f"Congratulations! Your payment of <b>{amount_formatted}</b> was successful.<br><br>"
                    f"Your {app_name} plan is now active. If you have any questions, just reply to this email."
                )
                html = render_email("email_basic.html", title="Payment confirmed", intro=intro_html)
                text = f"Congratulations! Your payment of {amount_formatted} was successful.\nYour {app_name} plan is now active."
                send_email_smtp(
                    user_email,
                    subject,
                    html,
                    text,
                    from_addr=os.getenv("MAIL_FROM_BILLING", os.getenv("MAIL_FROM", "noreply@photomark.cloud")),
                    reply_to=os.getenv("REPLY_TO_BILLING", os.getenv("REPLY_TO", "support@photomark.cloud")),
                    from_name=os.getenv("MAIL_FROM_NAME_BILLING", "Photomark Billing"),
                )
        except Exception as _ex:
            logger.warning(f"[dodo.webhook] purchase email failed: {_ex}")

        if not user_uid or amount_cents <= 0:
            logger.warning("[dodo.webhook] missing user or amount")
            return {"ok": True}

        # Affiliate attribution
        attrib = read_json_key(_attrib_key(user_uid)) or {}
        affiliate_uid = attrib.get("affiliate_uid")
        if not affiliate_uid:
            logger.info(f"[dodo.webhook] no attribution for user={user_uid}")
            return {"ok": True}

        # Update stats
        stats = read_json_key(_stats_key(affiliate_uid)) or {}
        gross = int(stats.get("gross_cents") or 0) + amount_cents
        conversions = int(stats.get("conversions") or 0) + 1
        payout_add = _default_payout_rate_cents(amount_cents)
        payout_total = int(stats.get("payout_cents") or 0) + payout_add

        stats.update({
            "gross_cents": gross,
            "payout_cents": payout_total,
            "conversions": conversions,
            "currency": currency,
            "last_conversion_at": datetime.utcnow().isoformat(),
        })
        write_json_key(_stats_key(affiliate_uid), stats)

        # Mirror to Firestore
        try:
            _fs = _get_fs_client()
            if _fs:
                _fs.collection('affiliate_stats').document(affiliate_uid).set({
                    **stats,
                    'uid': affiliate_uid,
                    'updatedAt': datetime.utcnow(),
                }, merge=True)
        except Exception:
            pass

        logger.info(f"[dodo.webhook] attributed {amount_cents}c to affiliate={affiliate_uid} (+{payout_add}c payout)")

    return {"ok": True}