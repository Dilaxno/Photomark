from fastapi import APIRouter, Request, Header
from fastapi.responses import JSONResponse
from datetime import datetime
from typing import Optional
import os

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
    x_dodo_signature: Optional[str] = Header(default=None, alias="X-Dodo-Signature"),
    dodo_signature: Optional[str] = Header(default=None, alias="Dodo-Signature"),
    webhook_signature: Optional[str] = Header(default=None, alias="webhook-signature"),
    webhook_timestamp: Optional[str] = Header(default=None, alias="webhook-timestamp"),
    webhook_id: Optional[str] = Header(default=None, alias="webhook-id"),
):
    # Verify signature using shared secret from env (support multiple common env names)
    secret = (
        os.getenv("DODO_WEBHOOK_SECRET")
        or os.getenv("NUXT_PRIVATE_WEBHOOK_KEY")
        or os.getenv("WEBHOOK_SECRET")
        or os.getenv("DODO_WEBHOOK_KEY")
        or ""
    ).strip()
    try:
        raw = await request.body()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)
    if secret:
        try:
            import hmac, hashlib, base64, json as _json
            provided_header = (webhook_signature or x_dodo_signature or dodo_signature or "").strip()
            # Extract the signature value (supports formats like "v1,<sig>", "v1=<sig>", "sha256=<sig>")
            received_sig = ""
            if provided_header:
                parts = [p.strip() for p in provided_header.split(",") if p.strip()]
                if len(parts) >= 2 and parts[0].lower() in ("v1", "sha256") and "=" not in parts[0]:
                    # Format: "v1,<sig>"
                    received_sig = parts[1]
                else:
                    # Look for key=value tokens
                    for p in parts:
                        if "=" in p:
                            k, v = p.split("=", 1)
                            if k.strip().lower() in ("v1", "sha256") and v.strip():
                                received_sig = v.strip()
                                break
                    if not received_sig and parts:
                        # Fallback: single token signature
                        received_sig = parts[-1]
            # Strict Dodo scheme per docs: Base64(HMAC_SHA256(secret, "{webhook-id}.{webhook-timestamp}.{raw_body}"))
            if not webhook_timestamp or not webhook_id:
                logger.warning("[dodo.webhook] missing required headers for verification")
                return JSONResponse({"error": "invalid signature"}, status_code=401)

            # Build possible message byte patterns to cover provider variations
            id_b = webhook_id.encode("utf-8")
            ts_b = webhook_timestamp.encode("utf-8")
            dot = b"."
            colon = b":"
            body_b = raw  # exact raw bytes
            message_variants = [
                id_b + dot + ts_b + dot + body_b,
                ts_b + dot + id_b + dot + body_b,
                id_b + colon + ts_b + colon + body_b,
                ts_b + colon + id_b + colon + body_b,
            ]

            # Compute expected signatures using multiple key interpretations and encodings
            rec = (received_sig or "").strip()
            try:
                import binascii
                # Clean secret of accidental wrapping quotes
                s_clean = secret.strip().strip('"').strip("'")
                keys = []
                # Raw UTF-8 secret (most common)
                keys.append(s_clean.encode("utf-8"))
                # Try base64-decoded secret (some dashboards expose base64 strings)
                try:
                    k_b64 = base64.b64decode(s_clean, validate=False)
                    if k_b64:
                        keys.append(k_b64)
                except Exception:
                    pass
                # Try hex-decoded secret
                try:
                    if s_clean and all(c in "0123456789abcdefABCDEF" for c in s_clean) and (len(s_clean) % 2 == 0):
                        keys.append(binascii.unhexlify(s_clean))
                except Exception:
                    pass

                expected_set = set()
                for k in keys:
                    for msg in message_variants:
                        d = hmac.new(k, msg, hashlib.sha256).digest()
                        # Base64 variants
                        b64std = base64.b64encode(d).decode().strip()
                        b64url = base64.urlsafe_b64encode(d).decode().strip()
                        expected_set.update({b64std, b64std.rstrip("="), b64url, b64url.rstrip("=")})
                        # Hex variants (some providers/documentation examples use hex)
                        hexlower = d.hex()
                        hexupper = hexlower.upper()
                        expected_set.update({hexlower, hexupper})

                # Normalize received to compare against both base64 and base64url
                rec_std = rec.replace("-", "+").replace("_", "/")
                candidates = {rec, rec.rstrip("="), rec_std, rec_std.rstrip("="), rec.lower(), rec.upper()}
                ok = any(hmac.compare_digest(a, b) for a in candidates for b in expected_set)
            except Exception:
                ok = False

            if not ok:
                # Optional debug: show prefixes to diagnose mismatches (enable via DODO_WEBHOOK_DEBUG)
                if (os.getenv("DODO_WEBHOOK_DEBUG") or "").strip():
                    try:
                        any_expected = next(iter(expected_set)) if 'expected_set' in locals() and expected_set else ""
                    except Exception:
                        any_expected = ""
                    logger.warning(
                        f"[dodo.webhook] mismatch: rec={rec[:12]}..., exp={any_expected[:12]}..., ts='{webhook_timestamp}', id='{webhook_id}', raw_len={len(raw)}"
                    )
                return JSONResponse({"error": "invalid signature"}, status_code=401)

            # Parse JSON from the same raw body to avoid any whitespace changes
            payload = _json.loads(raw.decode("utf-8"))
        except Exception as ex:
            logger.warning(f"[dodo.webhook] signature check error: {ex}")
            return JSONResponse({"error": "invalid signature"}, status_code=401)
    else:
        # Reject if not configured
        return JSONResponse({"error": "webhook not configured"}, status_code=401)

    event_type = _get_event_type(payload)
    data = payload.get("data") or {}

    logger.info(f"[dodo.webhook] type={event_type}")
    # Send purchase confirmation on simple event name
    if event_type == "payment.succeeded":
        try:
            from app.utils.emailing import render_email, send_email_smtp
            from app.core.auth import get_user_email_from_uid
            data = payload.get("data") or {}
            user_uid = str(
                data.get("user_uid")
                or data.get("customer_uid")
                or (data.get("metadata") or {}).get("user_uid")
                or ""
            ).strip()
            user_email = str(
                data.get("email")
                or (data.get("customer") or {}).get("email")
                or (data.get("metadata") or {}).get("email")
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
                text = (
                    f"Congratulations! Your payment of {amount_formatted} was successful.\n"
                    f"Your {app_name} plan is now active."
                )
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

    if event_type in ("payment.succeeded", "payment_succeeded", "charge.succeeded"):
        # Map fields from Dodo schema to our names (robust fallbacks)
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

        # Send purchase confirmation email for payment.succeeded
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
                text = (
                    f"Congratulations! Your payment of {amount_formatted} was successful.\n"
                    f"Your {app_name} plan is now active."
                )
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

        # Find affiliate attribution for this paying user
        attrib = read_json_key(_attrib_key(user_uid)) or {}
        affiliate_uid = attrib.get("affiliate_uid")
        if not affiliate_uid:
            logger.info(f"[dodo.webhook] no attribution for user={user_uid}")
            return {"ok": True}

        # Update aggregate stats
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
        # Mirror in Firestore (lazy)
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

    # Acknowledge unhandled event types
    return {"ok": True}