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
    if not p:
        return ""
    # Normalize separators and remove common suffixes like "plan"
    p = p.replace("_", " ").replace("-", " ")
    if p.endswith(" plan"):
        p = p[:-5]
    if p.endswith(" plans"):
        p = p[:-6]
    p = p.strip()

    # Match by contains so variations like "photographers plan" work
    if "photograph" in p or p in ("photo", "pg", "p"):
        return "photographers"
    if "agenc" in p or p in ("ag",):
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


def _plan_from_products(obj: dict) -> str:
    """Infer plan from Dodo payload products when explicit plan metadata is missing.
    Prefers mapping by configured product IDs, then by product names, and only returns
    one of the allowed internal slugs: 'photographers' or 'agencies'.
    """
    allowed = _allowed_plans()
    pid_phot = (os.getenv("DODO_PHOTOGRAPHERS_PRODUCT_ID") or "").strip()
    pid_ag = (os.getenv("DODO_AGENCIES_PRODUCT_ID") or "").strip()
    found_ag = False
    found_phot = False
    names: list[str] = []

    try:
        items = obj.get("product_cart") or []
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                # Direct fields
                pid = str((it.get("product_id") or it.get("id") or "")).strip()
                name = str((it.get("product_name") or it.get("name") or it.get("title") or "")).strip()
                # Nested product object
                p = it.get("product")
                if isinstance(p, dict):
                    pid = pid or str((p.get("id") or p.get("product_id") or "")).strip()
                    name = name or str((p.get("name") or p.get("title") or "")).strip()

                if pid_ag and pid and pid == pid_ag:
                    found_ag = True
                if pid_phot and pid and pid == pid_phot:
                    found_phot = True
                if name:
                    names.append(name)

        # Sometimes a single product object may be present
        if not items and isinstance(obj.get("product"), dict):
            p = obj.get("product") or {}
            pid = str((p.get("id") or p.get("product_id") or "")).strip()
            name = str((p.get("name") or p.get("title") or "")).strip()
            if pid_ag and pid and pid == pid_ag:
                found_ag = True
            if pid_phot and pid and pid == pid_phot:
                found_phot = True
            if name:
                names.append(name)
    except Exception:
        pass

    try:
        logger.info(f"[pricing.webhook] product mapping: found_agencies={found_ag} found_photographers={found_phot} names={names}")
    except Exception:
        pass

    if found_ag:
        return "agencies"
    if found_phot:
        return "photographers"

    # Fallback: try names
    for nm in names:
        slug = _normalize_plan(nm)
        if slug in allowed:
            return slug
    return ""


@router.get("/user")
async def pricing_user(request: Request):
    """Return authenticated user's uid, email and current plan for the pricing page.
    Response: { uid, email, plan, isPaid }
    """
    uid = get_uid_from_request(request)
    if not uid:
        logger.info("[pricing.user] unauthorized request (no uid)")
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
                logger.info(f"[pricing.user] firestore read ok: uid={uid} email='{email}' plan='{plan}' isPaid={is_paid}")
    except Exception as ex:
        logger.debug(f"[pricing.user] firestore read failed for {uid}: {ex}")

    # Fallback to entitlement mirror
    try:
        ent = read_json_key(_entitlement_key(uid)) or {}
        if ent:
            prev_plan, prev_paid = plan, is_paid
            plan = str(ent.get("plan") or plan)
            is_paid = bool(ent.get("isPaid") or is_paid)
            logger.info(f"[pricing.user] entitlement read: uid={uid} plan '{prev_plan}' -> '{plan}', isPaid {prev_paid} -> {is_paid}")
    except Exception:
        pass

    # Optional: fetch email from Firebase Auth if not in Firestore
    if not email and firebase_enabled and fb_auth:
        try:
            user = fb_auth.get_user(uid)
            email = (getattr(user, "email", None) or "").strip()
        except Exception:
            email = ""

    logger.info(f"[pricing.user] return: uid={uid} email='{email}' plan='{plan}' isPaid={bool(is_paid)}")
    return {"uid": uid, "email": email, "plan": plan, "isPaid": bool(is_paid)}


@router.post("/webhook")
async def pricing_webhook(request: Request):
    """
    Webhook endpoint to receive payment events for pricing upgrades.
    Security:
      - If PRICING_WEBHOOK_SECRET or DODO_PAYMENTS_WEBHOOK_KEY is set and starts with whsec_,
        verify using provider's Standard Webhooks signature headers.
      - Otherwise require X-Pricing-Secret header to equal the configured secret.
    """

    logger.info("[pricing.webhook] received webhook")
    payload = None

    # --- Step 1: Verify secret ---
    try:
        secret_raw = (
            os.getenv("PRICING_WEBHOOK_SECRET")
            or os.getenv("DODO_PAYMENTS_WEBHOOK_KEY")
            or os.getenv("DODO_WEBHOOK_SECRET")
            or ""
        ).strip()

        if secret_raw:
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
        if secret_raw:
            return JSONResponse({"error": "invalid signature"}, status_code=401)

    # --- Step 2: Parse JSON payload if not already verified ---
    if payload is None:
        try:
            payload = await request.json()
        except Exception as ex:
            logger.warning(f"[pricing.webhook] invalid JSON: {ex}")
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

    # --- Step 3: Event type check ---
    evt_type = str((payload.get("type") or payload.get("event") or "")).strip().lower()
    if evt_type != "payment.succeeded":
        return {"ok": True, "ignored": True, "reason": "unexpected_event_type", "event_type": evt_type}

    # --- Step 4: Extract object and metadata ---
    data = payload.get("data") or {}
    obj = data.get("object") if isinstance(data, dict) else payload
    obj = obj if isinstance(obj, dict) else {}
    meta = obj.get("metadata") if isinstance(obj, dict) else {}
    meta = meta if isinstance(meta, dict) else {}

    # --- Step 5: Enforce presence of metadata.user_uid ---
    uid = str(meta.get("user_uid") or "").strip()
    if not uid:
        logger.warning("[pricing.webhook] missing metadata.user_uid; cannot upgrade")
        return {"ok": True, "skipped": True, "reason": "missing_metadata_user_uid"}

    # --- Step 6: Resolve plan ---
    plan_raw = str(meta.get("plan") or "").strip()
    plan = _normalize_plan(plan_raw)
    if not plan:
        # Fallback to product_cart mapping
        plan = _plan_from_products(obj or {})
    if not plan or plan not in _allowed_plans():
        allowed = sorted(list(_allowed_plans()))
        return {"ok": True, "skipped": True, "reason": "unsupported_plan", "plan_raw": plan_raw, "normalized": plan, "allowed": allowed}

    # --- Step 7: Persist to Firestore ---
    db = _get_fs_client()
    if not db or not fb_fs:
        logger.error(f"[pricing.webhook] Firestore unavailable; cannot persist plan")
        return {"ok": True, "skipped": True, "reason": "firestore_unavailable"}

try:
       db.collection("users").document(uid).set(
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
except Exception as ex:
    logger.warning(f"[pricing.webhook] failed to persist plan for {uid}: {ex}")
    return {"ok": True, "skipped": True, "reason": "firestore_write_failed", "error": str(ex)}

        # Local entitlement mirror
    try:
            write_json_key(_entitlement_key(uid), {
                "isPaid": True,
                "plan": plan,
                "updatedAt": obj.get("created_at") or obj.get("paid_at") or obj.get("timestamp") or None
            })
    except Exception:
            pass
    except Exception as ex:
        logger.warning(f"[pricing.webhook] failed to persist plan for {uid}: {ex}")
        return {"ok": True, "skipped": True, "reason": "firestore_write_failed", "error": str(ex)}

    logger.info(f"[pricing.webhook] completed upgrade: uid={uid} plan={plan}")
    return {"ok": True, "upgraded": True, "uid": uid, "plan": plan}
