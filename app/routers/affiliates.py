from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import os
from datetime import datetime

from app.core.auth import get_uid_from_request
from app.core.config import logger
from app.utils.emailing import render_email, send_email_smtp
from app.utils.storage import read_json_key, write_json_key

router = APIRouter(prefix="/api/affiliates", tags=["affiliates"]) 


def _stats_key(affiliate_uid: str) -> str:
    return f"affiliates/{affiliate_uid}/stats.json"


def _attrib_key(user_uid: str) -> str:
    # Which affiliate referred this user
    return f"affiliates/attributions/{user_uid}.json"


def _extract_affiliate_uid(ref_code: str) -> str | None:
    # Our ref codes are either "<slug>-<uid>" or just "<uid>"
    rc = (ref_code or "").strip()
    if not rc:
        return None
    parts = rc.split("-")
    cand = parts[-1]
    return cand or None


@router.get("/ping")
async def affiliates_ping(request: Request):
    """Quick check that the affiliates router is mounted and reachable."""
    client_ip = request.client.host if request.client else "?"
    logger.info(f"[affiliates.ping] from={client_ip}")
    return {"ok": True}


@router.post("/invite")
async def affiliates_invite(request: Request, email: str = Body(..., embed=True), channel: str = Body("", embed=True)):
    # Require authenticated user to prevent abuse
    uid = get_uid_from_request(request)
    client_ip = request.client.host if request.client else "?"
    logger.info(f"[affiliates.invite] start uid={uid or '-'} ip={client_ip} email={email} channel={channel}")

    if not uid:
        logger.warning(f"[affiliates.invite] unauthorized ip={client_ip}")
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    email = (email or "").strip()
    if not email or "@" not in email:
        logger.warning(f"[affiliates.invite] invalid-email uid={uid} email={email}")
        return JSONResponse({"error": "Valid email required"}, status_code=400)

    try:
        app_name = os.getenv("APP_NAME", "Photomark")
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")

        # Compose email content (plain, non-promotional tone)
        safe_channel = (channel or "").strip()
        subject = "Collaboration Proposal"

        # Build HTML using the neutral template without CTA button
        intro_html = (
            f"Hi{(' ' + safe_channel) if safe_channel else ''},<br><br>"
            f"I wanted to introduce you to <b>{app_name}</b> — an all‑in‑one platform for photographers, graphic designers, and digital artists to manage, protect, and deliver their work efficiently.<br><br>"
            f"{app_name} makes it easy to:<br>"
            f"<ul>"
            f"<li>Bulk watermark images to protect your content</li>"
            f"<li>Apply creative style transformations in batches</li>"
            f"<li>Convert image formats at scale</li>"
            f"<li>Host your work in a secure, private cloud gallery</li>"
            f"</ul>"
            f"You can also create password‑protected vaults for clients, embed your gallery into your personal site, and collaborate with teammates using role‑based access.<br><br>"
            f"I believe your audience would find real value in this, which is why I’d like to invite you to join our 40% affiliate partnership. We offer:<br>"
            f"<ul>"
            f"<li>Fast weekly payouts</li>"
            f"<li>A custom dashboard to track your earnings</li>"
            f"<li>A product that solves practical problems for creative communities</li>"
            f"</ul>"
            f"You can explore the platform firsthand and see how it fits with your content. If this sounds like a fit, I’d be happy to get you set up this week.<br><br>"
            f"Looking forward to hearing your thoughts.<br><br>"
            f"Best regards,<br>"
            f"Soufiane"
        )

        html = render_email(
            "email_basic.html",
            title="Collaboration Proposal",
            intro=intro_html,
            button_label="Explore Photomark",
            button_url="https://photomark.cloud",
        )

        text = (
            f"Hi{(' ' + safe_channel) if safe_channel else ''},\n\n"
            f"I wanted to introduce you to {app_name} — an all-in-one platform for photographers, graphic designers, and digital artists to manage, protect, and deliver their work efficiently.\n\n"
            f"{app_name} makes it easy to:\n"
            f"- Bulk watermark images to protect your content\n"
            f"- Apply creative style transformations in batches\n"
            f"- Convert image formats at scale\n"
            f"- Host your work in a secure, private cloud gallery\n\n"
            f"You can also create password-protected vaults for clients, embed your gallery into your personal site, and collaborate with teammates using role-based access.\n\n"
            f"I believe your audience would find real value in this, which is why I’d like to invite you to join our 40% affiliate partnership. We offer:\n"
            f"- Fast weekly payouts\n"
            f"- A custom dashboard to track your earnings\n"
            f"- A product that solves practical problems for creative communities\n\n"
            f"You can explore the platform firsthand and see how it fits with your content. If this sounds like a fit, I’d be happy to get you set up this week.\n\n"
            f"Looking forward to hearing your thoughts.\n\n"
            f"Best regards,\n"
            f"Soufiane\n"
        )

        logger.info(f"[affiliates.invite] sending to={email} uid={uid}")
        ok = send_email_smtp(
            email,
            subject,
            html,
            text,
            from_addr=os.getenv("MAIL_FROM_AFFILIATES", "affiliates@photomark.cloud"),
            reply_to=os.getenv("REPLY_TO_AFFILIATES", "affiliates@photomark.cloud"),
            from_name=os.getenv("MAIL_FROM_NAME_AFFILIATES", "Photomark Partnerships"),
        )
        if not ok:
            logger.error(f"[affiliates.invite] smtp-failed to={email}")
            return JSONResponse({"error": "Failed to send email"}, status_code=500)
        logger.info(f"[affiliates.invite] success to={email} uid={uid}")
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[affiliates.invite] error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/track/click")
async def affiliates_track_click(payload: dict = Body(...)):
    """Record a click for a referral code. Public endpoint."""
    ref = str(payload.get("ref") or "").strip()
    uid = _extract_affiliate_uid(ref)
    if not uid:
        return JSONResponse({"error": "invalid ref"}, status_code=400)
    try:
        stats = read_json_key(_stats_key(uid)) or {}
        stats["clicks"] = int(stats.get("clicks") or 0) + 1
        stats["last_click_at"] = datetime.utcnow().isoformat()
        write_json_key(_stats_key(uid), stats)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[affiliates.track.click] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/track/signup")
async def affiliates_track_signup(payload: dict = Body(...)):
    """Attribute a signup to a referral code and update counters. Public endpoint."""
    ref = str(payload.get("ref") or "").strip()
    new_user_uid = str(payload.get("new_user_uid") or "").strip()
    if not ref or not new_user_uid:
        return JSONResponse({"error": "missing fields"}, status_code=400)
    affiliate_uid = _extract_affiliate_uid(ref)
    if not affiliate_uid:
        return JSONResponse({"error": "invalid ref"}, status_code=400)
    try:
        # Record attribution (idempotent overwrite)
        write_json_key(_attrib_key(new_user_uid), {
            "affiliate_uid": affiliate_uid,
            "attributed_at": datetime.utcnow().isoformat(),
            "ref": ref,
        })
        # Aggregate signup count
        stats = read_json_key(_stats_key(affiliate_uid)) or {}
        stats["signups"] = int(stats.get("signups") or 0) + 1
        stats["last_signup_at"] = datetime.utcnow().isoformat()
        write_json_key(_stats_key(affiliate_uid), stats)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[affiliates.track.signup] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.get("/stats")
async def affiliates_stats(request: Request):
    """Return aggregated stats for the authenticated affiliate."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        stats = read_json_key(_stats_key(uid)) or {}
        # Fill defaults so the dashboard can render cleanly
        return {
            "clicks": int(stats.get("clicks") or 0),
            "signups": int(stats.get("signups") or 0),
            "conversions": int(stats.get("conversions") or 0),
            "gross_cents": int(stats.get("gross_cents") or 0),
            "payout_cents": int(stats.get("payout_cents") or 0),
            "currency": (stats.get("currency") or "usd").lower(),
            "last_click_at": stats.get("last_click_at"),
            "last_signup_at": stats.get("last_signup_at"),
            "last_conversion_at": stats.get("last_conversion_at"),
        }
    except Exception as ex:
        logger.exception(f"[affiliates.stats] {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)