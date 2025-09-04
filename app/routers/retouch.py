from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from typing import Optional
import io
import os
import uuid as _uuid
import hashlib

from PIL import Image
import numpy as np
import cv2

from rembg import remove, new_session  # background removal


from app.core.config import logger

# ---------------- CONFIG ----------------
_vignette_cache = {}
_rmbg_mask_cache = {}  # Cache masks keyed by file hash
_rmbg_session = None  # Global rembg session (lazy init)

# Global instructpix2pix pipeline (lazy init)
_ai_retouch_pipe = None

router = APIRouter(prefix="/api/retouch", tags=["retouch"])

# ---- One-free-generation helpers ----
from app.utils.storage import read_json_key, write_json_key
from datetime import datetime as _dt
from fastapi import Request
from app.core.auth import resolve_workspace_uid

def _billing_uid_from_request(request: Request) -> str:
    eff_uid, _ = resolve_workspace_uid(request)
    if eff_uid:
        return eff_uid
    try:
        ip = request.client.host if getattr(request, 'client', None) else 'unknown'
    except Exception:
        ip = 'unknown'
    return f"anon:{ip}"


def _is_paid_customer(uid: str) -> bool:
    try:
        ent = read_json_key(f"users/{uid}/billing/entitlement.json") or {}
        return bool(ent.get('isPaid'))
    except Exception:
        return False


def _consume_one_free(uid: str, tool: str) -> bool:
    key = f"users/{uid}/billing/free_usage.json"
    try:
        data = read_json_key(key) or {}
    except Exception:
        data = {}
    count = int(data.get('count') or 0)
    if count >= 1:
        return False
    tools = data.get('tools') or {}
    tools[tool] = int(tools.get(tool) or 0) + 1
    try:
        write_json_key(key, {
            'used': True,
            'count': count + 1,
            'tools': tools,
            'updatedAt': int(_dt.utcnow().timestamp()),
        })
    except Exception:
        pass
    return True


# ---------------- IMAGE UTILITIES ----------------
async def fetch_bytes(url: str, timeout: float = 20.0) -> bytes:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


def composite_onto_background(fg: Image.Image, bg: Image.Image) -> Image.Image:
    fg_w, fg_h = fg.size
    bg = bg.convert("RGBA")
    if fg.mode != "RGBA":
        fg = fg.convert("RGBA")

    bg_ratio = bg.width / bg.height
    fg_ratio = fg_w / fg_h
    if bg_ratio > fg_ratio:
        new_h = fg_h
        new_w = int(bg_ratio * new_h)
    else:
        new_w = fg_w
        new_h = int(new_w / bg_ratio)
    bg_resized = bg.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - fg_w) // 2
    top = (new_h - fg_h) // 2
    bg_cropped = bg_resized.crop((left, top, left + fg_w, top + fg_h))

    out = Image.new("RGBA", (fg_w, fg_h))
    out.paste(bg_cropped, (0, 0))
    out.alpha_composite(fg)
    return out


# ---------------- MASK CACHING ----------------
def compute_mask(img: Image.Image) -> Image.Image:
    """Compute background-removed image using rembg, cached by file hash."""
    global _rmbg_session
    raw_bytes = img.tobytes()
    file_hash = hashlib.md5(raw_bytes).hexdigest()

    if file_hash in _rmbg_mask_cache:
        return _rmbg_mask_cache[file_hash]

    try:
        if _rmbg_session is None:
            try:
                _rmbg_session = new_session(
                    providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
                )
                logger.info("rembg session initialized with CUDAExecutionProvider (GPU) if available")
            except Exception as e:
                logger.exception(f"Failed to init rembg with CUDAExecutionProvider: {e}")
                _rmbg_session = new_session(providers=["CPUExecutionProvider"])
                logger.info("rembg session initialized with CPUExecutionProvider")

        fg_bytes = remove(
            img,
            session=_rmbg_session,
            alpha_matting=True,
            alpha_matting_foreground_threshold=240,
            alpha_matting_background_threshold=10,
            alpha_matting_erode_structure_size=5,
            alpha_matting_base_size=1000,
        )
        fg = fg_bytes if isinstance(fg_bytes, Image.Image) else Image.open(io.BytesIO(fg_bytes))
        if fg.mode != "RGBA":
            fg = fg.convert("RGBA")

        # Validate transparency
        amin, amax = fg.getchannel("A").getextrema()
        if amin == 255 and amax == 255:
            raise ValueError("Background removal produced fully opaque image (no transparency)")

    except Exception as e:
        logger.exception(f"rembg background removal failed: {e}")
        raise

    _rmbg_mask_cache[file_hash] = fg
    return fg


# ---------------- API ENDPOINTS ----------------
@router.post("/remove_background")
async def remove_background(request: Request, file: UploadFile = File(...)):
    """Remove background using rembg and return transparent PNG cutout."""
    raw = await file.read()
    if not raw:
        return {"error": "empty file"}
    # Enforce one free generation unless paid
    billing_uid = _billing_uid_from_request(request)
    if not _is_paid_customer(billing_uid):
        if not _consume_one_free(billing_uid, 'retouch_remove_bg'):
            return {"error": "free_limit_reached", "message": "You have used your free generation. Upgrade to continue."}
    try:
        img = Image.open(io.BytesIO(raw))
        fg = compute_mask(img)
        if fg.mode != "RGBA":
            fg = fg.convert("RGBA")
        buf = io.BytesIO()
        fg.save(buf, format="PNG")
        buf.seek(0)
        return StreamingResponse(buf, media_type="image/png")
    except Exception as ex:
        logger.exception(f"Background removal failed: {ex}")
        return {"error": str(ex)}


def _parse_hex_color(hex_color: str):
    c = hex_color.strip()
    if c.startswith("#"):
        c = c[1:]
    if len(c) == 3:
        c = "".join([ch * 2 for ch in c])
    if len(c) != 6:
        raise ValueError("hex_color must be in #RRGGBB or #RGB format")
    try:
        r = int(c[0:2], 16)
        g = int(c[2:4], 16)
        b = int(c[4:6], 16)
    except ValueError:
        raise ValueError("Invalid hex_color")
    return (r, g, b)


@router.post("/recompose")
async def recompose_background(
    request: Request,
    cutout: UploadFile = File(...),
    mode: str = Form("transparent"),
    hex_color: Optional[str] = Form(None),
    background: Optional[UploadFile] = File(None),
    bg_url: Optional[str] = Form(None),
):
    """Recompose a provided cutout (RGBA) onto different backgrounds without re-running segmentation."""
    try:
        cut_raw = await cutout.read()
        if not cut_raw:
            return {"error": "empty cutout"}
        fg = Image.open(io.BytesIO(cut_raw)).convert("RGBA")

        # Enforce one free generation unless paid (for recompositions)
        billing_uid = _billing_uid_from_request(request)
        if not _is_paid_customer(billing_uid):
            if not _consume_one_free(billing_uid, 'retouch_recompose'):
                return {"error": "free_limit_reached", "message": "You have used your free generation. Upgrade to continue."}

        if mode == "transparent":
            out = fg
        elif mode == "color":
            if not hex_color:
                return {"error": "hex_color required for color mode"}
            r, g, b = _parse_hex_color(hex_color)
            bg = Image.new("RGBA", fg.size, (r, g, b, 255))
            out = Image.new("RGBA", fg.size)
            out.paste(bg, (0, 0))
            out.alpha_composite(fg)
        elif mode == "image":
            if background is None:
                return {"error": "background file required for image mode"}
            bg_raw = await background.read()
            bg = Image.open(io.BytesIO(bg_raw)).convert("RGBA")
            out = composite_onto_background(fg, bg)
        elif mode == "url":
            if not bg_url:
                return {"error": "bg_url required for url mode"}
            try:
                bg_bytes = await fetch_bytes(bg_url)
            except Exception as e:
                return {"error": f"failed to fetch background url: {e}"}
            bg = Image.open(io.BytesIO(bg_bytes)).convert("RGBA")
            out = composite_onto_background(fg, bg)
        else:
            return {"error": "invalid mode. use one of: transparent,color,image,url"}

        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)
        return StreamingResponse(buf, media_type="image/png")
    except Exception as ex:
        logger.exception(f"Recompose failed: {ex}")
        return {"error": str(ex)}






