from fastapi import APIRouter, UploadFile, File, Form, Request
import json
from fastapi.responses import StreamingResponse
from typing import Optional
import io
import hashlib
import numpy as np
import cv2
from PIL import Image

from rembg import remove, new_session  # background removal

from app.core.config import logger
from app.utils.storage import read_json_key, write_json_key
from app.core.auth import resolve_workspace_uid
from datetime import datetime as _dt

# ---------------- CONFIG ----------------
_rmbg_mask_cache = {}  # Cache masks keyed by file hash
_rmbg_session = None  # Global rembg session (lazy init)
router = APIRouter(prefix="/api/retouch", tags=["retouch"])

# ---------------- BILLING HELPERS ----------------
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
        plan = str(ent.get('plan') or '').strip().lower()
        if plan and plan != 'free':
            return True
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
def _fast_preview(img_bytes: bytes, max_size: int = 1024) -> np.ndarray:
    """Decode and resize image for preview (OpenCV)."""
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("Invalid image file")

    h, w = img.shape[:2]
    scale = min(max_size / max(h, w), 1.0)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def _encode_png(arr: np.ndarray) -> StreamingResponse:
    """Encode NumPy image to PNG and return as streaming response."""
    success, encoded = cv2.imencode(".png", arr)
    if not success:
        raise ValueError("PNG encoding failed")
    return StreamingResponse(io.BytesIO(encoded.tobytes()), media_type="image/png")


def composite_onto_background(fg: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """Composite cutout (RGBA NumPy) onto background (RGBA NumPy)."""
    fg_h, fg_w = fg.shape[:2]
    bg_h, bg_w = bg.shape[:2]

    bg_ratio = bg_w / bg_h
    fg_ratio = fg_w / fg_h
    if bg_ratio > fg_ratio:
        new_h = fg_h
        new_w = int(bg_ratio * new_h)
    else:
        new_w = fg_w
        new_h = int(new_w / bg_ratio)

    bg_resized = cv2.resize(bg, (new_w, new_h), interpolation=cv2.INTER_AREA)
    left = (new_w - fg_w) // 2
    top = (new_h - fg_h) // 2
    bg_cropped = bg_resized[top:top + fg_h, left:left + fg_w]

    # Alpha blend
    alpha = fg[:, :, 3:] / 255.0
    out = (alpha * fg[:, :, :3] + (1 - alpha) * bg_cropped[:, :, :3]).astype(np.uint8)

    out_rgba = np.dstack([out, (alpha * 255).astype(np.uint8).squeeze()])
    return out_rgba


# ---------------- SELECTIVE COLOR ----------------
def _apply_selective_color_bgr(img_bgr: np.ndarray, selective: dict) -> np.ndarray:
    """Apply per-channel selective color adjustments in HSV.
    selective is a mapping of channel name -> { hue_shift: -180..180, saturation_shift: -1..1, luminance_shift: -1..1 }

    Channels: reds, oranges, yellows, greens, aquas, blues, purples, magentas.
    """
    if not selective:
        return img_bgr

    # Convert to HSV (OpenCV: H in [0,179], S,V in [0,255])
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV).astype(np.int16)
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]

    # Define hue ranges for each channel (inclusive), in 0..179.
    # Approximate ranges with some overlap to avoid seams.
    ranges = {
        'reds':      [(0, 10), (170, 179)],
        'oranges':   [(11, 25)],
        'yellows':   [(26, 35)],
        'greens':    [(36, 85)],
        'aquas':     [(86, 95)],  # cyan
        'blues':     [(96, 130)],
        'purples':   [(131, 145)],
        'magentas':  [(146, 169)],
    }

    def channel_mask(channel: str) -> np.ndarray:
        m = np.zeros_like(H, dtype=np.uint8)
        for lo, hi in ranges.get(channel, []):
            if lo <= hi:
                m = cv2.bitwise_or(m, ((H >= lo) & (H <= hi)).astype(np.uint8))
            else:
                # wrap-around
                m = cv2.bitwise_or(m, ((H >= lo) | (H <= hi)).astype(np.uint8))
        return m

    for ch, params in selective.items():
        if ch not in ranges:
            continue
        hue_shift = float(params.get('hue_shift', 0.0))
        sat_shift = float(params.get('saturation_shift', 0.0))
        lum_shift = float(params.get('luminance_shift', 0.0))

        if abs(hue_shift) < 1e-6 and abs(sat_shift) < 1e-6 and abs(lum_shift) < 1e-6:
            continue

        mask = channel_mask(ch)
        if mask.max() == 0:
            continue

        # Expand mask to 0/1 int16
        m = mask.astype(np.int16)

        # Hue shift: degrees -> OpenCV units (180 deg == 180 units), wrap 0..179
        dH = int(round(hue_shift))
        if dH != 0:
            H[:] = ((H + dH * m) % 180)

        # Saturation shift: scale by (1 + sat_shift)
        if abs(sat_shift) > 1e-6:
            factor = 1.0 + sat_shift
            S[:] = np.clip(S + ((S * (factor - 1.0)) * m / 1).astype(np.int16), 0, 255)

        # Luminance shift approximated via V channel scale
        if abs(lum_shift) > 1e-6:
            factor = 1.0 + lum_shift
            V[:] = np.clip(V + ((V * (factor - 1.0)) * m / 1).astype(np.int16), 0, 255)

    hsv_out = np.stack([H, S, V], axis=-1).astype(np.uint8)
    out_bgr = cv2.cvtColor(hsv_out, cv2.COLOR_HSV2BGR)
    return out_bgr


# ---------------- MASK CACHING ----------------
def compute_mask(img_bytes: bytes, preview: bool = True) -> np.ndarray:
    """Compute background-removed image using rembg (cached)."""
    global _rmbg_session

    file_hash = hashlib.md5(img_bytes).hexdigest()
    if file_hash in _rmbg_mask_cache:
        return _rmbg_mask_cache[file_hash]

    try:
        if _rmbg_session is None:
            try:
                _rmbg_session = new_session(
                    providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
                )
                logger.info("rembg session initialized with GPU if available")
            except Exception as e:
                logger.exception(f"Failed GPU rembg init: {e}")
                _rmbg_session = new_session(providers=["CPUExecutionProvider"])
                logger.info("rembg session initialized with CPU")

        if preview:
            img = _fast_preview(img_bytes, max_size=1024)
            pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        else:
            pil_img = Image.open(io.BytesIO(img_bytes))

        fg_bytes = remove(
            pil_img,
            session=_rmbg_session,
            alpha_matting=True,
            alpha_matting_foreground_threshold=240,
            alpha_matting_background_threshold=10,
            alpha_matting_erode_structure_size=5,
            alpha_matting_base_size=1000,
        )

        fg = fg_bytes if isinstance(fg_bytes, Image.Image) else Image.open(io.BytesIO(fg_bytes))
        fg = fg.convert("RGBA")
        fg_np = cv2.cvtColor(np.array(fg), cv2.COLOR_RGBA2BGRA)

    except Exception as e:
        logger.exception(f"rembg background removal failed: {e}")
        raise

    _rmbg_mask_cache[file_hash] = fg_np
    return fg_np


# ---------------- API ENDPOINTS ----------------
@router.post("/remove_background")
async def remove_background(request: Request, file: UploadFile = File(...)):
    """Remove background using rembg and return transparent PNG cutout (fast preview)."""
    raw = await file.read()
    if not raw:
        return {"error": "empty file"}
    billing_uid = _billing_uid_from_request(request)
    if not _is_paid_customer(billing_uid):
        if not _consume_one_free(billing_uid, 'retouch_remove_bg'):
            return {"error": "free_limit_reached", "message": "Upgrade to continue."}
    try:
        fg = compute_mask(raw, preview=True)
        return _encode_png(fg)
    except Exception as ex:
        logger.exception(f"Background removal failed: {ex}")
        return {"error": str(ex)}


@router.post("/remove_background_masked")
async def remove_background_masked(
    request: Request,
    file: UploadFile = File(...),
    mask: UploadFile = File(...),
    feather: int = Form(0),
):
    """Remove background with user-provided mask adjustments."""
    raw = await file.read()
    mask_raw = await mask.read()
    if not raw:
        return {"error": "empty file"}
    if not mask_raw:
        return {"error": "empty mask"}

    billing_uid = _billing_uid_from_request(request)
    if not _is_paid_customer(billing_uid):
        if not _consume_one_free(billing_uid, 'retouch_remove_bg_masked'):
            return {"error": "free_limit_reached", "message": "Upgrade to continue."}

    try:
        cut = compute_mask(raw, preview=True)
        user_mask = _fast_preview(mask_raw, max_size=cut.shape[1])
        if user_mask.ndim == 3:
            user_mask = cv2.cvtColor(user_mask, cv2.COLOR_BGR2GRAY)
        user_mask = cv2.resize(user_mask, (cut.shape[1], cut.shape[0]), interpolation=cv2.INTER_LINEAR)

        if feather and feather > 0:
            user_mask = cv2.GaussianBlur(user_mask, (0, 0), sigmaX=feather)

        alpha = cut[:, :, 3]
        merged_alpha = np.maximum(alpha, user_mask)
        cut[:, :, 3] = merged_alpha
        return _encode_png(cut)
    except Exception as ex:
        logger.exception(f"Masked background removal failed: {ex}")
        return {"error": str(ex)}


@router.post("/recompose")
async def recompose_background(
    request: Request,
    cutout: UploadFile = File(...),
    mode: str = Form("transparent"),
    hex_color: Optional[str] = Form(None),
    background: Optional[UploadFile] = File(None),
    bg_url: Optional[str] = Form(None),
):
    """Recompose cutout onto new backgrounds (fast preview)."""
    try:
        cut_raw = await cutout.read()
        if not cut_raw:
            return {"error": "empty cutout"}

        cut = _fast_preview(cut_raw, max_size=1024)
        if cut.shape[2] != 4:
            cut = cv2.cvtColor(cut, cv2.COLOR_BGR2BGRA)

        billing_uid = _billing_uid_from_request(request)
        if not _is_paid_customer(billing_uid):
            if not _consume_one_free(billing_uid, 'retouch_recompose'):
                return {"error": "free_limit_reached", "message": "Upgrade to continue."}

        if mode == "transparent":
            out = cut
        elif mode == "color":
            if not hex_color:
                return {"error": "hex_color required"}
            c = hex_color.lstrip("#")
            if len(c) == 3:
                c = "".join([ch * 2 for ch in c])
            r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
            bg = np.full((cut.shape[0], cut.shape[1], 4), (b, g, r, 255), dtype=np.uint8)
            out = composite_onto_background(cut, bg)
        elif mode == "image":
            if background is None:
                return {"error": "background required"}
            bg_raw = await background.read()
            bg = _fast_preview(bg_raw, max_size=max(cut.shape[:2]))
            if bg.shape[2] != 4:
                bg = cv2.cvtColor(bg, cv2.COLOR_BGR2BGRA)
            out = composite_onto_background(cut, bg)
        elif mode == "url":
            if not bg_url:
                return {"error": "bg_url required"}
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(bg_url)
                r.raise_for_status()
                bg = _fast_preview(r.content, max_size=max(cut.shape[:2]))
                if bg.shape[2] != 4:
                    bg = cv2.cvtColor(bg, cv2.COLOR_BGR2BGRA)
            out = composite_onto_background(cut, bg)
        else:
            return {"error": "invalid mode"}

        return _encode_png(out)
    except Exception as ex:
        logger.exception(f"Recompose failed: {ex}")
        return {"error": str(ex)}



@router.post("/apply_selective")
async def apply_selective(
    request: Request,
    file: UploadFile = File(...),
    selective: str = Form("{}"),
):
    """Apply selective color grading to the uploaded image and return PNG (full resolution)."""
    try:
        raw = await file.read()
        if not raw:
            return {"error": "empty file"}

        billing_uid = _billing_uid_from_request(request)
        if not _is_paid_customer(billing_uid):
            if not _consume_one_free(billing_uid, 'retouch_selective'):
                return {"error": "free_limit_reached", "message": "Upgrade to continue."}

        try:
            selective_obj = json.loads(selective or "{}")
            if not isinstance(selective_obj, dict):
                selective_obj = {}
        except Exception:
            selective_obj = {}

        # Decode full-resolution image using OpenCV
        arr = np.frombuffer(raw, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return {"error": "invalid image"}

        out_bgr = _apply_selective_color_bgr(img, selective_obj)
        out_bgra = cv2.cvtColor(out_bgr, cv2.COLOR_BGR2BGRA)
        return _encode_png(out_bgra)
    except Exception as ex:
        logger.exception(f"Selective color apply failed: {ex}")
        return {"error": str(ex)}

# ---------------- LIGHTROOM-LIKE PIPELINE ----------------

def _apply_white_balance_bgr(img_bgr: np.ndarray, kelvin_shift: float, tint_shift: float) -> np.ndarray:
    # Simple white balance: adjust in LAB space a/b channels and global scale
    out = img_bgr.copy()
    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB).astype(np.int16)
    L, A, B = lab[...,0], lab[...,1], lab[...,2]
    # Kelvin: warm(+) pushes B up, cool(-) pushes B down. Scale small.
    B[:] = np.clip(B + int(kelvin_shift * 0.02), 0, 255)
    # Tint: green<->magenta affects A
    A[:] = np.clip(A + int(tint_shift * 0.1), 0, 255)
    lab = np.stack([L, A, B], axis=-1).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def _apply_exposure_contrast(img_bgr: np.ndarray, factor: float, stops: float, contrast: float) -> np.ndarray:
    # Exposure via scaling in linear-ish space; contrast via simple S-curve
    out = img_bgr.astype(np.float32) / 255.0
    exp = max(0.0, factor) * (2.0 ** stops)
    out *= exp
    out = np.clip(out, 0, 1)
    # Contrast: center around 0.5
    c = max(0.0, contrast)
    if abs(c - 1.0) > 1e-6:
        out = (out - 0.5) * c + 0.5
        out = np.clip(out, 0, 1)
    return (out * 255.0).astype(np.uint8)


def _apply_highlights_shadows(img_bgr: np.ndarray, highlights: float, shadows: float) -> np.ndarray:
    # Adjust via luminance mask in HSV V channel
    out = img_bgr.copy()
    hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV).astype(np.float32)
    V = hsv[...,2] / 255.0
    # Highlights: negative reduces brights, positive boosts brights (but clamp)
    if abs(highlights) > 1e-6:
        mask = np.clip((V - 0.6) * 3.0, 0, 1)  # emphasis on bright
        V = np.clip(V + mask * (highlights / 300.0), 0, 1)
    if abs(shadows) > 1e-6:
        mask = np.clip((0.4 - V) * 3.0, 0, 1)  # emphasis on dark
        V = np.clip(V + mask * (shadows / 300.0), 0, 1)
    hsv[...,2] = V * 255.0
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def _apply_vibrance_saturation(img_bgr: np.ndarray, saturation: float, vibrance: float) -> np.ndarray:
    # Saturation uniformly, vibrance less on saturated pixels
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    S = hsv[...,1] / 255.0
    if abs(vibrance - 1.0) > 1e-6:
        vib = max(0.0, vibrance)
        weight = 1.0 - S  # stronger on less saturated areas
        S = np.clip(S * (1.0 + (vib - 1.0) * weight), 0, 1)
    if abs(saturation - 1.0) > 1e-6:
        S = np.clip(S * max(0.0, saturation), 0, 1)
    hsv[...,1] = S * 255.0
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def _apply_clarity(img_bgr: np.ndarray, amount: float) -> np.ndarray:
    # Local contrast via unsharp on L channel
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)
    blur = cv2.GaussianBlur(L, (0,0), sigmaX=1.5)
    amt = max(0.0, amount - 1.0) * 2.0
    sh = cv2.addWeighted(L, 1 + amt, blur, -amt, 0)
    out = cv2.merge([np.clip(sh,0,255).astype(np.uint8), A, B])
    return cv2.cvtColor(out, cv2.COLOR_LAB2BGR)


def _apply_dehaze(img_bgr: np.ndarray, amount: float) -> np.ndarray:
    # Approx via CLAHE on L channel
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)
    clip = 2.0 + max(0.0, amount) * 6.0
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8,8))
    L2 = clahe.apply(L)
    out = cv2.merge([L2, A, B])
    return cv2.cvtColor(out, cv2.COLOR_LAB2BGR)


def _apply_sharpen(img_bgr: np.ndarray, radius: float, amount: float) -> np.ndarray:
    if amount <= 0: return img_bgr
    sigma = max(0.1, radius)
    blur = cv2.GaussianBlur(img_bgr, (0,0), sigmaX=sigma)
    return cv2.addWeighted(img_bgr, 1 + amount, blur, -amount, 0)


def _apply_noise_reduction(img_bgr: np.ndarray, lum: float, color: float) -> np.ndarray:
    out = img_bgr.copy()
    if lum > 0:
        # Bilateral filter approximates luminance NR
        d = 5
        out = cv2.bilateralFilter(out, d=d, sigmaColor=lum*50, sigmaSpace=lum*50)
    if color > 0:
        try:
            out = cv2.fastNlMeansDenoisingColored(out, None, h=10*color, hColor=10*color, templateWindowSize=7, searchWindowSize=21)
        except Exception:
            pass
    return out


def _apply_vignette(img_bgr: np.ndarray, strength: float, radius: float) -> np.ndarray:
    h, w = img_bgr.shape[:2]
    kernel_x = cv2.getGaussianKernel(w, w * max(0.001, 1.0 - radius))
    kernel_y = cv2.getGaussianKernel(h, h * max(0.001, 1.0 - radius))
    kernel = kernel_y * kernel_x.T
    mask = kernel / kernel.max()
    mask = 1 - strength * (1 - mask)
    out = np.empty_like(img_bgr)
    for c in range(3):
        out[...,c] = np.clip(img_bgr[...,c].astype(np.float32) * mask, 0, 255)
    return out.astype(np.uint8)


def _apply_film_grain(img_bgr: np.ndarray, amount: float, size: str) -> np.ndarray:
    if amount <= 0: return img_bgr
    h, w = img_bgr.shape[:2]
    noise = np.random.randn(h, w).astype(np.float32)
    if size == 'small':
        pass
    elif size == 'large':
        noise = cv2.GaussianBlur(noise, (0,0), 2.0)
    else:
        noise = cv2.GaussianBlur(noise, (0,0), 1.0)
    # apply to luminance in LAB
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[...,0] = np.clip(lab[...,0] + noise * (amount * 10.0), 0, 255)
    return cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2BGR)


def _apply_pipeline_bgr(img_bgr: np.ndarray, settings: dict) -> np.ndarray:
    s = settings or {}
    # White balance
    wb = s.get('whiteBalance') or {}
    img_bgr = _apply_white_balance_bgr(img_bgr, float(wb.get('kelvin_shift', 0)), float(wb.get('tint_shift', 0)))
    # Exposure/contrast
    br = s.get('brightness') or {}
    ct = s.get('contrast') or {}
    img_bgr = _apply_exposure_contrast(img_bgr, float(br.get('factor',1.0)), float(br.get('stops',0)), float(ct.get('factor',1.0)))
    # Highlights/Shadows
    hs = s.get('highlights_shadows') or {}
    img_bgr = _apply_highlights_shadows(img_bgr, float(hs.get('highlights',0)), float(hs.get('shadows',0)))
    # Vibrance/Saturation
    sat = s.get('saturation') or {}
    img_bgr = _apply_vibrance_saturation(img_bgr, float(sat.get('saturation_factor',1.0)), float(sat.get('vibrance_factor',1.0)))
    # Clarity
    cl = s.get('clarity') or {}
    img_bgr = _apply_clarity(img_bgr, float(cl.get('amount',1.0)))
    # Dehaze
    dh = s.get('dehaze') or {}
    img_bgr = _apply_dehaze(img_bgr, float(dh.get('amount',0)))
    # Selective color
    sel = s.get('selective_color') or {}
    if isinstance(sel, dict) and sel:
        img_bgr = _apply_selective_color_bgr(img_bgr, sel)
    # Noise reduction
    nr = s.get('noise_reduction') or {}
    img_bgr = _apply_noise_reduction(img_bgr, float(nr.get('luminance',0)), float(nr.get('color',0)))
    # Sharpen
    sh = s.get('sharpness') or {}
    img_bgr = _apply_sharpen(img_bgr, float(sh.get('radius',1.0)), float(sh.get('amount',1.0)))
    # Vignette
    vg = s.get('vignette') or {}
    img_bgr = _apply_vignette(img_bgr, float(vg.get('strength',0)), float(vg.get('radius',0.75)))
    # Film grain
    fg = s.get('film_grain') or {}
    img_bgr = _apply_film_grain(img_bgr, float(fg.get('amount',0)), str(fg.get('size','medium')))
    return img_bgr


@router.post("/apply")
async def apply_pipeline(
    request: Request,
    file: UploadFile = File(...),
    settings: str = Form("{}"),
):
    """Apply Lightroom-like pipeline using provided settings JSON; returns PNG preview/export."""
    try:
        raw = await file.read()
        if not raw:
            return {"error": "empty file"}
        billing_uid = _billing_uid_from_request(request)
        if not _is_paid_customer(billing_uid):
            if not _consume_one_free(billing_uid, 'retouch_apply'):
                return {"error": "free_limit_reached", "message": "Upgrade to continue."}
        try:
            s = json.loads(settings or "{}")
            if not isinstance(s, dict):
                s = {}
        except Exception:
            s = {}
        arr = np.frombuffer(raw, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return {"error": "invalid image"}
        out_bgr = _apply_pipeline_bgr(img, s)
        out_bgra = cv2.cvtColor(out_bgr, cv2.COLOR_BGR2BGRA)
        return _encode_png(out_bgra)
    except Exception as ex:
        logger.exception(f"Pipeline apply failed: {ex}")
        return {"error": str(ex)}






