from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException, status
from typing import Optional, List, Dict, Any, Tuple
import io
import os
import json
from datetime import datetime as _dt

import numpy as np
import cv2
from PIL import Image

from app.core.config import (
    logger,
    COLLAB_MAX_IMAGE_MB,
    COLLAB_ALLOWED_EXTS,
    COLLAB_RATE_LIMIT_WINDOW_SEC,
    COLLAB_RATE_LIMIT_MAX_ACTIONS,
    COLLAB_MAX_RECIPIENTS,
)
from app.core.auth import get_uid_from_request, get_uid_by_email, get_user_email_from_uid
from app.utils.emailing import render_email, send_email_smtp
from app.utils.storage import upload_bytes, read_json_key, write_json_key, read_bytes_key

router = APIRouter(prefix="/api/collab", tags=["collab"]) 

# ----------------------
# Helpers
# ----------------------

def _normalize_email(e: str) -> str:
    return (e or "").strip().lower()


def _friendly_err(msg: str, code: int = status.HTTP_400_BAD_REQUEST):
    raise HTTPException(status_code=code, detail={"error": msg})


def _rate_key(uid: str) -> str:
    return f"users/{uid}/collab/rate.json"


def _recent_key(uid: str) -> str:
    return f"users/{uid}/collab/recent_recipients.json"

# Special vault to categorize collaboration uploads in the gallery
FRIENDS_VAULT_NAME = "Photos sent by friends"


def _safe_vault(name: str) -> str:
    safe = "".join(c for c in (name or '') if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
    return safe or "Inbox"


def _vault_json_key(uid: str, vault: str) -> str:
    safe = _safe_vault(vault)
    return f"users/{uid}/vaults/{safe}.json"


def _vault_meta_key(uid: str, vault: str) -> str:
    safe = _safe_vault(vault)
    return f"users/{uid}/vaults/_meta/{safe}.json"


def _ensure_vault_meta(uid: str, vault: str):
    """Ensure the vault has a display label for nicer UI."""
    try:
        meta_key = _vault_meta_key(uid, vault)
        meta = read_json_key(meta_key) or {}
        dn = meta.get("display_name")
        desired = FRIENDS_VAULT_NAME if vault == FRIENDS_VAULT_NAME else (vault or '')
        if not dn:
            meta["display_name"] = desired
            write_json_key(meta_key, meta)
    except Exception as ex:
        logger.warning(f"collab: ensure_vault_meta failed: {ex}")


def _add_to_vault(uid: str, vault: str, new_keys: List[str]):
    """Append keys to the user's vault json (creates if missing)."""
    try:
        vkey = _vault_json_key(uid, vault)
        data = read_json_key(vkey) or {}
        cur = set([k for k in (data.get("keys") or []) if isinstance(k, str)])
        added = False
        for k in (new_keys or []):
            if isinstance(k, str) and k.startswith(f"users/{uid}/"):
                if k not in cur:
                    cur.add(k)
                    added = True
        if added or data.get("keys") is None:
            write_json_key(vkey, {"keys": sorted(cur)})
        _ensure_vault_meta(uid, vault)
    except Exception as ex:
        logger.warning(f"collab: add_to_vault failed: {ex}")


def _incr_rate(uid: str):
    now = int(_dt.utcnow().timestamp())
    rec = read_json_key(_rate_key(uid)) or {}
    ws = int(rec.get("window_start_ts") or 0)
    cnt = int(rec.get("count") or 0)
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

# ----------------------
# OpenCV Annotation Helpers
# ----------------------

HEX_DEFAULT = "#1E90FF"  # DodgerBlue as a readable default


def _hex_to_bgr(color: Optional[str]) -> Tuple[int, int, int]:
    try:
        if not color:
            color = HEX_DEFAULT
        c = color.lstrip('#')
        if len(c) == 3:
            c = ''.join(ch*2 for ch in c)
        r = int(c[0:2], 16)
        g = int(c[2:4], 16)
        b = int(c[4:6], 16)
        return (b, g, r)
    except Exception:
        return (255, 165, 0)  # fallback: BGR for orange


def _ensure_uint8_rgb(img: Image.Image) -> np.ndarray:
    arr = np.array(img.convert('RGB'))
    return arr[:, :, ::-1].copy()  # RGB -> BGR for OpenCV


def _cv_to_jpeg_bytes(cv_img: np.ndarray, quality: int = 95) -> bytes:
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    ok, enc = cv2.imencode('.jpg', cv_img, encode_param)
    if not ok:
        raise RuntimeError('Failed to encode JPEG')
    return enc.tobytes()


def _draw_annotations_cv(
    jpeg_bytes: bytes,
    annotations: Any,
    note: Optional[str] = None,
) -> bytes:
    """
    Draws simple annotations directly on the image using OpenCV and returns new JPEG bytes.

    Accepted annotation objects (normalized 0..1):
      - Rect: {"type":"rect", "x":0.1, "y":0.2, "w":0.3, "h":0.25, "label":"optional", "color":"#RRGGBB"}
      - Circle: {"type":"circle", "cx":0.5, "cy":0.4, "r":0.1, "label":"optional", "color":"#RRGGBB"}
      - Text: {"type":"text", "x":0.1, "y":0.1, "text":"Hello", "color":"#RRGGBB"}
    Also supports lists of such objects.

    "note" (if provided) will be drawn as a header label in the top-left.
    """
    try:
        pil = Image.open(io.BytesIO(jpeg_bytes)).convert('RGB')
    except Exception:
        # If bytes aren't JPEG, try decoding with OpenCV directly
        data = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        cv_img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if cv_img is None:
            raise HTTPException(status_code=400, detail={"error": "Invalid image bytes for annotation"})
    else:
        cv_img = _ensure_uint8_rgb(pil)

    h, w = cv_img.shape[:2]

    # Normalize annotations to a list
    if annotations is None:
        annotations = []
    if isinstance(annotations, dict):
        annotations = [annotations]

    # Draw global note if present
    if note and str(note).strip():
        _put_text(cv_img, str(note).strip(), (int(0.02 * w), int(0.06 * h)))

    # Heuristic thickness based on image size
    base_t = max(2, int(round(0.0025 * (w + h))))

    for ann in (annotations or []):
        if not isinstance(ann, dict):
            continue
        a_type = (ann.get("type") or "").lower()
        color = _hex_to_bgr(ann.get("color"))
        label = str(ann.get("label") or ann.get("text") or "").strip()

        if a_type == "rect":
            x = float(ann.get("x", 0))
            y = float(ann.get("y", 0))
            wnorm = float(ann.get("w", 0))
            hnorm = float(ann.get("h", 0))
            x1 = max(0, min(w - 1, int(round(x * w))))
            y1 = max(0, min(h - 1, int(round(y * h))))
            x2 = max(0, min(w - 1, int(round((x + wnorm) * w))))
            y2 = max(0, min(h - 1, int(round((y + hnorm) * h))))
            cv2.rectangle(cv_img, (x1, y1), (x2, y2), color, thickness=base_t)
            if label:
                _boxed_label(cv_img, label, (x1, max(0, y1 - int(0.01 * h))), color)

        elif a_type == "circle":
            cx = float(ann.get("cx", 0))
            cy = float(ann.get("cy", 0))
            r = float(ann.get("r", 0))
            center = (max(0, min(w - 1, int(round(cx * w)))), max(0, min(h - 1, int(round(cy * h)))))
            radius = max(1, int(round(r * (w + h) / 2)))
            cv2.circle(cv_img, center, radius, color, thickness=base_t)
            if label:
                _boxed_label(cv_img, label, (center[0], max(0, center[1] - radius - int(0.01 * h))), color)

        elif a_type == "text":
            x = float(ann.get("x", 0))
            y = float(ann.get("y", 0))
            pos = (max(0, min(w - 1, int(round(x * w)))), max(0, min(h - 1, int(round(y * h)))))
            _put_text(cv_img, label or "", pos, color=color)

        # Silently ignore unsupported types

    return _cv_to_jpeg_bytes(cv_img)


def _boxed_label(img: np.ndarray, text: str, org: Tuple[int, int], color: Tuple[int, int, int]):
    if not text:
        return
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.6
    thickness = 2
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    x, y = org
    pad = 6
    cv2.rectangle(img, (x, y - th - 2 * pad), (x + tw + 2 * pad, y + baseline + pad), color, thickness=-1)
    cv2.putText(img, text, (x + pad, y - pad), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)


def _put_text(img: np.ndarray, text: str, org: Tuple[int, int], color: Tuple[int, int, int] = (0, 0, 0)):
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.8
    thickness = 2
    # Shadow for readability
    cv2.putText(img, text, (org[0] + 2, org[1] + 2), font, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(img, text, org, font, scale, color, thickness, cv2.LINE_AA)

# ----------------------
# Collab: Send endpoints (now with optional OpenCV burn-in)
# ----------------------

CT_MAP = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
    '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff'
}


def _reencode_to_jpeg(raw: bytes) -> bytes:
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=95, subsampling=0, progressive=True, optimize=True)
    return buf.getvalue()


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

    _validate_upload(file.filename or "image", getattr(file, "size", 0) or 0)
    raw = await file.read()
    if not raw:
        _friendly_err("Empty file")

    fname = file.filename or "image"
    orig_ext = (os.path.splitext(fname)[1] or '.jpg').lower()
    if not _ext_ok(orig_ext):
        _friendly_err(f"Unsupported format {orig_ext}")
    orig_ct = CT_MAP.get(orig_ext, 'application/octet-stream')

    # Re-encode once for gallery JPEG
    gallery_jpeg = _reencode_to_jpeg(raw)

    # Annotations removed: do not burn any annotations; keep gallery JPEG as re-encoded original
    # Note is stored only in metadata below and not rendered onto pixels.

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
            key = f"users/{friend_uid}/external/{date_prefix}/{base}-{stamp}-fromfriend-o{oext_token}.jpg"
            url = upload_bytes(key, gallery_jpeg, content_type='image/jpeg')

            # Store meta envelope (note/annotations are already burned-in; we still keep lightweight meta for traceability)
            try:
                meta_key = f"{os.path.splitext(key)[0]}.json"
                meta: Dict[str, Any] = read_json_key(meta_key) or {}
                if not isinstance(meta, dict):
                    meta = {}
                meta.setdefault("from", get_user_email_from_uid(sender_uid) or None)
                meta["at"] = _dt.utcnow().isoformat()
                if note and str(note).strip():
                    meta["note"] = str(note).strip()
                write_json_key(meta_key, meta)
            except Exception as ex:
                logger.warning(f"collab: failed to write meta json for {key}: {ex}")

            try:
                sender_email = get_user_email_from_uid(sender_uid) or "a friend"
                gallery_link = os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip().rstrip("/") + "#gallery"
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

            try:
                _add_to_vault(friend_uid, FRIENDS_VAULT_NAME, [key])
            except Exception as ex:
                logger.warning(f"collab: failed to add to friends vault for {email}: {ex}")

            results.append({
                "email": email,
                "ok": True,
                "key": key,
                "url": url,
                "original_key": original_key,
                "original_url": original_url
            })
        except Exception as ex:
            logger.exception(f"send_to_friends error for {email}: {ex}")
            results.append({"email": email, "ok": False, "error": "Internal error"})

    _record_recent(sender_uid, emails)

    return {"ok": True, "results": results}


@router.post("/send-multiple-to-friend")
async def send_multiple_to_friend(
    request: Request,
    friend_email: str = Form(...),
    files: List[UploadFile] = File(...),
    notes: Optional[str] = Form(None),  # JSON array of notes aligned with files
):
    """
    Send multiple images to a single friend's gallery. Notes are stored in metadata only; no annotations are burned.
    """
    sender_uid = get_uid_from_request(request)
    if not sender_uid:
        _friendly_err("Unauthorized", status.HTTP_401_UNAUTHORIZED)

    _incr_rate(sender_uid)

    email = _normalize_email(friend_email)
    if not email:
        _friendly_err("Friend email required")
    friend_uid = get_uid_by_email(email)
    if not friend_uid:
        _friendly_err("Friend not found")

    # Parse and validate required notes array (one per file)
    try:
        arr = json.loads(notes or "[]")
    except Exception:
        _friendly_err("Invalid notes payload")
    if not isinstance(arr, list):
        _friendly_err("Invalid notes payload: expected JSON array")

    if not files or len(files) == 0:
        _friendly_err("At least one file is required")
    if len(arr) != len(files):
        _friendly_err("A note is required for each file")

    per_item_notes: List[str] = []
    for i, v in enumerate(arr):
        s = ("" if v is None else str(v)).strip()
        if not s:
            _friendly_err(f"Note for item {i+1} is required")
        per_item_notes.append(s)

    date_prefix = _dt.utcnow().strftime('%Y/%m/%d')

    items: List[Dict[str, Any]] = []
    for idx, f in enumerate(files):
        try:
            _validate_upload(f.filename or "image", getattr(f, "size", 0) or 0)
            raw = await f.read()
            if not raw:
                items.append({"index": idx, "ok": False, "error": "Empty file"})
                continue

            fname = f.filename or "image"
            orig_ext = (os.path.splitext(fname)[1] or '.jpg').lower()
            if not _ext_ok(orig_ext):
                items.append({"index": idx, "ok": False, "error": f"Unsupported format {orig_ext}"})
                continue
            orig_ct = CT_MAP.get(orig_ext, 'application/octet-stream')

            # Prepare gallery jpeg
            gallery_jpeg = _reencode_to_jpeg(raw)

            # No annotations: do not modify pixels; notes are stored in metadata only
            n = per_item_notes[idx] if idx < len(per_item_notes) else None

            base = os.path.splitext(os.path.basename(fname))[0][:100] or 'image'
            stamp = int(_dt.utcnow().timestamp())

            # Save ORIGINAL
            original_key = f"users/{friend_uid}/originals/{date_prefix}/{base}-{stamp}-fromfriend-orig{orig_ext}"
            original_url = upload_bytes(original_key, raw, content_type=orig_ct)

            # Save GALLERY JPEG (possibly annotated)
            oext_token = (orig_ext.lstrip('.') or 'jpg').lower()
            key = f"users/{friend_uid}/external/{date_prefix}/{base}-{stamp}-fromfriend-o{oext_token}.jpg"
            url = upload_bytes(key, gallery_jpeg, content_type='image/jpeg')

            # Lightweight meta
            try:
                meta_key = f"{os.path.splitext(key)[0]}.json"
                meta: Dict[str, Any] = {
                    "from": get_user_email_from_uid(sender_uid) or None,
                    "at": _dt.utcnow().isoformat(),
                }
                if n and str(n).strip():
                    meta["note"] = str(n).strip()
                if len(meta.keys()) > 2:
                    write_json_key(meta_key, meta)
            except Exception as ex:
                logger.warning(f"collab: failed to write per-item meta json for {key}: {ex}")

            items.append({
                "index": idx,
                "ok": True,
                "key": key,
                "url": url,
                "original_key": original_key,
                "original_url": original_url,
                "note": n,
                "annotations_burned": False,
            })

            try:
                _add_to_vault(friend_uid, FRIENDS_VAULT_NAME, [key])
            except Exception as ex:
                logger.warning(f"collab: failed to record multi item {idx} in friends vault: {ex}")
        except Exception as ex:
            logger.exception(f"send-multiple-to-friend error for index {idx}: {ex}")
            items.append({"index": idx, "ok": False, "error": "Internal error"})

    # Email once
    try:
        sender_email = get_user_email_from_uid(sender_uid) or "a friend"
        gallery_link = os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip().rstrip("/") + "#gallery"
        ok_count = len([x for x in items if x.get('ok')])
        html = render_email(
            "email_basic.html",
            title="You received photos",
            intro=f"<p>You received {ok_count} photos from <b>{sender_email}</b> to your gallery.</p>",
            button_url=gallery_link,
            button_label="Open your gallery",
            footer_note="If you weren't expecting this, you can ignore this message.",
        )
        send_email_smtp(email, "New photos received", html)
    except Exception as ex:
        logger.warning(f"Email notify failed (send-multiple-to-friend): {ex}")

    _record_recent(sender_uid, [email])

    return {"ok": True, "result": {"email": email, "items": items}}


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
    keys: str = Form(...),  # JSON array of keys to already-uploaded gallery JPEGs
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

    # Annotations removed: copies are sent as-is; optional note stored in metadata only

    results: List[Dict[str, Any]] = []

    for email in emails:
        friend_uid = get_uid_by_email(email)
        if not friend_uid:
            results.append({"email": email, "ok": False, "error": "Friend not found"})
            continue

        per_email = {"email": email, "ok": True, "items": []}

        for k in src_keys:
            try:
                data = read_bytes_key(k)
                if not data:
                    per_email["items"].append({"key": k, "ok": False, "error": "Not found"})
                    continue

                # No annotations: send copy as-is
                to_send = data

                base_name = os.path.basename(k)
                orig_token = "jpg"
                if "-o" in base_name:
                    orig_token = base_name.split("-o")[-1].split(".")[0].lower() or "jpg"
                date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
                name = os.path.splitext(base_name)[0]
                stamp = int(_dt.utcnow().timestamp())

                dest_key = f"users/{friend_uid}/external/{date_prefix}/{name}-{stamp}-fromfriend.jpg"
                dest_url = upload_bytes(dest_key, to_send, content_type='image/jpeg')
                # Persist note metadata for recipient if provided
                try:
                    if note and str(note).strip():
                        meta_key = f"{os.path.splitext(dest_key)[0]}.json"
                        meta = {
                            "from": get_user_email_from_uid(uid) or None,
                            "at": _dt.utcnow().isoformat(),
                            "note": str(note).strip(),
                        }
                        write_json_key(meta_key, meta)
                except Exception as ex:
                    logger.warning(f"collab: failed to write note meta for {dest_key}: {ex}")

                # Try to copy original too
                candidate_orig = k.replace('/watermarked/', '/originals/').rsplit('-o', 1)[0]
                candidate_orig = f"{candidate_orig}-orig.{orig_token}"
                orig_bytes = read_bytes_key(candidate_orig) or data
                orig_ct = {
                    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'heic': 'image/heic', 'tif': 'image/tiff', 'tiff': 'image/tiff'
                }.get(orig_token, 'application/octet-stream')
                dest_orig_key = f"users/{friend_uid}/originals/{date_prefix}/{name}-{stamp}-fromfriend-orig.{orig_token}"
                dest_orig_url = upload_bytes(dest_orig_key, orig_bytes, content_type=orig_ct)

                per_email["items"].append({
                    "key": k,
                    "ok": True,
                    "dest_key": dest_key,
                    "dest_url": dest_url,
                    "dest_original_key": dest_orig_key,
                    "dest_original_url": dest_orig_url,
                })

                try:
                    _add_to_vault(friend_uid, FRIENDS_VAULT_NAME, [dest_key])
                except Exception as ex:
                    logger.warning(f"collab: failed to record existing item in friends vault for {email}: {ex}")
            except Exception as ex:
                logger.warning(f"send-existing failed for {k}: {ex}")
                per_email["items"].append({"key": k, "ok": False, "error": "Internal error"})

        results.append(per_email)

    try:
        if emails:
            sender_email = get_user_email_from_uid(uid) or "a friend"
            gallery_link = os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip().rstrip("/") + "#gallery"
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


# ----------------------
# Collaboration notes (annotations removed)
# ----------------------

@router.post("/annotations/note")
async def set_note(request: Request):
    """
    Set or update a top-level note stored in metadata only. Pixels are not modified.

    Body JSON: { key: str, note: Optional[str] }
    """
    uid = get_uid_from_request(request)
    if not uid:
        _friendly_err("Unauthorized", status.HTTP_401_UNAUTHORIZED)

    try:
        body = await request.json()
    except Exception:
        _friendly_err("Invalid JSON body")

    key = (body or {}).get("key")
    note = (body or {}).get("note")
    if not key or not isinstance(key, str):
        _friendly_err("Missing key")

    # Validate existence
    data = read_bytes_key(key)
    if not data:
        _friendly_err("Image not found", status.HTTP_404_NOT_FOUND)

    # Update meta only
    meta_key = f"{os.path.splitext(key)[0]}.json"
    meta = read_json_key(meta_key) or {}
    if not isinstance(meta, dict):
        meta = {}

    if note is None or (isinstance(note, str) and not note.strip()):
        if "note" in meta:
            del meta["note"]
    else:
        meta["note"] = str(note).strip()

    if not meta.get("from"):
        try:
            meta["from"] = get_user_email_from_uid(uid) or None
        except Exception:
            meta["from"] = None
    meta["at"] = _dt.utcnow().isoformat()

    write_json_key(meta_key, meta)
    return {"ok": True, "meta": meta}
