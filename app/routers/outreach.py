from fastapi import APIRouter, Request, Body, UploadFile, File
from fastapi.responses import JSONResponse
import os
from typing import List, Optional

from app.core.auth import get_uid_from_request
from app.core.config import logger
from app.utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api/outreach", tags=["outreach"])  # POST /api/outreach/email


def _compose_intro(app_name: str, name: Optional[str]) -> tuple[str, str, str, str]:
    # Use the exact copy requested by user; static link to incoming page
    prelaunch_url = "https://photomark.cloud/#incoming"

    subject = "A small tool I built for photographers"
    intro_html = (
        (f"Hi {name},<br><br>" if name else "Hi,<br><br>") +
        "I’ve been working on something I think you’ll find useful. It’s called <b>Photomark</b> — a simple toolkit I built to help photographers and artists protect and share their work without spending hours on repetitive edits.<br><br>"
        "With it, you can:" "<ul>"
        "<li>Quickly watermark and batch-process your images</li>"
        "<li>Convert formats and optimize files in one go</li>"
        "<li>Apply creative looks across entire shoots</li>"
        "<li>Even host private client galleries when you need to share securely</li>"
        "</ul>"
        "I’m opening early access soon, and I’d love to hear what you think. If you’d like me to send you the invite when it’s ready, you can join the pre-launch list here: "
        f"<a href=\"{prelaunch_url}\">{prelaunch_url}</a><br><br>"
        "No spam, just a quick note once it’s live.<br><br>"
        "Wishing you good light and great shoots,<br>"
        "Marouane"
    )
    text_plain = (
        (f"Hi {name},\n\n" if name else "Hi,\n\n") +
        "I’ve been working on something I think you’ll find useful. It’s called Photomark — a simple toolkit I built to help photographers and artists protect and share their work without spending hours on repetitive edits.\n\n"
        "With it, you can:\n"
        "- Quickly watermark and batch-process your images\n"
        "- Convert formats and optimize files in one go\n"
        "- Apply creative looks across entire shoots\n"
        "- Even host private client galleries when you need to share securely\n\n"
        "I’m opening early access soon, and I’d love to hear what you think. If you’d like me to send you the invite when it’s ready, you can join the pre-launch list here: "
        f"{prelaunch_url}\n\n"
        "No spam, just a quick note once it’s live.\n\n"
        "Wishing you good light and great shoots,\n"
        "Marouane"
    )
    return subject, intro_html, prelaunch_url, text_plain


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
        subject, intro_html, prelaunch_url, text_plain = _compose_intro(app_name, name)

        html = render_email(
            "email_basic.html",
            title=subject,
            intro=intro_html,
            # No CTA button for this copy
            button_label=None,
            button_url=None,
            footer_note=None,
        )

        text = text_plain

        logger.info(f"[outreach.email] uid={uid} to={email}")
        ok = send_email_smtp(
            email,
            subject,
            html,
            text,
            from_addr=os.getenv("MAIL_FROM_OUTREACH", "Marouane@photomark.cloud"),
            reply_to=os.getenv("REPLY_TO_OUTREACH", os.getenv("MAIL_REPLY_TO", "Marouane@photomark.cloud")),
            from_name=os.getenv("MAIL_FROM_NAME_OUTREACH", "Marouane"),
        )
        if not ok:
            logger.error(f"[outreach.email] smtp-failed to={email}")
            return JSONResponse({"error": "Failed to send email"}, status_code=500)

        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[outreach.email] error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


class BulkPayloadItem(dict):
    email: str
    name: Optional[str]


@router.post("/bulk")
async def send_outreach_bulk(
    request: Request,
    payload: dict = Body(...),
):
    """Accepts JSON { entries: [{ email, name? }, ...] } and sends personalized emails.
    Auth required. Returns counts and per-row errors when applicable.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    entries: List[dict] = list((payload or {}).get("entries") or [])
    if not isinstance(entries, list) or not entries:
        return JSONResponse({"error": "entries[] required"}, status_code=400)

    app_name = os.getenv("APP_NAME", "Photomark")
    sent, failed = 0, 0
    errors: List[dict] = []

    for idx, rec in enumerate(entries):
        try:
            email = str((rec or {}).get("email") or "").strip()
            name = str((rec or {}).get("name") or "").strip()
            if not email or "@" not in email:
                failed += 1
                errors.append({"index": idx, "email": email, "error": "invalid email"})
                continue

            subject, intro_html, prelaunch_url, text_plain = _compose_intro(app_name, name)
            html = render_email(
                "email_basic.html",
                title=subject,
                intro=intro_html,
                button_label=None,
                button_url=None,
                footer_note=None,
            )
            text = text_plain

            ok = send_email_smtp(
                email,
                subject,
                html,
                text,
                from_addr=os.getenv("MAIL_FROM_OUTREACH", "Marouane@photomark.cloud"),
                reply_to=os.getenv("REPLY_TO_OUTREACH", os.getenv("MAIL_REPLY_TO", "Marouane@photomark.cloud")),
                from_name=os.getenv("MAIL_FROM_NAME_OUTREACH", "Marouane"),
            )
            if ok:
                sent += 1
            else:
                failed += 1
                errors.append({"index": idx, "email": email, "error": "smtp failed"})
        except Exception as ex:
            failed += 1
            errors.append({"index": idx, "email": str((rec or {}).get("email") or ""), "error": str(ex)})

    return {"ok": True, "sent": sent, "failed": failed, "errors": errors or None}