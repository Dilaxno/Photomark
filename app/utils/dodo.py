import os
import httpx
from typing import Dict, Any, Optional, Tuple
from app.core.config import logger, DODO_API_BASE, DODO_CHECKOUT_PATH, DODO_API_KEY

# Build standard headers list including variants used across integrations
def build_headers_list() -> list[dict]:
    api_key = (DODO_API_KEY or "").strip()
    headers_list = [
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
        {"X-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"},
    ]
    # Optional environment/business/brand
    business_id = (os.getenv("DODO_BUSINESS_ID") or "").strip()
    brand_id = (os.getenv("DODO_BRAND_ID") or "").strip()
    env_hdr = (os.getenv("DODO_PAYMENTS_ENVIRONMENT") or os.getenv("DODO_ENV") or "").strip().strip('"')
    for h in headers_list:
        if business_id:
            h["Dodo-Business-Id"] = business_id
        if brand_id:
            h["Dodo-Brand-Id"] = brand_id
        if env_hdr:
            h["Dodo-Environment"] = env_hdr
    return headers_list


def build_endpoints() -> list[str]:
    base = (DODO_API_BASE or "https://api.dodopayments.com").rstrip("/")
    path = (DODO_CHECKOUT_PATH or "/v1/payment-links").strip()
    if not path.startswith("/"):
        path = "/" + path
    return [
        f"{base}{path}",
        f"{base}/checkouts",
        f"{base}/v1/checkouts",
        f"{base}/v1/checkout/session",
        f"{base}/checkout/session",
        f"{base}/v1/checkout/sessions",
        f"{base}/v1/payment-links",
        f"{base}/payment-links",
        f"{base}/api/payment-links",
        f"{base}/v1/payment_links",
        f"{base}/v1/payment-links/create",
        f"{base}/payment-links/create",
        f"{base}/v1/checkout",
        f"{base}/checkout",
    ]


def pick_checkout_url(data: Dict[str, Any]) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    link = data.get("checkout_url") or data.get("url") or data.get("payment_link")
    if link:
        return str(link)
    obj = data.get("data") if isinstance(data, dict) else None
    if isinstance(obj, dict):
        return str(obj.get("checkout_url") or obj.get("url") or obj.get("payment_link") or "") or None
    return None


async def create_checkout_link(payloads: list[dict]) -> Tuple[Optional[str], Optional[dict]]:
    """Try multiple endpoints, header variants, and payload shapes to create a checkout link.
    Returns (link, error_details). If link is None, error_details contains last failure.
    """
    endpoints = build_endpoints()
    headers_list = build_headers_list()

    # Add success redirect URL to all payloads
    redirect_url = "https://photomark.cloud"
    updated_payloads = []
    for p in payloads:
        new_p = p.copy()
        # Common naming patterns for payment providers
        new_p.setdefault("success_url", redirect_url)
        new_p.setdefault("return_url", redirect_url)
        new_p.setdefault("redirect_url", redirect_url)
        updated_payloads.append(new_p)
        # Some endpoints (e.g., /checkout) require a nested payment_details object
        try:
            pd: dict = {}
            # Include common fields inside payment_details
            for k in ("product_cart", "metadata", "redirect_url", "return_url", "cancel_url"):
                if k in new_p and new_p[k] is not None:
                    pd[k] = new_p[k]
            # Pass through customer/email hints
            if "customer" in new_p and isinstance(new_p.get("customer"), dict):
                pd["customer"] = new_p["customer"]
            if "email" in new_p:
                pd["email"] = new_p["email"]
            if "customer_email" in new_p:
                pd["customer_email"] = new_p["customer_email"]
            wrapper: dict = {}
            # Preserve business/brand ids at top-level if present
            for k in ("business_id", "brand_id"):
                if k in new_p and new_p[k] is not None:
                    wrapper[k] = new_p[k]
            # Preserve reference identifiers at top-level
            for k in ("client_reference_id", "reference_id", "external_id"):
                if k in new_p and new_p[k] is not None:
                    wrapper[k] = new_p[k]
            wrapper["payment_details"] = pd
            updated_payloads.append(wrapper)
        except Exception:
            # If building wrapper fails, continue with base variants only
            pass

    last_error = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in endpoints:
            for headers in headers_list:
                for payload in updated_payloads:
                    try:
                        logger.info(f"[dodo] creating payment link via {url} with headers {list(headers.keys())}")
                        resp = await client.post(url, headers=headers, json=payload)
                        if resp.status_code in (200, 201):
                            try:
                                data = resp.json()
                            except Exception:
                                data = {}
                            link = pick_checkout_url(data)
                            if link:
                                logger.info("[dodo] created payment link successfully")
                                return link, None
                        try:
                            body_text = resp.text
                        except Exception:
                            body_text = ""
                        last_error = {"status": resp.status_code, "endpoint": url, "payload_keys": list(payload.keys()), "body": body_text[:2000]}
                    except Exception as ex:
                        last_error = {"exception": str(ex), "endpoint": url, "payload_keys": list(payload.keys())}
    return None, last_error