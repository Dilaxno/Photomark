import os
import base64
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Header
from fastapi.responses import JSONResponse
from standardwebhooks import Webhook, WebhookVerificationError

from app.core.config import (
    logger,
    DODO_WEBHOOK_SECRET,
    LICENSE_SECRET,
    LICENSE_PRIVATE_KEY,
    LICENSE_PUBLIC_KEY,
    LICENSE_ISSUER,
)
from app.utils.storage import read_json_key, write_json_key
from app.core.auth import get_uid_by_email


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


def _extract_affiliate_uid(ref_code: str) -> str | None:
    rc = (ref_code or "").strip()
    if not rc:
        return None
    parts = rc.split("-")
    cand = parts[-1]
    return cand or None


def _custmap_key(customer_id: str) -> str:
    # Map external processor customer id to our internal user uid
    return f"payments/dodo/customer_map/{customer_id}.json"


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
    # Use centralized secret from config (supports whsec_ or base64)
    secret_raw = (DODO_WEBHOOK_SECRET or "").strip()
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

    # Handle payment succeeded (also for vault license purchases via share token)
    if event_type in ("payment.succeeded", "payment_succeeded", "charge.succeeded"):
        # Some providers nest the actual object under data.object or other containers
        obj = (
            (data.get("object") if isinstance(data, dict) else None)
            or (data.get("payment") if isinstance(data, dict) else None)
            or (data.get("order") if isinstance(data, dict) else None)
            or (data.get("checkout") if isinstance(data, dict) else None)
            or (data.get("session") if isinstance(data, dict) else None)
            or data
            or {}
        )

        # If share token is present, mark the corresponding shared vault as licensed
        try:
            meta = (obj.get("metadata") if isinstance(obj, dict) else None) or {}
            token = (meta.get("token") or "").strip()
            if token:
                # Load share record
                from app.routers.vaults import _share_key, _write_json_key, _read_json_key  # local import to avoid cycles
                rec = _read_json_key(_share_key(token)) or {}
                if rec:
                    rec["licensed"] = True
                    pay_id = obj.get("id") or obj.get("payment_id") or obj.get("session_id")
                    if pay_id:
                        rec["payment_id"] = str(pay_id)
                    _write_json_key(_share_key(token), rec)
        except Exception as _ex:
            logger.warning(f"[dodo.webhook] vault license mark failed: {_ex}")

        # Extract user id from multiple possible shapes (obj, data, payload)
        user_uid = str(
            obj.get("user_uid")
            or obj.get("customer_uid")
            or obj.get("userId")
            or obj.get("customerId")
            or obj.get("customer_id")
            or (obj.get("customer") or {}).get("id")
            or (obj.get("customer") or {}).get("customer_id")
            or (obj.get("metadata") or {}).get("user_uid")
            or data.get("user_uid")
            or data.get("customer_uid")
            or data.get("userId")
            or data.get("customerId")
            or data.get("customer_id")
            or (data.get("customer") or {}).get("id")
            or (data.get("customer") or {}).get("customer_id")
            or (data.get("metadata") or {}).get("user_uid")
            or (payload.get("customer") or {}).get("id")
            or (payload.get("customer") or {}).get("customer_id")
            or (payload.get("metadata") or {}).get("user_uid")
            or ""
        ).strip()

        # Try customer-id mapping cache first
        if not user_uid:
            cust_id = str(
                obj.get("customer_id")
                or (obj.get("customer") or {}).get("id")
                or data.get("customer_id")
                or (data.get("customer") or {}).get("id")
                or (payload.get("customer") or {}).get("id")
                or ""
            ).strip()
            if cust_id:
                try:
                    m = read_json_key(_custmap_key(cust_id)) or {}
                    mapped = (m.get("user_uid") or "").strip()
                    if mapped:
                        user_uid = mapped
                        logger.info(f"[dodo.webhook] customer_id cache hit {cust_id} -> {user_uid}")
                except Exception:
                    pass

        # Fallback: resolve uid by email when processor-only customer id is present
        if not user_uid:
            email_guess = str(
                (obj.get("customer") or {}).get("email")
                or obj.get("email")
                or (obj.get("metadata") or {}).get("email")
                or (data.get("customer") or {}).get("email")
                or data.get("email")
                or (data.get("metadata") or {}).get("email")
                or (payload.get("customer") or {}).get("email")
                or (payload.get("metadata") or {}).get("email")
                or ""
            ).strip().lower()
            if email_guess:
                try:
                    resolved_uid = get_uid_by_email(email_guess)
                    if resolved_uid:
                        user_uid = resolved_uid
                        logger.info(f"[dodo.webhook] resolved user by email {email_guess} -> {user_uid}")
                        # Persist mapping for future webhooks
                        if cust_id:
                            try:
                                write_json_key(_custmap_key(cust_id), {"user_uid": user_uid, "email": email_guess, "mapped_at": datetime.utcnow().isoformat()})
                            except Exception:
                                pass
                except Exception as _ex:
                    logger.warning(f"[dodo.webhook] email->uid resolution failed: {_ex}")

        def _parse_money_to_cents(v):
            """Convert various money representations to integer cents.
            Heuristics:
            - If value is int/float >= 1000 -> already cents
            - If numeric but small -> treat as dollars and multiply by 100
            - If string -> strip currency symbols/commas and parse
            """
            try:
                if v is None:
                    return None
                if isinstance(v, (int, float)):
                    return int(round(v)) if float(v) >= 1000 else int(round(float(v) * 100))
                s = str(v).strip()
                if not s:
                    return None
                s = s.replace(",", "").replace("$", "")
                # Pure integer-like string
                if s.isdigit():
                    iv = int(s)
                    return iv if iv >= 1000 else iv * 100
                # Decimal string
                return int(round(float(s) * 100))
            except Exception:
                return None

        def _dig(d: dict, path: tuple[str, ...]):
            cur = d if isinstance(d, dict) else None
            for k in path:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(k)
            return cur

        # 1) Prefer explicit cents fields from common locations
        cents_paths = [
            ("amount_cents",),
            ("total_cents",),
            ("price_cents",),
            ("amounts", "total_cents"),
            ("amounts", "paid_cents"),
            ("totals", "grand_cents"),
            ("payment", "amount_cents"),
            # Dodo specific: total_amount and settlement_amount are cents
            ("total_amount",),
            ("settlement_amount",),
        ]
        amount_cents = None
        for src in (obj, data, payload):
            if amount_cents is not None:
                break
            for p in cents_paths:
                v = _dig(src, p)
                cv = _parse_money_to_cents(v)
                if cv is not None:
                    amount_cents = cv
                    break

        # 2) Fallback to dollar-like fields and convert to cents
        if amount_cents is None:
            dollar_paths = [
                ("amount",),
                ("total",),
                ("price",),
                ("amount_total",),
                ("grand_total",),
                ("amounts", "total"),
                ("totals", "grand"),
                ("payment", "amount"),
            ]
            for src in (obj, data, payload):
                if amount_cents is not None:
                    break
                for p in dollar_paths:
                    v = _dig(src, p)
                    cv = _parse_money_to_cents(v)
                    if cv is not None:
                        amount_cents = cv
                        break

        amount_cents = amount_cents or 0

        currency = str(
            obj.get("currency")
            or (obj.get("amounts") or {}).get("currency")
            or (obj.get("total") or {}).get("currency")
            or (obj.get("settlement_currency") if isinstance(obj, dict) else None)
            or data.get("currency")
            or (data.get("amounts") or {}).get("currency")
            or data.get("settlement_currency")
            or payload.get("currency")
            or "usd"
        ).lower()

        # Send purchase confirmation email
        try:
            from app.utils.emailing import render_email, send_email_smtp
            from app.core.auth import get_user_email_from_uid

            user_email = str(
                obj.get("email")
                or (obj.get("customer") or {}).get("email")
                or (obj.get("metadata") or {}).get("email")
                or data.get("email")
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

        # Token-based license purchases may not include a user id
        token_present = bool(
            (obj.get("metadata") or {}).get("token")
            or (data.get("metadata") or {}).get("token")
        )

        if amount_cents <= 0:
            logger.warning("[dodo.webhook] missing amount")
            return {"ok": True}
        if not user_uid:
            if token_present:
                logger.info("[dodo.webhook] token-only purchase; no user id")
                return {"ok": True}
            logger.warning("[dodo.webhook] missing user id (no email match)")
            return {"ok": True}

        # Affiliate attribution (only when we have a user id)
        if user_uid:
            attrib = read_json_key(_attrib_key(user_uid)) or {}
            affiliate_uid = attrib.get("affiliate_uid")
            if not affiliate_uid:
                # Try attribution via metadata ref (e.g., ?ref=slug-uid passed into checkout)
                try:
                    meta_ref = str((obj.get("metadata") or {}).get("ref") or (data.get("metadata") or {}).get("ref") or (payload.get("metadata") or {}).get("ref") or "").strip()
                    if meta_ref:
                        affiliate_uid = _extract_affiliate_uid(meta_ref)
                        if affiliate_uid:
                            # Persist attribution now that we can resolve the user
                            write_json_key(_attrib_key(user_uid), {
                                "affiliate_uid": affiliate_uid,
                                "attributed_at": datetime.utcnow().isoformat(),
                                "ref": meta_ref,
                                "verified": True,
                            })
                except Exception:
                    pass

            if affiliate_uid:
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
            else:
                logger.info(f"[dodo.webhook] no attribution for user={user_uid}")

            # Mirror user paid status to Firestore 'users' collection
            try:
                _fs = _get_fs_client()
                if _fs and user_uid:
                    _fs.collection('users').document(user_uid).set({
                        'uid': user_uid,
                        'isPaid': True,
                        'planStatus': 'paid',
                        'currency': currency.upper(),
                        'lastPurchaseAt': datetime.utcnow(),
                        'lastAmountCents': int(amount_cents),
                        'updatedAt': datetime.utcnow(),
                    }, merge=True)
            except Exception:
                pass

    return {"ok": True}