from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from typing import Optional
import io
import os
import uuid as _uuid
import hashlib

from PIL import Image, ImageEnhance, ImageFilter, ImageColor
import numpy as np
import cv2

from rembg import remove  # <--- Using rembg for background removal

from app.core.config import logger
from app.utils.storage import upload_bytes

# ---------------- CONFIG ----------------
_vignette_cache = {}
_rmbg_mask_cache = {}  # Cache masks keyed by file hash

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
    raw_bytes = img.tobytes()
    file_hash = hashlib.md5(raw_bytes).hexdigest()

    if file_hash in _rmbg_mask_cache:
        return _rmbg_mask_cache[file_hash]

    try:
        fg = remove(img)  # rembg removes background and returns RGBA
    except Exception as e:
        logger.exception(f"rembg background removal failed: {e}")
        fg = img.convert("RGBA")  # fallback to original

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
@router.post("/background")
async def background_replace(
    file: UploadFile = File(...),
    background_url: Optional[str] = Form(None),
    replace_with: Optional[str] = Form(None)
):
    raw = await file.read()
    if not raw:
        return {"error": "empty file"}

    try:
        inp = Image.open(io.BytesIO(raw))
        fg = compute_mask(inp)

        # Composite background if provided
        if background_url:
            bg_bytes = await fetch_bytes(background_url)
            bg_img = Image.open(io.BytesIO(bg_bytes))
            out = composite_onto_background(fg, bg_img)
        elif replace_with:
            rep = (replace_with or '').strip()
            rep_l = rep.lower()
            if rep_l in ('blur', 'blurred'):
                # Create blurred background from the original input image
                base = inp.convert('RGB')
                # Heuristic blur radius based on image size
                w, h = base.size
                radius = max(10, int(min(w, h) * 0.03))
                blurred = base.filter(ImageFilter.GaussianBlur(radius=radius)).convert('RGBA')
                out = composite_onto_background(fg, blurred)
            else:
                try:
                    rgb = ImageColor.getrgb(rep)
                    bg_img = Image.new('RGBA', fg.size, rgb + (255,))
                    out = composite_onto_background(fg, bg_img)
                except Exception:
                    # If color parsing fails, fall back to the foreground-only output
                    out = fg
        else:
            out = fg

        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)
        suffix = _uuid.uuid4().hex[:8]
        key = f"retouch/ai-bg/{file.filename}-{suffix}.png"
        url = upload_bytes(key, buf.getvalue(), content_type="image/png")
        return {"ok": True, "url": f"{url}?v={suffix}", "key": key}

    except Exception as ex:
        logger.exception(f"AI background replace failed: {ex}")
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
        fg = compute_mask(img)
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
