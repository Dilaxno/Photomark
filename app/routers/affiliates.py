from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import os

from app.core.auth import get_uid_from_request
from app.core.config import logger
from app.utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api/affiliates", tags=["affiliates"]) 


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
        ok = send_email_smtp(email, subject, html, text)
        if not ok:
            logger.error(f"[affiliates.invite] smtp-failed to={email}")
            return JSONResponse({"error": "Failed to send email"}, status_code=500)
        logger.info(f"[affiliates.invite] success to={email} uid={uid}")
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[affiliates.invite] error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)