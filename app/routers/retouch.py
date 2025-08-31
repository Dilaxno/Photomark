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
from diffusers import StableDiffusionInstructPix2PixPipeline
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
            import torch
            # Use the widely available Stable Diffusion InstructPix2Pix pipeline
            model_id = os.getenv("INSTRUCT_PIX2PIX_MODEL", "timbrooks/instruct-pix2pix")
            _ai_retouch_pipe = StableDiffusionInstructPix2PixPipeline.from_pretrained(
                model_id,
                torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
                safety_checker=None,
            )
            device = "cuda" if torch.cuda.is_available() else "cpu"
            _ai_retouch_pipe = _ai_retouch_pipe.to(device)
            logger.info(f"Pipeline loaded: {model_id} on {device}")

        input_image = Image.open(io.BytesIO(raw)).convert("RGB")

        # Direct call to model — use guidance and image guidance defaults
        result = _ai_retouch_pipe(
            prompt=prompt,
            image=input_image,
            guidance_scale=float(os.getenv("IP2P_GUIDANCE", 7.5)),
            image_guidance_scale=float(os.getenv("IP2P_IMAGE_GUIDANCE", 1.5)),
            num_inference_steps=int(os.getenv("IP2P_STEPS", 30)),
        ).images[0]

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
            # JSON path with nested URL or embedded base64/data URL
            if "application/json" in ct:
                try:
                    data = response.json()
                except Exception:
                    data = None

                # Attempt to find an URL anywhere in the JSON
                def find_url(d):
                    if isinstance(d, dict):
                        for _, v in d.items():
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

                # Attempt to find a data URL (data:image/...;base64,...) anywhere
                def find_data_url(d):
                    import re, base64
                    if isinstance(d, str):
                        m = re.match(r'^data:(image/[^;]+);base64,(.+)$', d, re.DOTALL)
                        if m:
                            mime = m.group(1)
                            payload = m.group(2)
                            try:
                                return base64.b64decode(payload), mime
                            except Exception:
                                return None
                        return None
                    if isinstance(d, dict):
                        for _, v in d.items():
                            found = find_data_url(v)
                            if found:
                                return found
                    if isinstance(d, list):
                        for it in d:
                            found = find_data_url(it)
                            if found:
                                return found
                    return None

                # Attempt to find a raw base64 image string
                def find_base64_image(d):
                    import re, base64
                    def mime_from_bytes(b: bytes) -> str:
                        if b.startswith(b'\x89PNG\r\n\x1a\n'):
                            return 'image/png'
                        if b.startswith(b'\xff\xd8'):
                            return 'image/jpeg'
                        if b.startswith(b'GIF8'):
                            return 'image/gif'
                        if len(b) >= 12 and b[0:4] == b'RIFF' and b[8:12] == b'WEBP':
                            return 'image/webp'
                        return 'application/octet-stream'
                    base64_re = re.compile(r'^[A-Za-z0-9+/=\r\n]+$')
                    if isinstance(d, str):
                        s = d.strip()
                        # heuristic: long enough and looks like base64
                        if len(s) > 200 and base64_re.match(s):
                            try:
                                b = base64.b64decode(s)
                                # simple sniffing
                                mt = mime_from_bytes(b)
                                if mt.startswith('image/'):
                                    return b, mt
                            except Exception:
                                return None
                        return None
                    if isinstance(d, dict):
                        for _, v in d.items():
                            found = find_base64_image(v)
                            if found:
                                return found
                    if isinstance(d, list):
                        for it in d:
                            found = find_base64_image(it)
                            if found:
                                return found
                    return None

                if data is not None:
                    # 1) Try URL
                    url = find_url(data)
                    if url:
                        img = await client.get(url)
                        img.raise_for_status()
                        mt = img.headers.get("content-type", "image/jpeg")
                        return StreamingResponse(io.BytesIO(img.content), media_type=mt)

                    # 2) Try data URL
                    data_url = find_data_url(data)
                    if data_url:
                        b, mt = data_url
                        return StreamingResponse(io.BytesIO(b), media_type=mt)

                    # 3) Try raw base64 image
                    b64img = find_base64_image(data)
                    if b64img:
                        b, mt = b64img
                        return StreamingResponse(io.BytesIO(b), media_type=mt)

                    # Fallback: return JSON as-is if no image was found
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
