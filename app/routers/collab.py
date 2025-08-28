from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, status
from typing import Optional, List, Dict, Any
import io
import os
import json
from datetime import datetime as _dt
from PIL import Image

from app.core.config import logger, COLLAB_MAX_IMAGE_MB, COLLAB_ALLOWED_EXTS, COLLAB_RATE_LIMIT_WINDOW_SEC, COLLAB_RATE_LIMIT_MAX_ACTIONS, COLLAB_MAX_RECIPIENTS
from app.core.auth import get_uid_from_request, get_uid_by_email, get_user_email_from_uid
from app.utils.emailing import render_email, send_email_smtp
from app.utils.storage import upload_bytes, read_json_key, write_json_key, read_bytes_key

router = APIRouter(prefix="/api/collab", tags=["collab"])


def _normalize_email(e: str) -> str:
    return (e or "").strip().lower()


def _friendly_err(msg: str, code: int = status.HTTP_400_BAD_REQUEST):
    raise HTTPException(status_code=code, detail={"error": msg})


def _rate_key(uid: str) -> str:
    return f"users/{uid}/collab/rate.json"


def _recent_key(uid: str) -> str:
    return f"users/{uid}/collab/recent_recipients.json"


def _incr_rate(uid: str):
    now = int(_dt.utcnow().timestamp())
    # read current
    rec = read_json_key(_rate_key(uid)) or {}
    ws = int(rec.get("window_start_ts") or 0)
    cnt = int(rec.get("count") or 0)
    # new window?
    if now - ws > COLLAB_RATE_LIMIT_WINDOW_SEC:
        ws = now
        cnt = 0
    cnt += 1
    write_json_key(_rate_key(uid), {"window_start_ts": ws, "count": cnt})
    if cnt > COLLAB_RATE_LIMIT_MAX_ACTIONS:
        _friendly_err("Rate limit exceeded. Please try again later.", status.HTTP_429_TOO_MANY_REQUESTS)


def _record_recent(uid: str, emails: List[str]):
    emails = [e for e in (emails or []) if e]
    if not emails:
        return
    rec = read_json_key(_recent_key(uid)) or {"emails": []}
    cur = [str(x).lower().strip() for x in (rec.get("emails") or []) if str(x).strip()]
    for e in emails:
        if e in cur:
            # move to front
            cur.remove(e)
        cur.insert(0, e)
    cur = cur[:50]
    write_json_key(_recent_key(uid), {"emails": cur, "updated_at": _dt.utcnow().isoformat()})


def _ext_ok(ext: str) -> bool:
    ext = (ext or "").lower()
    return ext in set(COLLAB_ALLOWED_EXTS or [])


def _validate_upload(filename: str, size: int):
    if not filename:
        _friendly_err("Missing file name")
    ext = os.path.splitext(filename)[1] or ""
    if not _ext_ok(ext):
        _friendly_err(f"Unsupported format {ext or '(none)'}")
    max_bytes = COLLAB_MAX_IMAGE_MB * 1024 * 1024
    if size > max_bytes:
        _friendly_err(f"File too large. Limit is {COLLAB_MAX_IMAGE_MB} MB.")


@router.post("/send-to-friend")
async def send_to_friend(
    request: Request,
    friend_email: str = Form(...),
    file: UploadFile = File(...),
    note: Optional[str] = Form(None),
):
    # compatibility wrapper for single recipient
    return await send_to_friends(request, friend_emails=friend_email, file=file, note=note)

@router.post("/send-to-friends")
async def send_to_friends(
    request: Request,
    friend_emails: str = Form(...),  # comma-separated
    file: UploadFile = File(...),
    note: Optional[str] = Form(None),
):
    sender_uid = get_uid_from_request(request)
    if not sender_uid:
        _friendly_err("Unauthorized", status.HTTP_401_UNAUTHORIZED)

    # rate limit: one action counts as 1 regardless of recipients
    _incr_rate(sender_uid)

    emails = [
        _normalize_email(e)
        for e in (friend_emails or "").split(",")
        if _normalize_email(e)
    ]
    if not emails:
        _friendly_err("At least one recipient is required")
    if len(emails) > COLLAB_MAX_RECIPIENTS:
        _friendly_err(f"Too many recipients. Limit is {COLLAB_MAX_RECIPIENTS}")

    # Validate upload now (size/format)
    _validate_upload(file.filename or "image", getattr(file, "size", 0) or 0)
    raw = await file.read()
    if not raw:
        _friendly_err("Empty file")

    fname = file.filename or "image"
    orig_ext = (os.path.splitext(fname)[1] or '.jpg').lower()
    if not _ext_ok(orig_ext):
        _friendly_err(f"Unsupported format {orig_ext}")
    ct_map = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
        '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff'
    }
    orig_ct = ct_map.get(orig_ext, 'application/octet-stream')

    # Re-encode to JPEG once for reuse
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95, subsampling=0, progressive=True, optimize=True)
    buf.seek(0)

    date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
    base = os.path.splitext(os.path.basename(fname))[0][:100] or 'image'
    stamp = int(_dt.utcnow().timestamp())

    results: List[Dict[str, Any]] = []

    for email in emails:
        try:
            friend_uid = get_uid_by_email(email)
            if not friend_uid:
                results.append({"email": email, "ok": False, "error": "Friend not found"})
                continue

            original_key = f"users/{friend_uid}/originals/{date_prefix}/{base}-{stamp}-fromfriend-orig{orig_ext}"
            original_url = upload_bytes(original_key, raw, content_type=orig_ct)

            oext_token = (orig_ext.lstrip('.') or 'jpg').lower()
            key = f"users/{friend_uid}/watermarked/{date_prefix}/{base}-{stamp}-fromfriend-o{oext_token}.jpg"
            url = upload_bytes(key, buf.getvalue(), content_type='image/jpeg')

            try:
                sender_email = get_user_email_from_uid(sender_uid) or "a friend"
                gallery_link = os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip().rstrip("/") + "#/gallery"
                html = render_email(
                    "email_basic.html",
                    title="You received a photo",
                    intro=f"<p>You received a photo from <b>{sender_email}</b> to your gallery.</p>" + (f"<p>Note: {note}</p>" if note else ""),
                    button_url=gallery_link,
                    button_label="Open your gallery",
                    footer_note="If you weren't expecting this, you can ignore this message.",
                )
                send_email_smtp(email, "New photo received", html)
            except Exception as ex:
                logger.warning(f"Email notify failed for {email}: {ex}")

            results.append({"email": email, "ok": True, "key": key, "url": url, "original_key": original_key, "original_url": original_url})
        except Exception as ex:
            logger.exception(f"send_to_friends error for {email}: {ex}")
            results.append({"email": email, "ok": False, "error": "Internal error"})

    _record_recent(sender_uid, emails)

    return {"ok": True, "results": results}


@router.get("/recent-recipients")
async def recent_recipients(request: Request):
    uid = get_uid_from_request(request)
    if not uid:
        _friendly_err("Unauthorized", status.HTTP_401_UNAUTHORIZED)
    rec = read_json_key(_recent_key(uid)) or {"emails": []}
    return {"emails": rec.get("emails") or []}


@router.post("/send-existing")
async def send_existing(
    request: Request,
    friend_emails: str = Form(...),
    keys: str = Form(...),  # JSON array of keys
    note: Optional[str] = Form(None),
):
    uid = get_uid_from_request(request)
    if not uid:
        _friendly_err("Unauthorized", status.HTTP_401_UNAUTHORIZED)

    _incr_rate(uid)

    emails = [
        _normalize_email(e)
        for e in (friend_emails or "").split(",")
        if _normalize_email(e)
    ]
    if not emails:
        _friendly_err("At least one recipient is required")
    if len(emails) > COLLAB_MAX_RECIPIENTS:
        _friendly_err(f"Too many recipients. Limit is {COLLAB_MAX_RECIPIENTS}")

    try:
        src_keys = json.loads(keys or "[]")
    except Exception:
        _friendly_err("Invalid keys payload")
    if not isinstance(src_keys, list) or not src_keys:
        _friendly_err("No items selected")

    results: List[Dict[str, Any]] = []

    for email in emails:
        friend_uid = get_uid_by_email(email)
        if not friend_uid:
            results.append({"email": email, "ok": False, "error": "Friend not found"})
            continue

        per_email = {"email": email, "ok": True, "items": []}

        for k in src_keys:
            try:
                # read watermarked bytes
                data = read_bytes_key(k)
                if not data:
                    per_email["items"].append({"key": k, "ok": False, "error": "Not found"})
                    continue

                base_name = os.path.basename(k)
                # ext token defaults
                orig_token = "jpg"
                if "-o" in base_name:
                    orig_token = base_name.split("-o")[-1].split(".")[0].lower() or "jpg"
                date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
                # keep readable name, trim token suffix
                name = os.path.splitext(base_name)[0]
                stamp = int(_dt.utcnow().timestamp())

                # save gallery jpeg for friend
                dest_key = f"users/{friend_uid}/watermarked/{date_prefix}/{name}-{stamp}-fromfriend.jpg"
                dest_url = upload_bytes(dest_key, data, content_type='image/jpeg')

                # save original – try derived original path
                candidate_orig = k.replace('/watermarked/', '/originals/').rsplit('-o', 1)[0]
                candidate_orig = f"{candidate_orig}-orig.{orig_token}"
                orig_bytes = read_bytes_key(candidate_orig) or data
                orig_ct = {
                    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'heic': 'image/heic', 'tif': 'image/tiff', 'tiff': 'image/tiff'
                }.get(orig_token, 'application/octet-stream')
                dest_orig_key = f"users/{friend_uid}/originals/{date_prefix}/{name}-{stamp}-fromfriend-orig.{orig_token}"
                dest_orig_url = upload_bytes(dest_orig_key, orig_bytes, content_type=orig_ct)

                per_email["items"].append({"key": k, "ok": True, "dest_key": dest_key, "dest_url": dest_url, "dest_original_key": dest_orig_key, "dest_original_url": dest_orig_url})
            except Exception as ex:
                logger.warning(f"send-existing failed for {k}: {ex}")
                per_email["items"].append({"key": k, "ok": False, "error": "Internal error"})

        results.append(per_email)

    try:
        if emails:
            sender_email = get_user_email_from_uid(uid) or "a friend"
            gallery_link = os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip().rstrip("/") + "#/gallery"
            html = render_email(
                "email_basic.html",
                title="You received photos",
                intro=f"<p>You received photos from <b>{sender_email}</b> to your gallery.</p>" + (f"<p>Note: {note}</p>" if note else ""),
                button_url=gallery_link,
                button_label="Open your gallery",
                footer_note="If you weren't expecting this, you can ignore this message.",
            )
            send_email_smtp(emails[0], "New photos received", html)
    except Exception as ex:
        logger.warning(f"Email notify failed (send-existing): {ex}")

    _record_recent(uid, emails)

    return {"ok": True, "results": results}
    """
    Allow a logged-in user to send a photo to a friend's gallery.
    - friend_email must belong to an existing user (by Firebase email).
    - The file is stored under friend's watermarked area; original saved too.
    - An email notification is sent to the friend with a link to their gallery.
    """
    sender_uid = get_uid_from_request(request)
    if not sender_uid:
        _friendly_err("Unauthorized", status.HTTP_401_UNAUTHORIZED)

    # rate limit (1 action)
    _incr_rate(sender_uid)

    friend_email = _normalize_email(friend_email)
    if not friend_email:
        _friendly_err("Friend email required")
    friend_uid = get_uid_by_email(friend_email)
    if not friend_uid:
        _friendly_err("Friend not found")

    try:
        # basic upload validation
        _validate_upload(file.filename or "image", getattr(file, "size", 0) or 0)
        raw = await file.read()
        if not raw:
            _friendly_err("Empty file")
        fname = file.filename or "image"
        orig_ext = (os.path.splitext(fname)[1] or '.jpg').lower()
        if not _ext_ok(orig_ext):
            _friendly_err(f"Unsupported format {orig_ext}")
        ct_map = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
            '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff'
        }
        orig_ct = ct_map.get(orig_ext, 'application/octet-stream')

        # Re-encode to JPEG for gallery view
        img = Image.open(io.BytesIO(raw)).convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=95, subsampling=0, progressive=True, optimize=True)
        buf.seek(0)

        date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
        base = os.path.splitext(os.path.basename(fname))[0][:100] or 'image'
        stamp = int(_dt.utcnow().timestamp())

        # Save ORIGINAL under friend's originals
        original_key = f"users/{friend_uid}/originals/{date_prefix}/{base}-{stamp}-fromfriend-orig{orig_ext}"
        original_url = upload_bytes(original_key, raw, content_type=orig_ct)

        # Save GALLERY JPEG under friend's watermarked
        oext_token = (orig_ext.lstrip('.') or 'jpg').lower()
        key = f"users/{friend_uid}/watermarked/{date_prefix}/{base}-{stamp}-fromfriend-o{oext_token}.jpg"
        url = upload_bytes(key, buf.getvalue(), content_type='image/jpeg')

        # Email notify friend
        try:
            sender_email = get_user_email_from_uid(sender_uid) or "a friend"
            gallery_link = os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip().rstrip("/") + "#/gallery"
            html = render_email(
                "email_basic.html",
                title="You received a photo",
                intro=f"<p>You received a photo from <b>{sender_email}</b> to your gallery.</p>" + (f"<p>Note: {note}</p>" if note else ""),
                button_url=gallery_link,
                button_label="Open your gallery",
                footer_note="If you weren't expecting this, you can ignore this message.",
            )
            send_email_smtp(friend_email, "New photo received", html)
        except Exception as ex:
            logger.warning(f"Email notify failed: {ex}")

        _record_recent(sender_uid, [friend_email])
        return {"ok": True, "key": key, "url": url, "original_key": original_key, "original_url": original_url}
    except HTTPException:
        raise
    except Exception as ex:
        logger.exception(f"Send to friend failed: {ex}")
        _friendly_err("Could not send photo. Please try again.", status.HTTP_500_INTERNAL_SERVER_ERROR)