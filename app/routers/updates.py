from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from typing import List, Optional
from datetime import datetime
import os
import secrets

from app.core.auth import require_admin, firebase_enabled, fb_auth
from app.core.config import logger, ADMIN_EMAILS
from app.utils.storage import read_json_key, write_json_key
from app.utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api/updates", tags=["updates"]) 

INDEX_KEY = "updates/index.json"
ITEM_KEY_FMT = "updates/items/{id}.json"


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _gen_id() -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    rand = secrets.token_hex(3)
    return f"{ts}-{rand}"


def _read_index() -> dict:
    return read_json_key(INDEX_KEY) or {"items": []}


def _write_index(idx: dict):
    idx["updated_at"] = _now_iso()
    write_json_key(INDEX_KEY, idx)


def _collect_all_user_emails(limit: Optional[int] = None) -> List[str]:
    emails: List[str] = []
    if not firebase_enabled or not fb_auth:
        logger.warning("Firebase not enabled; cannot collect user emails")
        return emails
    try:
        iterator = fb_auth.list_users()
        for user in iterator.iterate_all():  # type: ignore[attr-defined]
            email = (getattr(user, "email", None) or "").strip().lower()
            if email:
                emails.append(email)
                if limit and len(emails) >= limit:
                    break
    except Exception as ex:
        logger.exception(f"Collect users failed: {ex}")
    return emails


@router.get("")
async def list_updates():
    idx = _read_index()
    # Return newest first
    items = list(sorted(idx.get("items", []), key=lambda x: x.get("date" ,""), reverse=True))
    return {"items": items}


@router.post("")
async def create_update(request: Request, payload: dict = Body(...)):
    # Admin only
    ok, who = require_admin(request, ADMIN_EMAILS)
    if not ok:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    title = str((payload or {}).get("title") or "").strip()
    description = str((payload or {}).get("description") or "").strip()
    version = str((payload or {}).get("version") or "").strip() or None
    tags = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()] or None
    ptype = str((payload or {}).get("type") or "").strip() or None

    if not title or not description:
        return JSONResponse({"error": "title and description required"}, status_code=400)

    item_id = _gen_id()
    date_iso = _now_iso()
    item = {
        "id": item_id,
        "date": date_iso[:10],  # YYYY-MM-DD for UI
        "title": title,
        "description": description,
        "version": version,
        "tags": tags,
        "type": ptype,
        "created_at": date_iso,
        "created_by": who,
    }

    # Persist item and index
    write_json_key(ITEM_KEY_FMT.format(id=item_id), item)
    idx = _read_index()
    idx_items = idx.get("items") or []
    idx_items.append(item)
    idx["items"] = idx_items
    _write_index(idx)

    # Send branded email to all users
    try:
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        updates_url = f"{front}/#whatsnew"
        subject = f"New in Photomark: {title}"
        html = render_email(
            "email_basic.html",
            title=title,
            intro=f"<strong>What's new</strong><br>{description}",
            button_label="See what's new",
            button_url=updates_url,
            footer_note="You're receiving this because you have a Photomark account.",
        )
        text = f"What's new: {title}\n\n{description}\n\nSee details: {updates_url}"

        limit_env = os.getenv("UPDATES_EMAIL_LIMIT", "").strip()
        limit = int(limit_env) if limit_env.isdigit() else None
        recipients = _collect_all_user_emails(limit)

        sent_count = 0
        for email in recipients:
            if send_email_smtp(email, subject, html, text):
                sent_count += 1
        logger.info(f"Update {item_id} emailed to {sent_count}/{len(recipients)} users")
    except Exception as ex:
        logger.exception(f"Update email broadcast failed: {ex}")

    return {"ok": True, "item": item}