from typing import List, Optional, Dict, Set
import os
import secrets
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.config import s3, R2_BUCKET, logger
from app.utils.storage import read_json_key, write_json_key
from app.core.auth import get_uid_from_request, get_user_email_from_uid
from app.utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api", tags=["updates"])  # separate tag for clarity

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
UPDATES_INDEX_KEY = "updates/index.json"


class UpdateCreate(BaseModel):
    title: str
    description: str
    version: Optional[str] = None
    tags: Optional[List[str]] = None
    type: Optional[str] = None  # 'feature' | 'improvement' | 'fix'
    date: Optional[str] = None  # ISO date; default now


# Storage helpers

def _read_updates() -> List[dict]:
    try:
        data = read_json_key(UPDATES_INDEX_KEY)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and data.get("items"):
            items = data.get("items")
            return items if isinstance(items, list) else []
    except Exception as ex:
        logger.warning(f"read updates failed: {ex}")
    return []


def _write_updates(items: List[dict]):
    # Persist as a flat list for simplicity
    try:
        write_json_key(UPDATES_INDEX_KEY, items or [])
    except Exception as ex:
        logger.warning(f"write updates failed: {ex}")
        raise


# Broadcast helpers

def _list_all_uids() -> Set[str]:
    uids: Set[str] = set()
    try:
        prefix = "users/"
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            for obj in bucket.objects.filter(Prefix=prefix):
                key = obj.key
                # Expect keys like users/{uid}/...
                parts = (key or "").split("/")
                if len(parts) >= 2 and parts[0] == "users" and parts[1]:
                    uids.add(parts[1])
        else:
            base = os.path.join(STATIC_DIR, "users")
            if os.path.isdir(base):
                for entry in os.listdir(base):
                    path = os.path.join(base, entry)
                    if os.path.isdir(path):
                        uids.add(entry)
    except Exception as ex:
        logger.warning(f"list uids failed: {ex}")
    return uids


def _broadcast_update_email(item: dict) -> int:
    try:
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        link = f"{front}/#whats-new"
        title = str(item.get("title") or "What's New")
        desc = str(item.get("description") or "")
        subject = f"What's New: {title}"
        intro = (f"<strong>{title}</strong><br>" + desc) if desc else f"<strong>{title}</strong>"
        html = render_email(
            "email_basic.html",
            title="New update available",
            intro=intro,
            button_label="See what's new",
            button_url=link,
        )
        text = f"{title}\n{desc}\nOpen: {link}"

        count = 0
        uids = _list_all_uids()
        sent_to: Set[str] = set()
        for uid in uids:
            try:
                email = (get_user_email_from_uid(uid) or "").strip()
                if not email or email in sent_to:
                    continue
                if send_email_smtp(email, subject, html, text):
                    count += 1
                    sent_to.add(email)
            except Exception:
                continue
        return count
    except Exception as ex:
        logger.warning(f"broadcast email failed: {ex}")
        return 0


@router.get("/updates")
async def updates_list():
    try:
        items = _read_updates()
        # Sort by date desc if present
        try:
            items.sort(key=lambda x: (x.get("date") or ""), reverse=True)
        except Exception:
            pass
        return {"items": items}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/updates/create")
async def updates_create(request: Request, payload: UpdateCreate):
    # Require authentication for creating updates
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        items = _read_updates()
        now_iso = datetime.utcnow().isoformat()
        item = {
            "id": secrets.token_urlsafe(8),
            "date": (payload.date or now_iso),
            "title": payload.title,
            "description": payload.description,
            "version": payload.version or "",
            "tags": payload.tags or [],
            "type": (payload.type or "").strip() or None,
        }
        # Prepend newest
        items = [item] + items
        _write_updates(items)

        # Broadcast to all users (best-effort)
        sent = _broadcast_update_email(item)
        return {"ok": True, "id": item["id"], "sent": sent}
    except Exception as ex:
        logger.warning(f"updates_create failed: {ex}")
        return JSONResponse({"error": "create_failed"}, status_code=500)
