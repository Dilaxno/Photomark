import os
import json
import base64
import os
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
from app.core.auth import get_fs_client as _get_fs_client, get_uid_from_request, get_uid_by_email, firebase_enabled, fb_auth

# Helper to find first plausible ID inside nested payloads

def _find_first_id(x):
    try:
        if isinstance(x, dict):
            for k in ("id","sessionId","session_id","checkout_id","payment_id","token","paymentId","order_id"):
                if k in x and isinstance(x[k], (str,int)):
                    return str(x[k])
            for v in x.values():
                rid = _find_first_id(v)
                if rid:
                    return rid
        if isinstance(x, list):
            for v in x:
                rid = _find_first_id(v)
                if rid:
                    return rid
    except Exception:
        return None
    return None


# Plan derivation helpers
_DODO_PID_PHOTOGRAPHERS = os.getenv('DODO_PHOTOGRAPHERS_PRODUCT_ID', '').strip().lower()
_DODO_PID_AGENCIES = os.getenv('DODO_AGENCIES_PRODUCT_ID', '').strip().lower()


def _scan_for_strings(obj, out: list[str]):
    try:
        if isinstance(obj, str):
            out.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _scan_for_strings(v, out)
        elif isinstance(obj, list):
            for v in obj:
                _scan_for_strings(v, out)
    except Exception:
        pass


def _derive_plan_from_payload(obj: dict, meta: dict) -> str:
    # 1) Prefer explicit metadata
    p = _normalize_plan(meta.get('plan') or meta.get('tier') or meta.get('product_plan'))
    if p:
        return p

    # 2) Try product ids in payload against env-configured product IDs
    #    Avoid scanning metadata for inference to prevent bias from fields like "current_plan".
    strings: list[str] = []
    obj_no_meta = obj
    try:
        if isinstance(obj, dict) and 'metadata' in obj:
            obj_no_meta = {k: v for k, v in obj.items() if k != 'metadata'}
    except Exception:
        obj_no_meta = obj
    _scan_for_strings(obj_no_meta, strings)
    sset = {s.strip().lower() for s in strings if isinstance(s, str)}
    if _DODO_PID_AGENCIES and _DODO_PID_AGENCIES in sset:
        return 'agencies'
    if _DODO_PID_PHOTOGRAPHERS and _DODO_PID_PHOTOGRAPHERS in sset:
        return 'photographers'

    # 3) Try names containing hints (prefer agencies if both appear)
    joined = '\n'.join(strings).lower()
    if 'agenc' in joined:  # agencies/agency
        return 'agencies'
    if 'photograph' in joined:
        return 'photographers'

    # 4) Fallback: leave unchanged to avoid wrong upgrades; caller can keep prior plan
    return ''

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
    # Reject unknown inputs; do not default to legacy 'pro'
    return ""


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

    # Deterministic user id: prefer metadata, otherwise derive by email best-effort
    uid = (
        (meta.get("user_uid") or meta.get("uid") or meta.get("firebase_uid") or "").strip()
        or (obj.get("user_uid") or obj.get("uid") or obj.get("firebase_uid") or "").strip()
        or (verified.get("user_uid") or verified.get("uid") or verified.get("firebase_uid") or "").strip()
    )

    if not uid:
        # Try mapping by email if metadata-uid is missing
        email_candidates = []
        try:
            # Common locations for email across providers
            for path in (
                ["customer","email"], ["buyer","email"], ["billing","email"], ["customer_email"], ["email"],
                ["data","customer","email"], ["data","email"], ["payment","email"], ["session","customer_email"],
            ):
                node = verified
                for key in path:
                    if isinstance(node, dict) and key in node:
                        node = node[key]
                    else:
                        node = None
                        break
                if isinstance(node, str) and "@" in node:
                    email_candidates.append(node.strip().lower())
            # Also check metadata
            for k in ("email","customer_email","payer_email","buyer_email"):
                v = meta.get(k)
                if isinstance(v, str) and "@" in v:
                    email_candidates.append(v.strip().lower())
        except Exception:
            pass
        email_candidates = [e for e in email_candidates if e]
        found_uid = None
        for em in email_candidates:
            try:
                found_uid = get_uid_by_email(em)
                if found_uid:
                    break
            except Exception:
                continue
        if found_uid:
            uid = found_uid
        else:
            # Try mapping by checkout/payment id saved during checkout creation
            map_id = _find_first_id(verified)
            mapped_uid = None
            mapped_plan = None
            try:
                if map_id:
                    m = read_json_key(f"payments/dodo/{map_id}.json") or {}
                    mapped_uid = (m.get('uid') or '').strip()
                    mapped_plan = (m.get('plan') or '').strip()
            except Exception:
                mapped_uid = None
            if not mapped_uid:
                # Firestore fallback
                try:
                    _db = _get_fs_client()
                    if _db and fb_fs and map_id:
                        snap = _db.collection('payments_dodo').document(map_id).get()
                        if snap.exists:
                            data = snap.to_dict() or {}
                            mapped_uid = (data.get('uid') or '').strip()
                            mapped_plan = (data.get('plan') or '').strip()
                except Exception:
                    mapped_uid = None
            if mapped_uid:
                uid = mapped_uid
                if not meta.get('plan') and mapped_plan:
                    meta['plan'] = mapped_plan
            else:
                logger.warning("[dodo.webhook] missing user id; could not resolve by email or mapping; skip plan update")
                return {"ok": True}

    plan = _derive_plan_from_payload(obj, meta) or _normalize_plan(meta.get("plan") or meta.get("tier") or meta.get("product_plan"))

    # Enforce allowlist: only 'photographers' or 'agencies'
    if plan not in ("photographers", "agencies"):
        raw_plan = str(meta.get("plan") or meta.get("tier") or meta.get("product_plan") or plan or "").strip()
        logger.warning(f"[dodo.webhook] unknown or unsupported plan '{raw_plan}' for uid={uid}; skipping persist")
        return {"ok": True}

    # Log resolved Firebase user details for traceability
    if firebase_enabled and fb_auth:
        try:
            _fb_user = fb_auth.get_user(uid)
            _fb_email = (getattr(_fb_user, "email", None) or "").strip()
            _fb_name = (getattr(_fb_user, "display_name", None) or "").strip()
            logger.info(f"[dodo.webhook] resolved_user: uid={uid} email={_fb_email} name={_fb_name}")
        except Exception as _ex:
            logger.debug(f"[dodo.webhook] fb user lookup failed for {uid}: {_ex}")

    db = _get_fs_client()
    if not db or not fb_fs:
        logger.error("[dodo.webhook] Firestore unavailable; cannot persist plan")
        return {"ok": True}

    try:
        # Firestore users/{uid}
        logger.info(f"[dodo.webhook] persist: uid={uid} plan={plan} event_type={event_type} event_id={_find_first_id(verified) or ''}")
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
    if not plan:
        return JSONResponse({"error": "invalid plan; must be 'photographers' or 'agencies'"}, status_code=400)

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

    # Build product_cart from products[] if missing (Dodo expects product_cart)
    try:
        if not out_payload.get("product_cart"):
            client_products = out_payload.get("products") or []
            cart: list[dict] = []
            if isinstance(client_products, list):
                for p in client_products:
                    if not isinstance(p, dict):
                        continue
                    pid = p.get("product_id") or p.get("productId") or p.get("id")
                    qty = int(p.get("quantity") or 1)
                    if pid:
                        cart.append({"product_id": str(pid), "quantity": max(1, qty)})
            if cart:
                # Dodo expects product_cart to be an array (sequence) of items
                out_payload["product_cart"] = cart
    except Exception:
        pass

    # Normalize redirect URL keys for provider compatibility
    try:
        redir = out_payload.get("redirectUrl") or out_payload.get("redirect_url")
        if redir:
            out_payload["redirectUrl"] = redir
            out_payload["redirect_url"] = redir
            out_payload.setdefault("success_url", redir)
    except Exception:
        pass

    # Ensure test mode if configured
    try:
        env_mode = (os.getenv("DODO_PAYMENTS_ENVIRONMENT", "").strip('"') or "").lower()
        if env_mode == "test" and not out_payload.get("mode"):
            out_payload["mode"] = "test"
    except Exception:
        pass

    # Forward request to Dodo
    url = f"{DODO_API_BASE}{DODO_CHECKOUT_PATH}"
    data = json.dumps(out_payload).encode("utf-8")
    origin = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or os.getenv("VITE_SITE_URL", "").strip() or os.getenv("VITE_FRONTEND_ORIGIN", "").strip()).rstrip("/")
    ua = os.getenv("DODO_HTTP_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": ua,
        "Authorization": f"Bearer {DODO_API_KEY}",
        **({"Origin": origin, "Referer": origin + "/pricing"} if origin else {}),
    }

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            obj = None
            try:
                obj = json.loads(raw)
            except Exception:
                obj = {"raw": raw}
            # Normalize response: extract a usable checkout URL and id
            def _find_first_url(x):
                try:
                    if isinstance(x, str) and x.startswith("http"):
                        return x
                    if isinstance(x, dict):
                        for k in ("checkout_url","hosted_url","url","payment_url","redirect_url","redirectUrl","hostedUrl","link"):
                            v = x.get(k)
                            u = _find_first_url(v)
                            if u:
                                return u
                        for v in x.values():
                            u = _find_first_url(v)
                            if u:
                                return u
                    if isinstance(x, list):
                        for v in x:
                            u = _find_first_url(v)
                            if u:
                                return u
                except Exception:
                    return None
                return None
            def _find_first_id(x):
                try:
                    if isinstance(x, dict):
                        for k in ("id","sessionId","session_id","checkout_id","payment_id","token"):
                            if k in x and isinstance(x[k], (str,int)):
                                return str(x[k])
                        for v in x.values():
                            rid = _find_first_id(v)
                            if rid:
                                return rid
                    if isinstance(x, list):
                        for v in x:
                            rid = _find_first_id(v)
                            if rid:
                                return rid
                except Exception:
                    return None
                return None
            url_out = _find_first_url(obj)
            id_out = _find_first_id(obj)
            result = {"ok": True}
            if url_out:
                result["checkoutUrl"] = url_out
                result["hostedUrl"] = url_out
                result["redirectUrl"] = url_out
            if id_out:
                result["id"] = id_out
                result["sessionId"] = id_out
                # Persist lightweight mapping for webhook fallback
                try:
                    write_json_key(f"payments/dodo/{id_out}.json", {"uid": uid, "plan": plan})
                except Exception:
                    pass
                try:
                    _db = _get_fs_client()
                    if _db and fb_fs:
                        _db.collection('payments_dodo').document(id_out).set({
                            'uid': uid,
                            'plan': plan,
                            'createdAt': fb_fs.SERVER_TIMESTAMP,
                        }, merge=True)
                except Exception:
                    pass
            result["raw"] = obj
            return result
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8", "ignore")
        logger.warning(f"[dodo.checkout] HTTP {ex.code}: {body}")
        try:
            return JSONResponse(json.loads(body), status_code=ex.code)
        except Exception:
            extra = {"error": "checkout failed", "detail": body}
            if ex.code == 403 and ("1010" in body or "Access denied" in body):
                extra["hint"] = "Upstream WAF blocked the server request. Ensure your server IP is allowlisted at the payment provider or use browser-like headers/origin."
            return JSONResponse(extra, status_code=ex.code)
    except Exception as ex:
        logger.exception(f"[dodo.checkout] request error: {ex}")
        return JSONResponse({"error": "checkout failed"}, status_code=500)
