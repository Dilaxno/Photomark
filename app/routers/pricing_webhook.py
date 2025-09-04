from fastapi import APIRouter, Request, Header, Body
from fastapi.responses import JSONResponse
from typing import Optional
import os
import json

from app.core.config import logger
from standardwebhooks import Webhook, WebhookVerificationError
from app.core.auth import (
    get_fs_client as _get_fs_client,
    get_uid_from_request,
    get_uid_by_email,
    firebase_enabled,
    fb_auth,
)
from app.utils.storage import read_json_key, write_json_key

# Firestore client via centralized helper
try:
    from firebase_admin import firestore as fb_fs  # type: ignore
except Exception:
    fb_fs = None  # type: ignore

router = APIRouter(prefix="/api/pricing", tags=["pricing"]) 


# Helpers

def _entitlement_key(uid: str) -> str:
    return f"users/{uid}/billing/entitlement.json"


def _normalize_plan(plan: Optional[str]) -> str:
    p = (plan or "").strip().lower()
    if p in ("photographer", "photographers", "photo", "pg", "p"):
        return "photographers"
    if p in ("agency", "agencies", "ag"):
        return "agencies"
    return ""


def _allowed_plans() -> set[str]:
    # Optionally controlled by env. Defaults are the two internal plans
    raw = (os.getenv("PRICING_ALLOWED_PLANS") or os.getenv("ALLOWED_PLANS") or "").strip()
    if raw:
        out: set[str] = set()
        for tok in raw.split(","):
            slug = _normalize_plan(tok)
            if slug:
                out.add(slug)
        if out:
            return out
    return {"photographers", "agencies"}


def _first_email_from_payload(payload: dict) -> str:
    candidates = []
    try:
        # Some common paths across providers
        paths = (
            ["email"],
            ["customer", "email"],
            ["data", "object", "email"],
            ["data", "object", "customer_email"],
            ["object", "customer_email"],
            ["object", "email"],
            ["metadata", "email"],
        )
        for path in paths:
            node = payload
            for key in path:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                else:
                    node = None
                    break
            if isinstance(node, str) and "@" in node:
                candidates.append(node.strip().lower())
    except Exception:
        pass
    return candidates[0] if candidates else ""


@router.get("/user")
async def pricing_user(request: Request):
    """Return authenticated user's uid, email and current plan for the pricing page.
    Response: { uid, email, plan, isPaid }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    email = ""
    plan = "free"
    is_paid = False

    # Prefer Firestore as source of truth
    try:
        db = _get_fs_client()
        if db and fb_fs:
            snap = db.collection("users").document(uid).get()
            if snap.exists:
                data = snap.to_dict() or {}
                email = str(data.get("email") or "").strip()
                plan = str(data.get("plan") or plan)
                is_paid = bool(data.get("isPaid") or False)
    except Exception as ex:
        logger.debug(f"[pricing.user] firestore read failed for {uid}: {ex}")

    # Fallback to entitlement mirror
    try:
        ent = read_json_key(_entitlement_key(uid)) or {}
        if ent:
            plan = str(ent.get("plan") or plan)
            is_paid = bool(ent.get("isPaid") or is_paid)
    except Exception:
        pass

    # Optional: fetch email from Firebase Auth if not in Firestore
    if not email and firebase_enabled and fb_auth:
        try:
            user = fb_auth.get_user(uid)
            email = (getattr(user, "email", None) or "").strip()
        except Exception:
            email = ""

    return {"uid": uid, "email": email, "plan": plan, "isPaid": bool(is_paid)}


@router.post("/webhook")
async def pricing_webhook(request: Request):
    """Webhook endpoint to receive payment events for pricing upgrades.
    Accepts generic JSON payloads; on type=payment.succeeded attempts to resolve the user and plan,
    then persists to Firestore immediately for instant upgrade on the frontend.

    Security:
      - If PRICING_WEBHOOK_SECRET or DODO_PAYMENTS_WEBHOOK_KEY is set and starts with whsec_,
        verify using provider's Standard Webhooks signature headers.
      - Otherwise require X-Pricing-Secret header to equal the configured secret.
    """
    logger.info("[pricing.webhook] received webhook")
    # Verify secret and parse payload
    payload = None
    try:
        secret_raw = (
            os.getenv("PRICING_WEBHOOK_SECRET")
            or os.getenv("DODO_PAYMENTS_WEBHOOK_KEY")
            or os.getenv("DODO_WEBHOOK_SECRET")
            or ""
        ).strip()
        if secret_raw:
            if secret_raw.startswith("whsec_"):
                try:
                    raw_body = await request.body()
                    headers = {
                        "webhook-id": request.headers.get("webhook-id") or request.headers.get("Webhook-Id") or "",
                        "webhook-timestamp": request.headers.get("webhook-timestamp") or request.headers.get("Webhook-Timestamp") or "",
                        "webhook-signature": request.headers.get("webhook-signature") or request.headers.get("Webhook-Signature") or "",
                    }
                    payload = Webhook(secret_raw).verify(data=raw_body, headers=headers)
                    logger.info(f"[pricing.webhook] verified signature (standardwebhooks); id={headers.get('webhook-id','')}")
                except WebhookVerificationError as ex:
                    logger.warning(f"[pricing.webhook] verification failed: {ex}")
                    return JSONResponse({"error": "invalid signature"}, status_code=401)
                except Exception as ex:
                    logger.warning(f"[pricing.webhook] verification error: {ex}")
                    return JSONResponse({"error": "invalid signature"}, status_code=401)
            else:
                secret_provided = request.headers.get("X-Pricing-Secret") or request.headers.get("x-pricing-secret") or ""
                if secret_provided != secret_raw:
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
                logger.info("[pricing.webhook] verified via header secret")
    except Exception:
        # If anything unexpected happens with verification, reject for safety when a secret is configured
        if (os.getenv("PRICING_WEBHOOK_SECRET") or os.getenv("DODO_PAYMENTS_WEBHOOK_KEY") or os.getenv("DODO_WEBHOOK_SECRET")):
            return JSONResponse({"error": "invalid signature"}, status_code=401)

    if payload is None:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

    evt_type = str((payload.get("type") or payload.get("event") or "")).strip().lower()
    logger.info(f"[pricing.webhook] type={evt_type}")
    if evt_type != "payment.succeeded":
        logger.info(f"[pricing.webhook] ignoring event type '{evt_type}'")
        return {"ok": True}

    data = payload.get("data") or {}
    obj = data.get("object") if isinstance(data, dict) else None
    obj = obj if isinstance(obj, dict) else (data if isinstance(data, dict) else payload)

    meta = obj.get("metadata") if isinstance(obj, dict) else None
    meta = meta if isinstance(meta, dict) else {}

    # Resolve user id
    uid = (
        str((meta.get("user_uid") or meta.get("uid") or meta.get("firebase_uid") or "")).strip()
        or str((obj.get("user_uid") or obj.get("uid") or obj.get("firebase_uid") or "")).strip()
        or str((payload.get("uid") or payload.get("user_uid") or payload.get("firebase_uid") or "")).strip()
    )
    if uid:
        logger.info(f"[pricing.webhook] resolved uid from metadata: {uid}")

    # Fallback by email
    if not uid:
        em = _first_email_from_payload(payload) or _first_email_from_payload(obj or {})
        if em:
            try:
                uid = get_uid_by_email(em) or ""
                logger.info(f"[pricing.webhook] resolved uid via email lookup: email={em} uid={uid}")
            except Exception:
                uid = ""

    if not uid:
        logger.warning("[pricing.webhook] missing uid in payment.succeeded payload; skip")
        return {"ok": True}

    # Resolve desired plan
    plan_raw = (
        str(payload.get("plan") or "").strip()
        or str(obj.get("plan") or "").strip()
        or str(meta.get("plan") or meta.get("tier") or meta.get("product_plan") or "").strip()
    )
    plan = _normalize_plan(plan_raw)
    logger.info(f"[pricing.webhook] plan_raw='{plan_raw}' normalized='{plan}'")

    if not plan or plan not in _allowed_plans():
        logger.warning(f"[pricing.webhook] unknown or unsupported plan '{plan_raw}' for uid={uid}; allowed={sorted(list(_allowed_plans()))}")
        return {"ok": True}

    # Firestore write
    db = _get_fs_client()
    if not db or not fb_fs:
        logger.error("[pricing.webhook] Firestore unavailable; cannot persist plan")
        return {"ok": True}

    try:
        logger.info(f"[pricing.webhook] persist: uid={uid} plan={plan} event_type={evt_type}")
        db.collection("users").document(uid).set(
            {
                "uid": uid,
                "plan": plan,
                "isPaid": True,
                "planStatus": "paid",
                "lastPaymentProvider": "pricing",
                "updatedAt": fb_fs.SERVER_TIMESTAMP,
                "paidAt": fb_fs.SERVER_TIMESTAMP,
            },
            merge=True,
        )

        # Local entitlement mirror for fast checks
        try:
            write_json_key(_entitlement_key(uid), {"isPaid": True, "plan": plan, "updatedAt": obj.get("created_at") or obj.get("paid_at") or obj.get("timestamp") or None})
            logger.info(f"[pricing.webhook] entitlement mirror updated for uid={uid} plan={plan}")
        except Exception:
            pass
    except Exception as ex:
        logger.warning(f"[pricing.webhook] failed to persist plan for {uid}: {ex}")
        return {"ok": True}

    logger.info(f"[pricing.webhook] completed upgrade: uid={uid} plan={plan}")
    return {"ok": True}
