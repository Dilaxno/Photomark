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

        # Compose email content
        safe_channel = (channel or "").strip()
        subject = f"{app_name} x {safe_channel or 'YouTuber'} — 40% Affiliate Partnership"

        # Simple pitch with HTML; rendered via email_basic.html template
        intro_html = (
            f"Hi{(' ' + safe_channel) if safe_channel else ''},<br><br>"
            f"I'm reaching out from <b>{app_name}</b>. We help creators watermark, convert, retouch, and style photos in seconds — built for speed and quality." \
            f"<br><br>Why this aligns with your audience:" \
            f"<ul>" \
            f"<li>Fast watermarking and bulk conversion (PNG/JPG/WebP/AVIF)</li>" \
            f"<li>AI-powered background retouch and style transfer</li>" \
            f"<li>Collaboration and secure sharing for teams and clients</li>" \
            f"<li>Simple web app — no heavy software</li>" \
            f"</ul>" \
            f"We’d love to invite you to our <b>40% revenue-share affiliate program</b>. If interested, reply to this email and we’ll get you set up right away." \
            f"<br><br>"
        )

        html = render_email(
            "email_basic.html",
            title=f"Partner with {app_name}",
            intro=intro_html,
            button_label="Explore Photomark",
            button_url=f"{front}/#software",
            footer_note="Reply to this email to join the affiliate program (40% revenue share).",
        )
        text = (
            f"Hi {safe_channel or ''},\n\n"
            f"We help creators watermark, convert, retouch, and style photos.\n"
            f"Why it aligns with your audience:\n"
            f"- Fast watermarking and bulk conversion (PNG/JPG/WebP/AVIF)\n"
            f"- AI background retouch and style transfer\n"
            f"- Collaboration and secure sharing\n"
            f"- Simple web app\n\n"
            f"We’d love to invite you to our 40% revenue-share affiliate program.\n"
            f"Explore: {front}/#software\n"
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