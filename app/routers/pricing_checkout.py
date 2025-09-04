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

    # Optional redirect/cancel URLs (with sensible defaults)
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

    allowed = _allowed_plans()
    if plan not in allowed:
        return JSONResponse({"error": "unsupported_plan", "allowed": sorted(list(allowed))}, status_code=400)

    product_id = _plan_to_product_id(plan)
    if not product_id:
        return JSONResponse({"error": "product_id_not_configured", "plan": plan}, status_code=500)

    api_base = (os.getenv("DODO_API_BASE") or "https://api.dodopayments.com").rstrip("/")
    api_key = (os.getenv("DODO_PAYMENTS_API_KEY") or os.getenv("DODO_API_KEY") or "").strip()
    if not api_key:
        return JSONResponse({"error": "missing_api_key"}, status_code=500)

    # Dodo requires business_id in creation payloads; brand_id optional
    business_id = (os.getenv("DODO_BUSINESS_ID") or "").strip()
    brand_id = (os.getenv("DODO_BRAND_ID") or "").strip()
    if not business_id:
        return JSONResponse({"error": "missing_business_id"}, status_code=500)

    # Build base payload and alternates using common Dodo structures
    common_top = {"business_id": business_id, **({"brand_id": brand_id} if brand_id else {})}
    base_payload = {
        **common_top,
        "metadata": {
            "user_uid": uid,
            "plan": plan,
        },
        "product_cart": [
            {"product_id": product_id, "quantity": qty},
        ],
        "redirect_url": redirect_url,
        "cancel_url": cancel_url,
    }

    # Add customer email if available (helps with receipts and receipts)
    email = _get_user_email(uid)
    if email:
        base_payload["customer"] = {"email": email}

    # Prepare alternate payload shapes (prefer documented overlay checkout schema)
    alt_payloads = [
        base_payload,
        {
            # Overlay checkout: items array
            **common_top,
            "metadata": base_payload["metadata"],
            "items": [{"product_id": product_id, "quantity": qty}],
            "redirect_url": redirect_url,
            "cancel_url": cancel_url,
            **({"customer": {"email": email}} if email else {}),
        },
        {
            # Snake_case products array
            **common_top,
            "metadata": base_payload["metadata"],
            "products": [{"product_id": product_id, "quantity": qty}],
            "redirect_url": redirect_url,
            "cancel_url": cancel_url,
            **({"customer": {"email": email}} if email else {}),
        },
        {
            # Single product id + quantity
            **common_top,
            "metadata": base_payload["metadata"],
            "product": {"id": product_id},
            "quantity": qty,
            "redirect_url": redirect_url,
            "cancel_url": cancel_url,
            **({"customer": {"email": email}} if email else {}),
        },
        {
            # Some APIs expect price_id instead of product_id
            **common_top,
            "metadata": base_payload["metadata"],
            "price_id": product_id,
            "quantity": qty,
            "redirect_url": redirect_url,
            "cancel_url": cancel_url,
            **({"customer": {"email": email}} if email else {}),
        },
    ]

    # Try a range of endpoints (prefer overlay checkout session first)
    endpoints = [
        f"{api_base}/v1/checkout/session",
        f"{api_base}/checkout/session",
        f"{api_base}/v1/payment-links",
        f"{api_base}/payment-links",
        f"{api_base}/api/payment-links",
        f"{api_base}/v1/payment_links",
        f"{api_base}/v1/payment-links/create",
        f"{api_base}/payment-links/create",
        f"{api_base}/v1/checkout/sessions",
        f"{api_base}/v1/checkout",
        f"{api_base}/checkout",
    ]

    headers_list = [
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {"X-API-KEY": api_key, "Content-Type": "application/json"},
    ]
    # Also include business/brand in headers for providers that expect them there
    for h in headers_list:
        h["Dodo-Business-Id"] = business_id
        if brand_id:
            h["Dodo-Brand-Id"] = brand_id

    # Include environment header if provided (e.g., "test")
    env_hdr = (os.getenv("DODO_PAYMENTS_ENVIRONMENT") or os.getenv("DODO_ENV") or "").strip().strip('"')
    if env_hdr:
        for h in headers_list:
            h["Dodo-Environment"] = env_hdr

    last_error = None
    async with httpx.AsyncClient(timeout=20.0) as client:
        for url in endpoints:
            for headers in headers_list:
                for payload in alt_payloads:
                    try:
                        logger.info(f"[pricing.link] creating payment link via {url} with headers {list(headers.keys())}")
                        resp = await client.post(url, headers=headers, json=payload)
                        if resp.status_code in (200, 201):
                            data = resp.json()
                            # Common possible fields
                            link = (
                                (data.get("payment_link") if isinstance(data, dict) else None)
                                or (data.get("url") if isinstance(data, dict) else None)
                                or (data.get("checkout_url") if isinstance(data, dict) else None)
                            )
                            if link:
                                logger.info("[pricing.link] created payment link successfully")
                                return {"url": link}
                            # Some APIs wrap the object
                            obj = data.get("data") if isinstance(data, dict) else None
                            if isinstance(obj, dict):
                                link = obj.get("payment_link") or obj.get("url") or obj.get("checkout_url")
                                if link:
                                    logger.info("[pricing.link] created payment link successfully (wrapped)")
                                    return {"url": link}
                        # Store error for diagnostics
                        try:
                            body_text = resp.text
                        except Exception:
                            body_text = ""
                        last_error = {"status": resp.status_code, "endpoint": url, "payload_keys": list(payload.keys()), "body": body_text[:2000]}
                    except Exception as ex:
                        last_error = {"exception": str(ex), "endpoint": url, "payload_keys": list(payload.keys())}

    logger.warning(f"[pricing.link] failed to create payment link: {last_error}")
    return JSONResponse({"error": "link_creation_failed", "details": last_error}, status_code=502)


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
