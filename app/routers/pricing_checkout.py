from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import os

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
    def _normalize_plan(plan: str) -> str:
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
    """
    Create a Dodo payment link with user_uid + plan metadata.
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

    redirect_url = (
        body.get("redirectUrl")
        or body.get("redirect_url")
        or os.getenv("PRICING_REDIRECT_URL")
        or "https://photomark.cloud/#success"
    )
    cancel_url = (
        body.get("cancelUrl")
        or body.get("cancel_url")
        or os.getenv("PRICING_CANCEL_URL")
        or "https://photomark.cloud/#pricing"
    )
    return_url = (
        body.get("returnUrl")
        or body.get("return_url")
        or os.getenv("PRICING_RETURN_URL")
        or redirect_url
    )

    allowed = _allowed_plans()
    if plan not in allowed:
        return JSONResponse(
            {"error": "unsupported_plan", "allowed": sorted(list(allowed))},
            status_code=400,
        )

    product_id = _plan_to_product_id(plan)
    if not product_id:
        return JSONResponse(
            {"error": "product_id_not_configured", "plan": plan}, status_code=500
        )

    api_key = (os.getenv("DODO_PAYMENTS_API_KEY") or os.getenv("DODO_API_KEY") or "").strip()
    if not api_key:
        return JSONResponse({"error": "missing_api_key"}, status_code=500)

    business_id = (os.getenv("DODO_BUSINESS_ID") or "").strip()
    brand_id = (os.getenv("DODO_BRAND_ID") or "").strip()

    email = _get_user_email(uid)
    # --- Payload ---
    payload = {
        "business_id": business_id or None,
        "brand_id": brand_id or None,
        "product_cart": [{"product_id": product_id, "quantity": qty}],
        "metadata": {"user_uid": uid, "plan": plan},
        "redirect_url": redirect_url,
        "return_url": return_url,
        "cancel_url": cancel_url,
        "client_reference_id": uid,
        "reference_id": uid,
        "external_id": uid,
    }

    if email:
        payload["customer"] = {"email": email}
        payload["customer_email"] = email

    # --- Create checkout link ---
    from app.utils.dodo import create_checkout_link

    link, details = await create_checkout_link([payload])
    if link:
        return {"url": link, "product_id": product_id, "link_kind": "url"}

    logger.warning(f"[pricing.link] failed to create payment link: {details}")
    return JSONResponse(
        {"error": "link_creation_failed", "details": details}, status_code=502
    )