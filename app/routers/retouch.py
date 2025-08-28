from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from typing import Optional
import io
import os
import uuid as _uuid
import hashlib

from PIL import Image, ImageEnhance
import numpy as np
import cv2

from rembg import remove, new_session  # <--- Using rembg for background removal

from app.core.config import logger

# ---------------- CONFIG ----------------
_vignette_cache = {}
_rmbg_mask_cache = {}  # Cache masks keyed by file hash
_rmbg_session = None  # Global rembg session (lazy init)

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
    """Compute background-removed image using rembg, cached by file hash.
    Applies alpha matting and post-processing to improve edge quality.
    Raises a ValueError if background removal appears to have failed (no transparency).
    """
    global _rmbg_session
    raw_bytes = img.tobytes()
    file_hash = hashlib.md5(raw_bytes).hexdigest()

    if file_hash in _rmbg_mask_cache:
        return _rmbg_mask_cache[file_hash]

    try:
        # Initialize session once for better quality and performance
        if _rmbg_session is None:
            try:
                # Try GPU first (requires onnxruntime-gpu)
                _rmbg_session = new_session(providers=["CUDAExecutionProvider", "CPUExecutionProvider"])  # GPU -> CPU fallback
                logger.info("rembg session initialized with CUDAExecutionProvider (GPU) if available")
            except Exception as e:
                logger.exception(f"Failed to init rembg with CUDAExecutionProvider: {e}")
                try:
                    _rmbg_session = new_session(providers=["CPUExecutionProvider"])  # CPU only
                    logger.info("rembg session initialized with CPUExecutionProvider")
                except Exception:
                    _rmbg_session = None

        # Prefer alpha matting to reduce halos; fall back if it fails
        try:
            fg_bytes = remove(
                img,
                session=_rmbg_session,
                alpha_matting=True,
                alpha_matting_foreground_threshold=240,
                alpha_matting_background_threshold=10,
                alpha_matting_erode_structure_size=5,
                alpha_matting_base_size=1000,
            )
        except Exception:
            fg_bytes = remove(img, session=_rmbg_session)

        fg = fg_bytes if isinstance(fg_bytes, Image.Image) else Image.open(io.BytesIO(fg_bytes))
        if fg.mode != "RGBA":
            fg = fg.convert("RGBA")

        # Post-process alpha: small morphological closing to remove thin bands
        alpha = np.array(fg.getchannel("A"))
        # Normalize and threshold to binary then close small gaps
        _, bin_alpha = cv2.threshold(alpha, 0, 255, cv2.THRESH_BINARY)
        kernel = np.ones((3, 3), np.uint8)
        bin_alpha = cv2.morphologyEx(bin_alpha, cv2.MORPH_CLOSE, kernel, iterations=1)
        # Optional: slight blur for softer edges
        soft_alpha = cv2.GaussianBlur(bin_alpha, (3, 3), 0)
        rgb = np.array(fg.convert("RGB"))
        fg = Image.fromarray(np.dstack([rgb, soft_alpha]).astype(np.uint8), mode="RGBA")

        # Validate transparency
        amin, amax = fg.getchannel("A").getextrema()
        if amin == 255 and amax == 255:
            raise ValueError("Background removal produced fully opaque image (no transparency)")
    except Exception as e:
        logger.exception(f"rembg background removal failed: {e}")
        raise

    _rmbg_mask_cache[file_hash] = fg
    return fg


# ---------------- VIGNETTE MASK ----------------
def precompute_vignette_mask(h, w):
    global _vignette_cache
    key = (h, w)
    if key in _vignette_cache:
        return _vignette_cache[key]
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2.0, h / 2.0
    r = np.sqrt(((xx - cx)/cx)**2 + ((yy - cy)/cy)**2)
    mask = np.clip(1.0 - r, 0.0, 1.0)
    _vignette_cache[key] = mask
    return mask


# ---------------- IMAGE ADJUSTMENTS ----------------
def apply_image_adjustments(img: Image.Image, adjustments: dict, preview: bool = False) -> Image.Image:
    if img.mode != 'RGB':
        img = img.convert('RGB')

    if preview:
        img.thumbnail((512, 512), Image.LANCZOS)

    img_np = np.array(img).astype(np.float32)/255.0
    h, w = img_np.shape[:2]
    bgr = img_np[..., ::-1].copy()

    brightness = 1.0 + adjustments.get("brightness", 0)/100.0
    contrast = 1.0 + adjustments.get("contrast", 0)/100.0
    saturation = 1.0 + adjustments.get("saturation", 0)/100.0
    sharpness = adjustments.get("sharpness", 0)/100.0

    bgr = np.clip((bgr - 0.5)*contrast + 0.5, 0, 1) * brightness
    bgr = np.clip(bgr, 0, 1)

    # Saturation adjustment via HSV
    hsv = cv2.cvtColor((bgr*255).astype(np.uint8), cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[...,1] = np.clip(hsv[...,1]*saturation,0,255)
    bgr = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32)/255.0

    # Temperature / Tint
    temp = adjustments.get("temperature",0)/100.0
    tint = adjustments.get("tint",0)/100.0
    B,G,R = cv2.split(bgr)
    R = np.clip(R*(1+0.15*temp),0,1)
    B = np.clip(B*(1+0.15*(-temp)),0,1)
    G = np.clip(G*(1+0.15*tint),0,1)
    bgr = cv2.merge([B,G,R])

    # Shadows / Highlights
    sh = adjustments.get("shadows",0)/100.0
    hi = adjustments.get("highlights",0)/100.0
    if abs(sh)>1e-3 or abs(hi)>1e-3:
        lab = cv2.cvtColor((bgr*255).astype(np.uint8), cv2.COLOR_BGR2LAB).astype(np.float32)/255.0
        L,A,Bc = cv2.split(lab)
        shadow_mask = (L<0.5).astype(np.float32)
        highlight_mask = (L>=0.5).astype(np.float32)
        L = np.clip(L + sh*shadow_mask*(0.5-L)*2 + hi*highlight_mask*(1-L)*2,0,1)
        lab = cv2.merge([L,A,Bc])
        bgr = cv2.cvtColor((lab*255).astype(np.uint8), cv2.COLOR_LAB2BGR).astype(np.float32)/255.0

    # Vignette
    vig = adjustments.get("vignette",0)/100.0
    if abs(vig)>1e-3:
        mask = precompute_vignette_mask(h,w)[...,None]
        if vig>0:
            gain = 1.0 - 0.8*vig*(1.0-mask)
        else:
            gain = 1.0 + 0.8*(-vig)*(1.0-mask)
        bgr = np.clip(bgr*gain,0,1)

    rgb = (bgr*255).astype(np.uint8)
    img_out = Image.fromarray(cv2.cvtColor(rgb, cv2.COLOR_BGR2RGB))
    return img_out


# ---------------- API ENDPOINTS ----------------






@router.post("/remove_background")
async def remove_background(
    file: UploadFile = File(...)
):
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
    if c.startswith('#'):
        c = c[1:]
    if len(c) == 3:
        c = ''.join([ch*2 for ch in c])
    if len(c) != 6:
        raise ValueError("hex_color must be in #RRGGBB or #RGB format")
    try:
        r = int(c[0:2], 16); g = int(c[2:4], 16); b = int(c[4:6], 16)
    except ValueError:
        raise ValueError("Invalid hex_color")
    return (r, g, b)


@router.post("/recompose")
async def recompose_background(
    cutout: UploadFile = File(...),
    mode: str = Form("transparent"),
    hex_color: Optional[str] = Form(None),
    background: Optional[UploadFile] = File(None),
    bg_url: Optional[str] = Form(None)
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
            out = Image.new("RGBA", fg.size); out.paste(bg, (0,0)); out.alpha_composite(fg)
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

        buf = io.BytesIO(); out.save(buf, format="PNG"); buf.seek(0)
        return StreamingResponse(buf, media_type="image/png")
    except Exception as ex:
        logger.exception(f"Recompose failed: {ex}")
        return {"error": str(ex)}


@router.post("/retouch")
async def retouch_image(
    file: UploadFile = File(...),
    brightness: float = Form(0),
    contrast: float = Form(0),
    saturation: float = Form(0),
    shadows: float = Form(0),
    highlights: float = Form(0),
    temperature: float = Form(0),
    tint: float = Form(0),
    sharpness: float = Form(0),
    vignette: float = Form(0),
    preview: bool = Form(True)
):
    raw = await file.read()
    if not raw:
        return {"error": "empty file"}

    try:
        img = Image.open(io.BytesIO(raw))
        try:
            fg = compute_mask(img)
        except Exception as e:
            return {"error": f"Background removal failed: {e}"}
        adjustments = {
            "brightness": brightness,
            "contrast": contrast,
            "saturation": saturation,
            "shadows": shadows,
            "highlights": highlights,
            "temperature": temperature,
            "tint": tint,
            "sharpness": sharpness,
            "vignette": vignette
        }
        processed_img = apply_image_adjustments(fg, adjustments, preview=preview)

        buf = io.BytesIO()
        processed_img.save(buf, format="PNG")
        buf.seek(0)
        return StreamingResponse(buf, media_type="image/png")

    except Exception as ex:
        logger.exception(f"AI retouch failed: {ex}")
        return {"error": str(ex)}
