from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import os
import httpx
from typing import Optional

from app.core.config import logger
from app.core.auth import (
    get_fs_client as _get_fs_client,
    get_uid_from_request,
    firebase_enabled,
    fb_auth,
)

try:
    from firebase_admin import firestore as fb_fs  # type: ignore
except Exception:
    fb_fs = None  # type: ignore

# Reuse normalization/allow-list logic from webhook module
try:
    from app.routers.pricing_webhook import _normalize_plan, _allowed_plans  # type: ignore
except Exception:
    # Minimal fallbacks
    def _normalize_plan(plan: Optional[str]) -> str:
        p = (plan or "").strip().lower().replace("_", " ").replace("-", " ")
        if p.endswith(" plan"):
            p = p[:-5]
        if p.endswith(" plans"):
            p = p[:-6]
        if "photograph" in p or p in ("photo", "pg", "p"):
            return "photographers"
        if "agenc" in p or p in ("ag",):
            return "agencies"
        return ""

    def _allowed_plans() -> set[str]:
        return {"photographers", "agencies"}

router = APIRouter(prefix="/api/pricing", tags=["pricing"])


def _plan_to_product_id(plan: str) -> str:
    if plan == "photographers":
        return (os.getenv("DODO_PHOTOGRAPHERS_PRODUCT_ID") or "").strip()
    if plan == "agencies":
        return (os.getenv("DODO_AGENCIES_PRODUCT_ID") or "").strip()
    return ""


def _get_user_email(uid: str) -> str:
    # Prefer Firestore document email; fallback to Firebase Auth
    try:
        db = _get_fs_client()
        if db and fb_fs:
            snap = db.collection("users").document(uid).get()
            if getattr(snap, "exists", False):
                data = snap.to_dict() or {}
                email = str(data.get("email") or "").strip()
                if email:
                    return email
    except Exception:
        pass
    if firebase_enabled and fb_auth:
        try:
            user = fb_auth.get_user(uid)
            return (getattr(user, "email", None) or "").strip()
        except Exception:
            return ""
    return ""


@router.post("/link")
async def create_pricing_link(request: Request):
    """Create a Dodo payment link with user_uid embedded in metadata.

    Request JSON: { plan: "photographers" | "agencies", quantity?: 1 }
    Response: { url }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}

    plan = _normalize_plan((body.get("plan") if isinstance(body, dict) else "") or "")
    qty = int((body.get("quantity") if isinstance(body, dict) else 1) or 1)
    qty = 1 if qty <= 0 else qty

    # Optional redirect/cancel/return URLs (with sensible defaults)
    redirect_url = str(
        (body.get("redirectUrl") if isinstance(body, dict) else None)
        or (body.get("redirect_url") if isinstance(body, dict) else None)
        or os.getenv("PRICING_REDIRECT_URL")
        or "https://photomark.cloud/#success"
    )
    cancel_url = str(
        (body.get("cancelUrl") if isinstance(body, dict) else None)
        or (body.get("cancel_url") if isinstance(body, dict) else None)
        or os.getenv("PRICING_CANCEL_URL")
        or "https://photomark.cloud/#pricing"
    )
    return_url = str(
        (body.get("returnUrl") if isinstance(body, dict) else None)
        or (body.get("return_url") if isinstance(body, dict) else None)
        or os.getenv("PRICING_RETURN_URL")
        or redirect_url
    )

    allowed = _allowed_plans()
    if plan not in allowed:
        return JSONResponse({"error": "unsupported_plan", "allowed": sorted(list(allowed))}, status_code=400)

    product_id = _plan_to_product_id(plan)
    if not product_id:
        logger.warning(f"[pricing.link] missing product id for plan='{plan}'. Check DODO_*_PRODUCT_ID env vars.")
        return JSONResponse({"error": "product_id_not_configured", "plan": plan}, status_code=500)

    api_base = (os.getenv("DODO_API_BASE") or "https://api.dodopayments.com").rstrip("/")
    api_key = (os.getenv("DODO_PAYMENTS_API_KEY") or os.getenv("DODO_API_KEY") or "").strip()
    if not api_key:
        logger.warning("[pricing.link] missing API key (DODO_PAYMENTS_API_KEY/DODO_API_KEY)")
        return JSONResponse({"error": "missing_api_key"}, status_code=500)

    # Dodo requires business_id in creation payloads; brand_id optional
    business_id = (os.getenv("DODO_BUSINESS_ID") or "").strip()
    brand_id = (os.getenv("DODO_BRAND_ID") or "").strip()

    # Build base payload and alternates using common Dodo structures (business/brand optional)
    common_top = {**({"business_id": business_id} if business_id else {}), **({"brand_id": brand_id} if brand_id else {})}
    base_payload = {
        **common_top,
        "metadata": {
            "user_uid": uid,
            "plan": plan,
        },
        # Surface identifiers in query params for overlay checkouts (providers often echo these back to webhooks)
        "query_params": {"user_uid": uid, "plan": plan},
        # Add common naming variants some providers expect
        "query": {"user_uid": uid, "plan": plan},
        "params": {"user_uid": uid, "plan": plan},
        "product_cart": [
            {"product_id": product_id, "quantity": qty},
        ],
        "redirect_url": redirect_url,
        "return_url": return_url,
        "cancel_url": cancel_url,
    }

    # Add customer email if available (helps with receipts and receipts)
    email = _get_user_email(uid)
    if email:
        base_payload["customer"] = {"email": email}
        base_payload["email"] = email
        base_payload["customer_email"] = email

    # Add common reference identifiers to aid webhook user resolution
    ref_fields = {"client_reference_id": uid, "reference_id": uid, "external_id": uid}
    # Also add to base payload
    try:
        base_payload.update(ref_fields)
    except Exception:
        pass

    # Prepare alternate payload shapes (start with the minimal unified schema)
    alt_payloads = [
        {
            # Minimal payload recommended by unified /checkouts API
            **ref_fields,
            "product_cart": [{"product_id": product_id, "quantity": qty}],
            "metadata": {"user_uid": uid, "plan": plan},
            "query_params": {"user_uid": uid, "plan": plan},
            "query": {"user_uid": uid, "plan": plan},
            "params": {"user_uid": uid, "plan": plan},
            **({"email": email, "customer_email": email} if email else {}),
        },
        base_payload,
        {
            # Overlay checkout: items array
            **common_top,
            **ref_fields,
            "metadata": base_payload["metadata"],
            "query_params": {"user_uid": uid, "plan": plan},
            "query": {"user_uid": uid, "plan": plan},
            "params": {"user_uid": uid, "plan": plan},
            "items": [{"product_id": product_id, "quantity": qty}],
            "redirect_url": redirect_url,
            "cancel_url": cancel_url,
            **({"customer": {"email": email}, "email": email, "customer_email": email} if email else {}),
        },
        {
            # Snake_case products array
            **common_top,
            **ref_fields,
            "metadata": base_payload["metadata"],
            "query_params": {"user_uid": uid, "plan": plan},
            "query": {"user_uid": uid, "plan": plan},
            "params": {"user_uid": uid, "plan": plan},
            "products": [{"product_id": product_id, "quantity": qty}],
            "redirect_url": redirect_url,
            "cancel_url": cancel_url,
            **({"customer": {"email": email}, "email": email, "customer_email": email} if email else {}),
        },
        {
            # Single product id + quantity
            **common_top,
            **ref_fields,
            "metadata": base_payload["metadata"],
            "query_params": {"user_uid": uid, "plan": plan},
            "query": {"user_uid": uid, "plan": plan},
            "params": {"user_uid": uid, "plan": plan},
            "product": {"id": product_id},
            "quantity": qty,
            "redirect_url": redirect_url,
            "cancel_url": cancel_url,
            **({"customer": {"email": email}, "email": email, "customer_email": email} if email else {}),
        },
        {
            # Some APIs expect price_id instead of product_id
            **common_top,
            **ref_fields,
            "metadata": base_payload["metadata"],
            "query_params": {"user_uid": uid, "plan": plan},
            "query": {"user_uid": uid, "plan": plan},
            "params": {"user_uid": uid, "plan": plan},
            "price_id": product_id,
            "quantity": qty,
            "redirect_url": redirect_url,
            "cancel_url": cancel_url,
            **({"customer": {"email": email}, "email": email, "customer_email": email} if email else {}),
        },
    ]

    # Use shared Dodo helper for link creation
    from app.utils.dodo import create_checkout_link

    try:
        logger.info(
            f"[pricing.link] creating link: plan={plan} product_id_set={bool(product_id)} api_base='{api_base}' business_id_set={bool(business_id)} brand_id_set={bool(brand_id)}"
        )
    except Exception:
        pass

    link, details = await create_checkout_link(alt_payloads)
    if link:
        # Persist lightweight context so webhook can recover uid/plan if provider omits metadata
        try:
            from app.utils.storage import write_json_key
            code = link.rsplit('/', 1)[-1]
            write_json_key(
                f"pricing/cache/links/{code}.json",
                {
                    "uid": uid,
                    "plan": plan,
                    "email": _get_user_email(uid),
                },
            )
        except Exception:
            pass
        return {"url": link, "product_id": product_id, "link_kind": "url"}
    logger.warning(f"[pricing.link] failed to create payment link: {details}")
    return JSONResponse({"error": "link_creation_failed", "details": details}, status_code=502)


@router.post("/session")
async def create_pricing_session(request: Request):
    """Create a Dodo checkout SESSION (server-side) and return a session_url for full-page redirect.

    Request JSON: { plan: "photographers" | "agencies", returnUrl?: string, cancelUrl?: string }
    Response: { session_url }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}

    plan = _normalize_plan((body.get("plan") if isinstance(body, dict) else "") or "")
    allowed = _allowed_plans()
    if plan not in allowed:
        return JSONResponse({"error": "unsupported_plan", "allowed": sorted(list(allowed))}, status_code=400)

    qty = 1
    return_url = str(
        (body.get("returnUrl") if isinstance(body, dict) else None)
        or (body.get("return_url") if isinstance(body, dict) else None)
        or os.getenv("PRICING_RETURN_URL")
        or "https://photomark.cloud/#success"
    )
    cancel_url = str(
        (body.get("cancelUrl") if isinstance(body, dict) else None)
        or (body.get("cancel_url") if isinstance(body, dict) else None)
        or os.getenv("PRICING_CANCEL_URL")
        or "https://photomark.cloud/#pricing"
    )

    product_id = _plan_to_product_id(plan)
    if not product_id:
        logger.warning(f"[pricing.session] missing product id for plan='{plan}'. Check DODO_*_PRODUCT_ID env vars.")
        return JSONResponse({"error": "product_id_not_configured", "plan": plan}, status_code=500)

    # Build payloads leaning toward session-based endpoints first
    email = _get_user_email(uid)
    meta = {"user_uid": uid, "plan": plan}
    qp = {"user_uid": uid, "plan": plan}
    ref_fields = {"client_reference_id": uid, "reference_id": uid, "external_id": uid}

    base = {
        **ref_fields,
        "metadata": meta,
        "query_params": qp,
        "query": qp,
        "params": qp,
        "product_cart": [{"product_id": product_id, "quantity": qty}],
        "return_url": return_url,
        "cancel_url": cancel_url,
        **({"customer": {"email": email}, "email": email, "customer_email": email} if email else {}),
    }
    alt_payloads = [
        base,
        {**ref_fields, "metadata": meta, "query_params": qp, "query": qp, "params": qp, "products": [{"product_id": product_id, "quantity": qty}], "return_url": return_url, "cancel_url": cancel_url, **({"customer": {"email": email}, "email": email, "customer_email": email} if email else {})},
        {**ref_fields, "metadata": meta, "query_params": qp, "query": qp, "params": qp, "items": [{"product_id": product_id, "quantity": qty}], "return_url": return_url, "cancel_url": cancel_url, **({"customer": {"email": email}, "email": email, "customer_email": email} if email else {})},
        {**ref_fields, "metadata": meta, "query_params": qp, "query": qp, "params": qp, "product": {"id": product_id}, "quantity": qty, "return_url": return_url, "cancel_url": cancel_url, **({"customer": {"email": email}, "email": email, "customer_email": email} if email else {})},
        {**ref_fields, "metadata": meta, "query_params": qp, "query": qp, "params": qp, "price_id": product_id, "quantity": qty, "return_url": return_url, "cancel_url": cancel_url, **({"customer": {"email": email}, "email": email, "customer_email": email} if email else {})},
    ]

    from app.utils.dodo import create_checkout_link, pick_checkout_url

    url, details = await create_checkout_link(alt_payloads)
    if not url:
        logger.warning(f"[pricing.session] failed to create session: {details}")
        return JSONResponse({"error": "session_creation_failed", "details": details}, status_code=502)

    # Try to label as session_url for clarity
    return {"session_url": url, "plan": plan}


@router.get("/link/photographers")
async def link_photographers(request: Request):
    # Convenience GET route
    return await create_pricing_link(Request({
        "type": request.scope.get("type"),
        "http_version": request.scope.get("http_version"),
        "method": "POST",
        "headers": request.scope.get("headers"),
        "path": request.scope.get("path"),
        "raw_path": request.scope.get("raw_path"),
        "query_string": request.scope.get("query_string"),
        "server": request.scope.get("server"),
        "client": request.scope.get("client"),
        "scheme": request.scope.get("scheme"),
        "root_path": request.scope.get("root_path"),
        "app": request.scope.get("app"),
        "router": request.scope.get("router"),
        "endpoint": request.scope.get("endpoint"),
        "route": request.scope.get("route"),
        "state": request.scope.get("state"),
        "asgi": request.scope.get("asgi"),
        "extensions": request.scope.get("extensions"),
        "user": getattr(request, "user", None),
        "session": getattr(request, "session", None),
        "_body": b'{"plan":"photographers"}',
    }))


@router.get("/link/agencies")
async def link_agencies(request: Request):
    return await create_pricing_link(Request({
        "type": request.scope.get("type"),
        "http_version": request.scope.get("http_version"),
        "method": "POST",
        "headers": request.scope.get("headers"),
        "path": request.scope.get("path"),
        "raw_path": request.scope.get("raw_path"),
        "query_string": request.scope.get("query_string"),
        "server": request.scope.get("server"),
        "client": request.scope.get("client"),
        "scheme": request.scope.get("scheme"),
        "root_path": request.scope.get("root_path"),
        "app": request.scope.get("app"),
        "router": request.scope.get("router"),
        "endpoint": request.scope.get("endpoint"),
        "route": request.scope.get("route"),
        "state": request.scope.get("state"),
        "asgi": request.scope.get("asgi"),
        "extensions": request.scope.get("extensions"),
        "user": getattr(request, "user", None),
        "session": getattr(request, "session", None),
        "_body": b'{"plan":"agencies"}',
    }))
