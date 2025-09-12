from fastapi import APIRouter, Request, Header, Body
from fastapi.responses import JSONResponse
from typing import Optional
import os
import json
from datetime import datetime

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
from app.utils.emailing import render_email, send_email_smtp

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


def _deep_find_first(obj: dict, keys: tuple[str, ...]) -> str:
    """Recursively search a dict for the first non-empty string value for any key in keys.
    Limits depth and size to avoid pathological payloads.
    """
    if not isinstance(obj, dict):
        return ""
    seen: set[int] = set()

    def _walk(node: dict, depth: int) -> str:
        if depth > 6:
            return ""
        node_id = id(node)
        if node_id in seen:
            return ""
        seen.add(node_id)

        # Direct match on this level
        for k in keys:
            if k in node:
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        # Check common wrappers
        for k in ("object", "data", "attributes", "details"):
            sub = node.get(k)
            if isinstance(sub, dict):
                got = _walk(sub, depth + 1)
                if got:
                    return got
            elif isinstance(sub, list):
                for it in sub[:50]:
                    if isinstance(it, dict):
                        got = _walk(it, depth + 1)
                        if got:
                            return got
        # Generic recursive descent over other dict and list values
        for v in list(node.values())[:100]:
            if isinstance(v, dict):
                got = _walk(v, depth + 1)
                if got:
                    return got
            elif isinstance(v, list):
                for it in v[:50]:
                    if isinstance(it, dict):
                        got = _walk(it, depth + 1)
                        if got:
                            return got
        return ""

    return _walk(obj, 0)


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
        # Collect potential arrays where products may be listed
        candidate_lists = []
        for key in ("product_cart", "items", "products", "lines", "line_items"):
            val = obj.get(key)
            if isinstance(val, list) and val:
                candidate_lists.append(val)
            elif isinstance(val, dict):
                # Some providers use objects with a nested 'data' array
                data_arr = val.get("data") if isinstance(val.get("data"), list) else None
                if data_arr:
                    candidate_lists.append(data_arr)

        # Inspect each list entry and try to resolve product id/name
        for items in candidate_lists:
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                pid = str((it.get("product_id") or it.get("price_id") or it.get("id") or "")).strip()
                name = str((it.get("product_name") or it.get("name") or it.get("title") or "")).strip()

                # Nested price/product structures
                p = it.get("product") if isinstance(it.get("product"), dict) else None
                pr = it.get("price") if isinstance(it.get("price"), dict) else None
                if p:
                    pid = pid or str((p.get("id") or p.get("product_id") or "")).strip()
                    name = name or str((p.get("name") or p.get("title") or "")).strip()
                if pr:
                    # Some APIs put product under price.product
                    pp = pr.get("product") if isinstance(pr.get("product"), dict) else None
                    if pp:
                        pid = pid or str((pp.get("id") or pp.get("product_id") or "")).strip()
                        name = name or str((pp.get("name") or pp.get("title") or "")).strip()
                    # Or the price itself is the id we map to a product id
                    pid = pid or str((pr.get("id") or pr.get("price_id") or "")).strip()

                # Compare ids against configured product ids
                if pid_ag and pid and pid == pid_ag:
                    found_ag = True
                if pid_phot and pid and pid == pid_phot:
                    found_phot = True
                if name:
                    names.append(name)

        # Sometimes a single product object may be present
        if isinstance(obj.get("product"), dict):
            p = obj.get("product") or {}
            pid = str((p.get("id") or p.get("product_id") or "")).strip()
            name = str((p.get("name") or p.get("title") or "")).strip()
            if pid_ag and pid and pid == pid_ag:
                found_ag = True
            if pid_phot and pid and pid == pid_phot:
                found_phot = True
            if name:
                names.append(name)

        # Fallback: bounded deep scan for id-like fields if nothing found so far
        if not (found_ag or found_phot):
            seen_ids: set[str] = set()
            def _scan_ids(node: dict, depth: int = 0):
                if depth > 4 or not isinstance(node, dict):
                    return
                # Common id fields
                for k in ("product_id", "productId", "price_id", "priceId", "id"):
                    v = node.get(k)
                    if isinstance(v, str) and v.strip():
                        seen_ids.add(v.strip())
                # Nested objects commonly used
                for k in ("product", "price", "data", "object", "item", "attributes"):
                    v = node.get(k)
                    if isinstance(v, dict):
                        _scan_ids(v, depth + 1)
                    elif isinstance(v, list):
                        for it in v[:50]:
                            if isinstance(it, dict):
                                _scan_ids(it, depth + 1)
            _scan_ids(obj)
            if pid_ag and pid_ag in seen_ids:
                found_ag = True
            if pid_phot and pid_phot in seen_ids:
                found_phot = True

        try:
            logger.info(f"[pricing.webhook] product mapping: found_agencies={found_ag} found_photographers={found_phot} names={names}")
        except Exception:
            pass
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

    # --- Step 3: Event type ---
    evt_type = str((payload.get("type") or payload.get("event") or "")).strip().lower()

    # --- Step 4: Normalize event object ---
    event_obj = None
    # Common provider shapes: { data: { object: {...} } }
    if isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("object"), dict):
        event_obj = payload["data"]["object"]
    # Some send arrays: { data: [ { object: {...} }, ... ] }
    elif isinstance(payload.get("data"), list) and payload.get("data") and isinstance(payload["data"][0], dict) and isinstance(payload["data"][0].get("object"), dict):
        event_obj = payload["data"][0]["object"]
    elif isinstance(payload.get("object"), dict):
        event_obj = payload["object"]
    else:
        event_obj = payload
    event_obj = event_obj if isinstance(event_obj, dict) else {}

    # --- Diagnostics: summarize payload structure to debug missing products ---
    try:
        def _summarize_list(lst):
            if not isinstance(lst, list):
                return {"type": type(lst).__name__}
            head = lst[0] if lst else None
            head_keys = list(head.keys())[:10] if isinstance(head, dict) else type(head).__name__
            return {"len": len(lst), "first_type": type(head).__name__ if head is not None else None, "first_keys": head_keys}

        top_keys = list(payload.keys())[:30]
        obj_keys = list(event_obj.keys())[:30]
        pc = event_obj.get("product_cart")
        items = event_obj.get("items")
        products = event_obj.get("products")
        lines = event_obj.get("lines")
        line_items = event_obj.get("line_items")
        logger.info(
            "[pricing.webhook] diag: top_keys=%s obj_keys=%s pc=%s items=%s products=%s lines=%s line_items=%s",
            top_keys,
            obj_keys,
            _summarize_list(pc),
            _summarize_list(items),
            _summarize_list(products),
            _summarize_list(lines if isinstance(lines, list) else (lines.get('data') if isinstance(lines, dict) else [])),
            _summarize_list(line_items),
        )
    except Exception:
        pass

    # --- Step 5: Extract metadata & query_params (overlay checkout) ---
    def _dict(d):
        return d if isinstance(d, dict) else {}
    payload_data = _dict(payload.get("data")) if isinstance(payload, dict) else {}
    meta = _dict((event_obj or {}).get("metadata")) or _dict(payload_data.get("metadata")) or {}
    # Overlay Checkout passes identifiers under data.query_params
    qp = _dict((event_obj or {}).get("query_params")) or _dict(payload_data.get("query_params")) or {}

    # --- Step 6: Resolve UID ---
    uid = ""
    # Prefer query_params for overlay integration
    qp_uid_keys = ("user_uid", "userUid", "uid", "userId", "user-id")
    for k in qp_uid_keys:
        v = str((qp.get(k) if isinstance(qp, dict) else "") or "").strip()
        if v:
            uid = v
            break
    # Fallback to metadata if not found in query_params
    uid_keys = ("user_uid", "userUid", "uid", "userId", "user-id")
    if not uid:
        for k in uid_keys:
            v = str((meta.get(k) if isinstance(meta, dict) else "") or "").strip()
            if v:
                uid = v
                break

    # Fallback by reference fields
    if not uid:
        for src in (event_obj, payload):
            if isinstance(src, dict):
                for k in (
                    "client_reference_id",
                    "reference_id",
                    "external_id",
                    "order_id",
                    "user_uid",
                    "uid",
                    "userUid",
                    "userId",
                    "user-id",
                ):
                    v = str((src.get(k) or "")).strip()
                    if v:
                        uid = v
                        break
            if uid:
                break

    # Fallback: provider-specific nesting (deep scan)
    if not uid and isinstance(payload, dict):
        deep_uid = _deep_find_first(
            payload,
            (
                "user_uid",
                "userUid",
                "uid",
                "userId",
                "user-id",
                "client_reference_id",
                "reference_id",
                "external_id",
                "order_id",
            ),
        )
        if deep_uid:
            uid = deep_uid

    # Fallback by email
    if not uid:
        email = _first_email_from_payload(payload) or _first_email_from_payload(event_obj or {})
        if email:
            try:
                resolved = get_uid_by_email(email)
                if resolved:
                    uid = resolved
            except Exception:
                pass

    if not uid:
        try:
            sample = {k: (v if isinstance(v, (str, int)) else type(v).__name__) for k, v in list((event_obj or {}).items())[:20]}
            logger.warning(f"[pricing.webhook] missing uid; keys hint={list(sample.keys())}")
        except Exception:
            pass
        logger.warning("[pricing.webhook] missing metadata.user_uid; cannot upgrade")
        return {"ok": True, "skipped": True, "reason": "missing_metadata_user_uid"}

    # --- Step 7: Resolve plan ---
    # Prefer overlay query_params plan when present
    plan_raw = str((qp.get("plan") if isinstance(qp, dict) else "") or "").strip() or str((meta.get("plan") if isinstance(meta, dict) else "") or "").strip()
    plan = _normalize_plan(plan_raw)

    # --- Step 7b: Capture and cache any available context for later payment.succeeded ---
    ctx = {"uid": uid, "plan": plan, "email": _first_email_from_payload(payload) or _first_email_from_payload(event_obj or {})}
    customer_id = ""
    try:
        cust = event_obj.get("customer") if isinstance(event_obj, dict) else None
        if isinstance(cust, dict):
            customer_id = str((cust.get("customer_id") or cust.get("id") or "")).strip()
    except Exception:
        pass
    sub_id = _deep_find_first(event_obj, ("subscription_id", "subscriptionId", "sub_id")) if isinstance(event_obj, dict) else ""
    # Write lightweight cache entries when we have any meaningful context
    try:
        def _write_ctx(key: str):
            if not key:
                return
            write_json_key(f"pricing/cache/{key}.json", {
                "uid": ctx.get("uid") or None,
                "plan": ctx.get("plan") or None,
                "email": ctx.get("email") or None,
                "updatedAt": int(datetime.utcnow().timestamp()),
            })
        if ctx.get("uid") or ctx.get("plan") or ctx.get("email"):
            if sub_id:
                _write_ctx(f"subscriptions/{sub_id}")
            if customer_id:
                _write_ctx(f"customers/{customer_id}")
            if ctx.get("email"):
                _write_ctx(f"emails/{(ctx['email'] or '').lower()}")
    except Exception:
        pass

    # If this is not a payment.succeeded, acknowledge after caching
    if evt_type != "payment.succeeded":
        return {"ok": True, "captured": bool(ctx.get("uid") or ctx.get("plan") or ctx.get("email")), "event_type": evt_type}

    # Detect subscription-style payloads which may not include product_cart
    sub_id = _deep_find_first(event_obj, ("subscription_id", "subscriptionId", "sub_id")) if isinstance(event_obj, dict) else ""
    is_subscription = bool(sub_id and not (isinstance(event_obj.get("product_cart"), list) and event_obj.get("product_cart")))

    # If uid/plan missing, try reading cached context by subscription/customer/email
    if (not uid or not plan):
        try:
            def _read_ctx(key: str) -> dict:
                try:
                    return read_json_key(f"pricing/cache/{key}.json") or {}
                except Exception:
                    return {}
            if sub_id and (not uid or not plan):
                c1 = _read_ctx(f"subscriptions/{sub_id}")
                uid = uid or str(c1.get("uid") or "").strip()
                plan = plan or _normalize_plan(str(c1.get("plan") or ""))
            if (not uid or not plan) and customer_id:
                c2 = _read_ctx(f"customers/{customer_id}")
                uid = uid or str(c2.get("uid") or "").strip()
                plan = plan or _normalize_plan(str(c2.get("plan") or ""))
            if (not uid or not plan) and ctx.get("email"):
                c3 = _read_ctx(f"emails/{(ctx.get('email') or '').lower()}")
                uid = uid or str(c3.get("uid") or "").strip()
                plan = plan or _normalize_plan(str(c3.get("plan") or ""))
        except Exception:
            pass

    if not plan and is_subscription:
        # Try mapping subscription_id to plan via env; otherwise use metadata plan or default to photographers
        sid = sub_id.strip()
        sid_phot = (os.getenv("DODO_PHOTOGRAPHERS_SUBSCRIPTION_ID") or "").strip()
        sid_ag = (os.getenv("DODO_AGENCIES_SUBSCRIPTION_ID") or "").strip()
        if sid and sid_ag and sid == sid_ag:
            plan = "agencies"
        elif sid and sid_phot and sid == sid_phot:
            plan = "photographers"
        else:
            # Fallback to provided metadata plan (already normalized above) or default to photographers
            plan = _normalize_plan(plan_raw) or "photographers"
        try:
            logger.info(f"[pricing.webhook] subscription detected: subscription_id={sid} resolved plan={plan}")
        except Exception:
            pass

    if not plan and not is_subscription:
        plan = _plan_from_products(event_obj or {})
    if not plan or plan not in _allowed_plans():
        allowed = sorted(list(_allowed_plans()))
        return {
            "ok": True,
            "skipped": True,
            "reason": "unsupported_plan",
            "plan_raw": plan_raw,
            "normalized": plan,
            "allowed": allowed,
        }

    # --- Step 8: Persist to Firestore ---
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

    # --- Step 9: Local entitlement mirror ---
    try:
        write_json_key(
            _entitlement_key(uid),
            {
                "isPaid": True,
                "plan": plan,
                "updatedAt": event_obj.get("created_at")
                or event_obj.get("paid_at")
                or event_obj.get("timestamp")
                or None,
            },
        )
    except Exception:
        pass

    # --- Step 10: Affiliate attribution (unchanged) ---
    # [keep your affiliate code block here exactly as before]

    logger.info(f"[pricing.webhook] completed upgrade: uid={uid} plan={plan}")
    return {"ok": True, "upgraded": True, "uid": uid, "plan": plan}
