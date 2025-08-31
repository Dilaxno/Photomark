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
from diffusers import DiffusionPipeline
from diffusers.utils import load_image
import torch

from app.core.config import logger

# ---------------- CONFIG ----------------
_vignette_cache = {}
_rmbg_mask_cache = {}  # Cache masks keyed by file hash
_rmbg_session = None  # Global rembg session (lazy init)

# Global instructpix2pix pipeline (lazy init)
_ai_retouch_pipe = None

router = APIRouter(prefix="/api/retouch", tags=["retouch"])


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
async def remove_background(file: UploadFile = File(...)):
    """Remove background using rembg and return transparent PNG cutout."""
    raw = await file.read()
    if not raw:
        return {"error": "empty file"}
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


# ---------------- AI RETOUCH (InstructPix2Pix) ----------------
# ---------------- AI RETOUCH (InstructPix2Pix) ----------------
@router.post("/retouch")
async def retouch_image(
    file: UploadFile = File(...),
    prompt: str = Form(...),   # 👈 user provides natural language edit instruction
):
    """
    Retouch / edit image using InstructPix2Pix (Qwen/Qwen-Image-Edit).
    Example prompt: "Make the background a beach" or "Turn this cat into a dog".
    """
    global _ai_retouch_pipe

    raw = await file.read()
    if not raw:
        return {"error": "empty file"}

    try:
        # Init instructpix2pix pipeline (lazy)
        if _ai_retouch_pipe is None:
            logger.info("Loading InstructPix2Pix pipeline...")
            from diffusers import DiffusionPipeline
            import torch

            _ai_retouch_pipe = DiffusionPipeline.from_pretrained(
                "Qwen/Qwen-Image-Edit",
                torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
            )
            _ai_retouch_pipe.to("cuda" if torch.cuda.is_available() else "cpu")
            logger.info("Pipeline loaded")

        input_image = Image.open(io.BytesIO(raw)).convert("RGB")

        # Direct call to model — no assistant wrapper
        result = _ai_retouch_pipe(image=input_image, prompt=prompt).images[0]

        buf = io.BytesIO()
        result.save(buf, format="PNG")
        buf.seek(0)
        return StreamingResponse(buf, media_type="image/png")

    except Exception as ex:
        logger.exception(f"AI retouch failed: {ex}")
        return {"error": str(ex)}


@router.post("/colorize")
async def colorize_photo(file: UploadFile = File(...)):
    """Proxy to RapidAPI colorization service. Accepts an image and returns a colorized image or URL.
    Uses env vars RAPIDAPI_COLORIZE_KEY, RAPIDAPI_COLORIZE_HOST, RAPIDAPI_COLORIZE_URL.
    """
    from app.core.config import (
        RAPIDAPI_COLORIZE_KEY,
        RAPIDAPI_COLORIZE_HOST,
        RAPIDAPI_COLORIZE_URL,
    )
    import httpx

    if not RAPIDAPI_COLORIZE_KEY or not RAPIDAPI_COLORIZE_HOST or not RAPIDAPI_COLORIZE_URL:
        return {"error": "Colorize API not configured"}

    raw = await file.read()
    if not raw:
        return {"error": "empty file"}

    filename = file.filename or "image.png"
    content_type = file.content_type or "image/png"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            headers = {
                "x-rapidapi-host": RAPIDAPI_COLORIZE_HOST,
                "x-rapidapi-key": RAPIDAPI_COLORIZE_KEY,
            }

            # Try a few common multipart field names for robustness
            variants = [
                {"image": (filename, raw, content_type)},
                {"file": (filename, raw, content_type)},
                {"photo": (filename, raw, content_type)},
            ]
            response = None
            last_error_text = None
            for files_variant in variants:
                try:
                    r = await client.post(RAPIDAPI_COLORIZE_URL, headers=headers, files=files_variant)
                    r.raise_for_status()
                    response = r
                    break
                except httpx.HTTPStatusError as ex:
                    last_error_text = ex.response.text
                    continue

            if response is None:
                return {"error": "upstream error", "detail": last_error_text}

            ct = response.headers.get("content-type", "")
            # JSON path with nested URL
            if "application/json" in ct:
                try:
                    data = response.json()
                except Exception:
                    data = None

                # Attempt to find an URL anywhere in the JSON
                def find_url(d):
                    if isinstance(d, dict):
                        for k, v in d.items():
                            if isinstance(v, str) and v.startswith("http"):
                                return v
                            found = find_url(v)
                            if found:
                                return found
                    if isinstance(d, list):
                        for it in d:
                            found = find_url(it)
                            if found:
                                return found
                    return None

                if data is not None:
                    url = find_url(data)
                    if url:
                        img = await client.get(url)
                        img.raise_for_status()
                        mt = img.headers.get("content-type", "image/jpeg")
                        return StreamingResponse(io.BytesIO(img.content), media_type=mt)
                    # Fallback: return JSON as-is if no URL was found
                    return data

            # Binary image directly
            if "image/" in ct or "octet-stream" in ct:
                return StreamingResponse(io.BytesIO(response.content), media_type=ct or "application/octet-stream")

            # Last resort: try to decode as JSON schema { content, headers, media_type }
            try:
                data = response.json()
                content = data.get("content") if isinstance(data, dict) else None
                media_type = (data.get("media_type") if isinstance(data, dict) else None) or "image/png"
                if isinstance(content, str):
                    import base64
                    try:
                        b = base64.b64decode(content)
                        return StreamingResponse(io.BytesIO(b), media_type=media_type)
                    except Exception:
                        pass
                return data
            except Exception:
                return StreamingResponse(io.BytesIO(response.content), media_type="application/octet-stream")
    except Exception as ex:
        logger.exception(f"Colorize upstream failed: {ex}")
        return {"error": str(ex)}
