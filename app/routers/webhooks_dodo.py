import json
import base64
import urllib.request
import urllib.error
from typing import Optional

from fastapi import APIRouter, Request, Header, Body
from fastapi.responses import JSONResponse
from standardwebhooks import Webhook, WebhookVerificationError

from app.core.config import (
    logger,
    DODO_PAYMENTS_WEBHOOK_KEY,
    DODO_API_BASE,
    DODO_CHECKOUT_PATH,
    DODO_API_KEY,
)
from app.utils.storage import write_json_key, read_json_key
from app.core.auth import get_fs_client as _get_fs_client, get_uid_from_request, get_uid_by_email

# Firestore client via centralized helper
try:
    from firebase_admin import firestore as fb_fs  # type: ignore
except Exception:
    fb_fs = None  # type: ignore

router = APIRouter(prefix="/api/payments/dodo/webhook", tags=["webhooks"]) 


# Helpers

def _entitlement_key(uid: str) -> str:
    return f"users/{uid}/billing/entitlement.json"


def _event_type(payload: dict) -> str:
    return str(payload.get("type") or payload.get("event") or "").strip().lower()


def _get_obj(payload: dict) -> dict:
    data = payload.get("data") or {}
    if isinstance(data, dict):
        for key in ("object", "payment", "session", "checkout", "order"):
            obj = data.get(key)
            if isinstance(obj, dict):
                return obj
    return data if isinstance(data, dict) else payload


def _normalize_plan(plan: Optional[str]) -> str:
    p = (plan or "").strip().lower()
    if p in ("photographer", "photographers"):
        return "photographers"
    if p in ("agency", "agencies"):
        return "agencies"
    return p or "pro"


# 1) Webhook receiver (fresh minimal implementation)
@router.post("")
async def dodo_webhook(
    request: Request,
    webhook_signature: Optional[str] = Header(default=None, alias="webhook-signature"),
    webhook_timestamp: Optional[str] = Header(default=None, alias="webhook-timestamp"),
    webhook_id: Optional[str] = Header(default=None, alias="webhook-id"),
):
    # Verify signature
    secret_raw = (DODO_PAYMENTS_WEBHOOK_KEY or "").strip()
    if not secret_raw:
        logger.error("[dodo.webhook] secret not configured")
        return JSONResponse({"error": "webhook not configured"}, status_code=401)

    try:
        raw_body = await request.body()
    except Exception:
        return JSONResponse({"error": "invalid body"}, status_code=400)

    # standardwebhooks accepts whsec_ or base64 secret strings, or raw bytes
    try:
        if secret_raw.startswith("whsec_"):
            wh_secret = secret_raw
        else:
            base64.b64decode(secret_raw)
            wh_secret = secret_raw
    except Exception:
        wh_secret = secret_raw.encode()

    try:
        verified = Webhook(wh_secret).verify(
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

    event_type = _event_type(verified)
    logger.info(f"[dodo.webhook] type={event_type}")

    if event_type not in ("payment.succeeded", "payment_succeeded", "charge.succeeded"):
        return {"ok": True}

    obj = _get_obj(verified)
    meta = obj.get("metadata") or {}

    # Deterministic user id: must be provided in metadata
    uid = (
        (meta.get("user_uid") or meta.get("uid") or meta.get("firebase_uid") or "").strip()
        or (obj.get("user_uid") or obj.get("uid") or obj.get("firebase_uid") or "").strip()
        or (verified.get("user_uid") or verified.get("uid") or verified.get("firebase_uid") or "").strip()
    )

    if not uid:
        logger.warning("[dodo.webhook] missing user id in metadata; skip plan update")
        return {"ok": True}

    plan = _normalize_plan(meta.get("plan") or meta.get("tier") or meta.get("product_plan"))

    db = _get_fs_client()
    if not db or not fb_fs:
        logger.error("[dodo.webhook] Firestore unavailable; cannot persist plan")
        return {"ok": True}

    try:
        # Firestore users/{uid}
        db.collection("users").document(uid).set(
            {
                "uid": uid,
                "isPaid": True,
                "plan": plan,
                "planStatus": "paid",
                "lastPaymentProvider": "dodo",
                "updatedAt": fb_fs.SERVER_TIMESTAMP,
                "paidAt": fb_fs.SERVER_TIMESTAMP,
            },
            merge=True,
        )

        # Local entitlement mirror
        write_json_key(
            _entitlement_key(uid),
            {
                "isPaid": True,
                "plan": plan,
                "updatedAt": obj.get("created_at") or obj.get("paid_at") or obj.get("timestamp") or None,
            },
        )
    except Exception as ex:
        logger.warning(f"[dodo.webhook] failed to persist plan for {uid}: {ex}")
        # still ack to avoid retries storm
        return {"ok": True}

    return {"ok": True}


# 2) Checkout proxy that injects required metadata (user_uid + plan)
@router.post("/checkout")
async def dodo_create_checkout(request: Request, payload: dict = Body(...)):
    # Resolve uid either from auth or email in payload (best-effort)
    uid = get_uid_from_request(request)
    if not uid:
        email = str(
            (payload or {}).get("email")
            or ((payload or {}).get("customer") or {}).get("email")
            or ((payload or {}).get("metadata") or {}).get("email")
            or ""
        ).strip().lower()
        if email:
            try:
                uid = get_uid_by_email(email)
            except Exception:
                uid = None
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not (DODO_API_BASE and DODO_CHECKOUT_PATH and DODO_API_KEY):
        logger.error("[dodo.checkout] Dodo API not configured")
        return JSONResponse({"error": "payments unavailable"}, status_code=500)

    # Normalize plan from client and backfill from Firestore
    plan = _normalize_plan(
        (payload or {}).get("plan")
        or (payload or {}).get("tier")
        or (payload or {}).get("product_plan")
    )

    # Merge metadata with enforced fields
    out_payload = dict(payload or {})
    meta = out_payload.get("metadata") if isinstance(out_payload.get("metadata"), dict) else {}

    # Read current plan from Firestore for auditing only
    current_plan = "free"
    try:
        _db = _get_fs_client()
        if _db:
            snap = _db.collection("users").document(uid).get()
            if snap.exists:
                current_plan = str((snap.to_dict() or {}).get("plan") or current_plan)
    except Exception:
        pass

    meta.update({
        "user_uid": uid,
        "uid": uid,
        "firebase_uid": uid,
        "plan": plan,
        "current_plan": current_plan,
    })

    out_payload["metadata"] = meta

    # Forward request to Dodo
    url = f"{DODO_API_BASE}{DODO_CHECKOUT_PATH}"
    data = json.dumps(out_payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DODO_API_KEY}",
    }

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            try:
                return json.loads(raw)
            except Exception:
                return JSONResponse({"ok": True, "raw": raw})
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8", "ignore")
        logger.warning(f"[dodo.checkout] HTTP {ex.code}: {body}")
        try:
            return JSONResponse(json.loads(body), status_code=ex.code)
        except Exception:
            return JSONResponse({"error": "checkout failed", "detail": body}, status_code=ex.code)
    except Exception as ex:
        logger.exception(f"[dodo.checkout] request error: {ex}")
        return JSONResponse({"error": "checkout failed"}, status_code=500)
