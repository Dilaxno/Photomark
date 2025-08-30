from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import os

from app.core.auth import get_uid_from_request
from app.core.config import logger
from app.utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api/outreach", tags=["outreach"])  # POST /api/outreach/email


@router.post("/email")
async def send_outreach_email(
    request: Request,
    recipient_email: str = Body(..., embed=True),
    recipient_name: str = Body("", embed=True),
):
    """
    Sends a branded introduction email about Photomark to photographers/artists.
    Uses the same email template and SMTP settings (e.g., Resend SMTP via env).
    Requires authenticated user to avoid abuse.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    email = (recipient_email or "").strip()
    name = (recipient_name or "").strip()
    if not email or "@" not in email:
        return JSONResponse({"error": "Valid recipient_email required"}, status_code=400)

    try:
        app_name = os.getenv("APP_NAME", "Photomark")
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        prelaunch_url = f"{front}/#home?prelaunch=1"

        # Compose email
        subject = f"{app_name} — Built for photographers and artists"
        intro_html = (
            (f"Hi {name},<br><br>" if name else "") +
            f"I'm reaching out to introduce <b>{app_name}</b> — a toolkit designed to protect your work and save time on repetitive tasks.<br><br>"
            f"With {app_name}, you can:<br>"
            f"<ul>"
            f"<li>Bulk watermark images to safeguard your content</li>"
            f"<li>Convert and optimize formats at scale</li>"
            f"<li>Apply creative looks and style transfers in batches</li>"
            f"<li>Host private galleries and share client vaults securely</li>"
            f"</ul>"
            f"We're launching in the coming days. If you'd like early access and updates, join our pre‑launch list below.<br><br>"
            f"Thanks for your time, and wishing you great shoots ahead!"
        )

        html = render_email(
            "email_basic.html",
            title=f"Introducing {app_name}",
            intro=intro_html,
            button_label="Join pre‑launch list",
            button_url=prelaunch_url,
            footer_note="You'll get a single notification when we launch and occasional relevant updates.",
        )

        text = (
            (f"Hi {name},\n\n" if name else "") +
            f"Introducing {app_name} — a toolkit to protect your work and save time.\n\n"
            f"- Bulk watermark images\n"
            f"- Convert formats at scale\n"
            f"- Batch style transfers\n"
            f"- Private galleries and client vaults\n\n"
            f"We're launching in the coming days. Join the pre‑launch list: {prelaunch_url}\n"
        )

        logger.info(f"[outreach.email] uid={uid} to={email}")
        ok = send_email_smtp(
            email,
            subject,
            html,
            text,
            from_addr=os.getenv("MAIL_FROM_OUTREACH", os.getenv("MAIL_FROM", "no-reply@photomark.cloud")),
            reply_to=os.getenv("REPLY_TO_OUTREACH", os.getenv("MAIL_REPLY_TO", "support@photomark.cloud")),
            from_name=os.getenv("MAIL_FROM_NAME_OUTREACH", os.getenv("APP_NAME", "Photomark")),
        )
        if not ok:
            logger.error(f"[outreach.email] smtp-failed to={email}")
            return JSONResponse({"error": "Failed to send email"}, status_code=500)

        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[outreach.email] error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)