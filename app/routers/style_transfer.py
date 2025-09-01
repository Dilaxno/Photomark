from typing import List, Optional, Tuple
import io
import os
from datetime import datetime as _dt

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from PIL import Image, ImageEnhance
import numpy as np

from app.core.config import MAX_FILES, logger
from app.core.auth import resolve_workspace_uid, has_role_access
from app.utils.storage import upload_bytes

router = APIRouter(prefix="", tags=["style-transfer"])  # serve at /style-transfer


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else float(x))


def _apply_hue_rotate(img: Image.Image, degrees: float) -> Image.Image:
    if abs(degrees) < 1e-3:
        return img
    # Convert to HSV and rotate H channel
    arr = np.asarray(img.convert('RGB'), dtype=np.uint8)
    # Normalize
    f = arr.astype(np.float32) / 255.0
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    cmax = np.max(f, axis=-1)
    cmin = np.min(f, axis=-1)
    delta = cmax - cmin + 1e-6

    # Hue computation (0..1)
    h = np.zeros_like(cmax)
    mask = delta > 1e-6
    r_is_max = (cmax == r) & mask
    g_is_max = (cmax == g) & mask
    b_is_max = (cmax == b) & mask
    h[r_is_max] = ((g[r_is_max] - b[r_is_max]) / delta[r_is_max]) % 6.0
    h[g_is_max] = ((b[g_is_max] - r[g_is_max]) / delta[g_is_max]) + 2.0
    h[b_is_max] = ((r[b_is_max] - g[b_is_max]) / delta[b_is_max]) + 4.0
    h = (h / 6.0)  # 0..1

    s = np.where(cmax <= 1e-6, 0.0, delta / (cmax + 1e-6))
    v = cmax

    # Rotate hue
    h = (h + (degrees / 360.0)) % 1.0

    # Back to RGB (HSV -> RGB)
    i = np.floor(h * 6.0)
    ffrac = (h * 6.0) - i
    p = v * (1.0 - s)
    q = v * (1.0 - ffrac * s)
    t = v * (1.0 - (1.0 - ffrac) * s)

    i = i.astype(int) % 6
    conds = [
        (i == 0, np.stack([v, t, p], axis=-1)),
        (i == 1, np.stack([q, v, p], axis=-1)),
        (i == 2, np.stack([p, v, t], axis=-1)),
        (i == 3, np.stack([p, q, v], axis=-1)),
        (i == 4, np.stack([t, p, v], axis=-1)),
        (i == 5, np.stack([v, p, q], axis=-1)),
    ]
    out = np.zeros_like(f)
    for m, val in conds:
        out[m] = val[m]
    out8 = np.clip(out * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(out8, mode='RGB')


def _apply_sepia(img: Image.Image, amount: float) -> Image.Image:
    k = _clamp01(amount)
    if k <= 1e-6:
        return img
    arr = np.asarray(img.convert('RGB'), dtype=np.float32)
    # Simple sepia matrix
    r = arr[..., 0]
    g = arr[..., 1]
    b = arr[..., 2]
    tr = 0.393 * r + 0.769 * g + 0.189 * b
    tg = 0.349 * r + 0.686 * g + 0.168 * b
    tb = 0.272 * r + 0.534 * g + 0.131 * b
    sep = np.stack([tr, tg, tb], axis=-1)
    out = arr + (sep - arr) * k
    out = np.clip(out, 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode='RGB')


def _apply_grayscale(img: Image.Image) -> Image.Image:
    return img.convert('L').convert('RGB')


def _apply_basic_adjustments(img: Image.Image, contrast: float = 1.0, saturation: float = 1.0, brightness: float = 1.0) -> Image.Image:
    out = img
    if abs(contrast - 1.0) > 1e-3:
        out = ImageEnhance.Contrast(out).enhance(contrast)
    if abs(saturation - 1.0) > 1e-3:
        out = ImageEnhance.Color(out).enhance(saturation)
    if abs(brightness - 1.0) > 1e-3:
        out = ImageEnhance.Brightness(out).enhance(brightness)
    return out


def _preset_adjustments(preset: str, k01: float) -> Tuple[float, float, float, float, float, bool]:
    """
    Return tuple: (contrast, saturation, brightness, hue_deg, sepia_amount, grayscale)
    Values approximate the frontend preview logic.
    """
    p = (preset or 'default').strip().lower()
    k = _clamp01(k01)
    if p == 'default':
        return (1.0, 1.0, 1.0, 0.0, 0.0, False)
    if p == 'film_noir':
        return (1.0 + 0.6 * k, 1.0, 1.0 - 0.05 * k, 0.0, 0.0, True)
    if p == 'golden_hour':
        return (1.0, 1.0 + 0.4 * k, 1.0 + 0.08 * k, 0.0, 0.5 * k, False)
    if p == 'hdr_cinematic':
        return (1.0 + 0.45 * k, 1.0 + 0.2 * k, 1.0 + 0.05 * k, 0.0, 0.0, False)
    if p == 'cyberpunk':
        return (1.0 + 0.25 * k, 1.0 + 0.8 * k, 1.0, (310.0 - 360.0) * k, 0.0, False)
    if p == 'teal_orange':
        return (1.0 + 0.2 * k, 1.0 + 0.5 * k, 1.0, 180.0 * k, 0.0, False)
    if p == 'stranger_things':
        return (1.0 + 0.3 * k, 1.0 + 0.4 * k, 1.0, 330.0 * k, 0.0, False)
    if p == 'blade_runner_2049':
        return (1.0 + 0.2 * k, 1.0, 1.0 + 0.05 * k, 0.0, 0.35 * k, False)
    if p == 'matrix_green':
        return (1.0 + 0.2 * k, 1.0 - 0.1 * k, 1.0, 120.0 * k, 0.0, False)
    if p == 'mad_max':
        return (1.0 + 0.35 * k, 1.0 + 0.3 * k, 1.0, 0.0, 0.45 * k, False)
    if p == 'la_la_land':
        return (1.0, 1.0 + 0.1 * k, 1.0 + 0.05 * k, 200.0 * k, 0.0, False)
    if p == 'wes_anderson':
        return (1.0 - 0.1 * k, 1.0 - 0.05 * k, 1.0 + 0.06 * k, 0.0, 0.08 * k, False)
    if p == 'john_wick_neon':
        return (1.0 + 0.2 * k, 1.0 + 0.8 * k, 1.0, 270.0 * k, 0.0, False)
    if p == 'bleach_bypass':
        return (1.0 + 0.35 * k, 1.0 - 0.5 * k, 1.0, 0.0, 0.0, False)
    if p == 'oppenheimer_bw':
        return (1.0 + 0.45 * k, 1.0, 1.0, 0.0, 0.0, True)
    if p == 'oil_painting':
        return (1.0 + 0.15 * k, 1.0 + 0.25 * k, 1.0, 0.0, 0.0, False)
    if p == 'watercolor_ink':
        return (1.0, 1.0 - 0.2 * k, 1.0 + 0.12 * k, 0.0, 0.0, False)
    if p == 'pencil_charcoal':
        return (1.0 + 0.7 * k, 1.0, 1.0, 0.0, 0.0, True)
    if p == 'pop_art':
        return (1.0 + 0.35 * k, 1.0 + 1.2 * k, 1.0, 0.0, 0.0, False)
    if p == 'abstract_surreal':
        return (1.0, 1.0 + 0.6 * k, 1.0, 120.0 * k, 0.0, False)
    if p == 'vintage_film':
        return (1.0 + 0.15 * k, 1.0 - 0.1 * k, 1.0, 0.0, 0.6 * k, False)
    if p == 'polaroid':
        return (1.0 - 0.05 * k, 1.0, 1.0 + 0.07 * k, 0.0, 0.35 * k, False)
    if p == 'sepia':
        return (1.0 + 0.1 * k, 1.0, 1.0, 0.0, 1.0 * k, False)
    if p == 'high_fashion_mag':
        return (1.0 + 0.2 * k, 1.0 + 0.05 * k, 1.0 + 0.05 * k, 0.0, 0.0, False)
    if p == 'editorial_matte':
        return (1.0 - 0.12 * k, 1.0 - 0.1 * k, 1.0 + 0.05 * k, 0.0, 0.0, False)
    if p == 'street_grit':
        return (1.0 + 0.35 * k, 1.0 + 0.2 * k, 1.0 - 0.05 * k, 0.0, 0.0, False)
    if p == 'bw_contrast':
        return (1.0 + 0.4 * k, 1.0, 1.0, 0.0, 0.0, True)
    if p == 'portrait_soft':
        return (1.0 - 0.05 * k, 1.0 + 0.05 * k, 1.0 + 0.05 * k, 0.0, 0.08 * k, False)
    if p == 'landscape_vivid':
        return (1.0 + 0.15 * k, 1.0 + 0.35 * k, 1.0, 0.0, 0.0, False)
    if p == 'portra_400':
        return (1.0 - 0.05 * k, 1.0 - 0.05 * k, 1.0 + 0.03 * k, 0.0, 0.18 * k, False)
    if p == 'kodachrome':
        return (1.0 + 0.25 * k, 1.0 + 0.25 * k, 1.0, 10.0 * k, 0.0, False)
    if p == 'cinestill_800t':
        return (1.0 + 0.1 * k, 1.0 + 0.25 * k, 1.0, 200.0 * k, 0.0, False)
    if p == 'fuji_velvia':
        return (1.0 + 0.2 * k, 1.0 + 0.6 * k, 1.0, 0.0, 0.0, False)
    if p == 'cross_process':
        return (1.0 + 0.25 * k, 1.0 + 0.2 * k, 1.0, -30.0 * k, 0.0, False)
    if p == 'lomography':
        return (1.0 + 0.2 * k, 1.0 + 0.25 * k, 1.0 + 0.05 * k, 0.0, 0.0, False)
    if p == 'soft_glow':
        return (1.0, 1.0 - 0.05 * k, 1.0 + 0.08 * k, 0.0, 0.0, False)
    if p == 'high_key':
        return (1.0 - 0.15 * k, 1.0, 1.0 + 0.2 * k, 0.0, 0.0, False)
    if p == 'low_key':
        return (1.0 + 0.25 * k, 1.0, 1.0 - 0.15 * k, 0.0, 0.0, False)
    if p == 'pastel_matte':
        return (1.0 - 0.1 * k, 1.0 - 0.2 * k, 1.0 + 0.05 * k, 0.0, 0.0, False)
    if p == 'poster':
        return (1.0 + 0.3 * k, 1.0 + 0.4 * k, 1.0, 0.0, 0.0, False)
    if p == 'anime_japanese':
        return (1.0 + 0.4 * k, 1.0 + 0.6 * k, 1.0, 0.0, 0.15 * k, False)
    if p == 'kdrama_soft':
        return (1.0 - 0.12 * k, 1.0 + 0.1 * k, 1.0 + 0.12 * k, 0.0, 0.0, False)
    if p == 'bollywood_vibrant':
        return (1.0 + 0.25 * k, 1.0 + 0.8 * k, 1.0 + 0.03 * k, 0.0, 0.2 * k, False)
    if p == 'african_tribal':
        return (1.0 + 0.35 * k, 1.0 + 0.2 * k, 1.0, 0.0, 0.12 * k, False)
    if p == 'moroccan_desert':
        return (1.0 - 0.08 * k, 1.0, 1.0 + 0.06 * k, 0.0, 0.35 * k, False)
    if p == '80s_vhs':
        return (1.0 + 0.25 * k, 1.0 + 0.2 * k, 1.0, 0.0, 0.1 * k, False)
    if p == 'duotone_magenta_cyan':
        # Approximation using hue rotate and saturation/contrast
        return (1.0 + 0.3 * k, 1.0 + 0.5 * k, 1.0, 180.0 * k, 0.0, False)
    if p == 'vintage_fade':
        return (1.0 - 0.15 * k, 1.0, 1.0 + 0.08 * k, 0.0, 0.2 * k, False)
    if p == 'retro_gameboy':
        return (1.0 + 0.6 * k, 1.0, 1.0, 0.0, 0.0, True)
    if p == 'forest_green':
        return (1.0 + 0.15 * k, 1.0 + 0.35 * k, 1.0, -10.0 * k, 0.0, False)
    if p == 'ocean_blue':
        return (1.0 + 0.15 * k, 1.0 + 0.3 * k, 1.0, 180.0 * k, 0.0, False)
    if p == 'sunset_pop':
        return (1.0 + 0.2 * k, 1.0 + 0.6 * k, 1.0, 0.0, 0.25 * k, False)
    if p == 'arctic_cool':
        return (1.0, 1.0 - 0.1 * k, 1.0 + 0.08 * k, 190.0 * k, 0.0, False)
    if p == 'fairytale_pastel':
        return (1.0, 1.0 - 0.2 * k, 1.0 + 0.12 * k, 0.0, 0.0, False)
    if p == 'dark_fantasy':
        return (1.0 + 0.35 * k, 1.0 + 0.1 * k, 1.0, 200.0 * k, 0.0, False)
    if p == 'steampunk_brass':
        return (1.0 + 0.2 * k, 1.0, 1.0, 0.0, 0.5 * k, False)
    if p == 'ethereal_glow':
        return (1.0 - 0.12 * k, 1.0, 1.0 + 0.18 * k, 0.0, 0.0, False)
    if p == 'spring_blossom':
        return (1.0, 1.0 + 0.25 * k, 1.0 + 0.05 * k, 20.0 * k, 0.0, False)
    if p == 'autumn_ember':
        return (1.0 + 0.2 * k, 1.0, 1.0, 0.0, 0.35 * k, False)
    if p == 'winter_crisp':
        return (1.0, 1.0 - 0.25 * k, 1.0 + 0.1 * k, 190.0 * k, 0.0, False)
    if p == 'summer_bright':
        return (1.0 + 0.1 * k, 1.0 + 0.5 * k, 1.0, 0.0, 0.0, False)
    # default fallback
    return (1.0, 1.0, 1.0, 0.0, 0.0, False)


def _apply_preset(img: Image.Image, preset: str, intensity01: float) -> Image.Image:
    c, s, b, hdeg, sep, gray = _preset_adjustments(preset, intensity01)
    out = img.convert('RGB')
    if gray:
        out = _apply_grayscale(out)
    if abs(hdeg) > 1e-3:
        out = _apply_hue_rotate(out, hdeg)
    out = _apply_basic_adjustments(out, contrast=c, saturation=s, brightness=b)
    if sep > 1e-6:
        out = _apply_sepia(out, sep)
    return out


@router.post('/style-transfer')
async def style_transfer(
    request: Request,
    files: List[UploadFile] = File(...),
    style_category: Optional[str] = Form(None),  # currently unused; preset drives adjustments
    style_preset: Optional[str] = Form('default'),
    intensity: Optional[float] = Form(70.0),  # 0..100 from UI
    artist: Optional[str] = Form(None),
    rename_on: Optional[str] = Form(None),
    rename_pattern: Optional[str] = Form(None),
    rename_start: Optional[int] = Form(1),
):
    """Apply a style preset server-side (approximation) and upload results to storage.
    Returns JSON with processed items and URLs.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Allow users with gallery access to write to their watermarked area
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"too many files (max {MAX_FILES})"}, status_code=400)

    k = _clamp01((intensity or 0.0) / 100.0)
    processed = []

    for idx, uf in enumerate(files):
        try:
            raw = await uf.read()
            if not raw:
                continue
            fname = uf.filename or 'image'
            base_name, ext = os.path.splitext(fname)
            ext = (ext or '.jpg').lower()
            if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff'):
                ext = ext if len(ext) <= 6 and ext.startswith('.') else '.bin'
            ct_map = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
                '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff', '.bin': 'application/octet-stream'
            }
            orig_ct = ct_map.get(ext, 'application/octet-stream')

            img = Image.open(io.BytesIO(raw)).convert('RGB')
            out = _apply_preset(img, style_preset or 'default', k)

            # Encode to JPEG; embed EXIF Artist if provided
            buf = io.BytesIO()
            try:
                import piexif  # type: ignore
                exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
                if (artist or '').strip():
                    exif_dict["0th"][piexif.ImageIFD.Artist] = artist  # type: ignore[attr-defined]
                exif_bytes = piexif.dump(exif_dict)
                out.save(buf, format='JPEG', quality=95, subsampling=0, progressive=True, optimize=True, exif=exif_bytes)
            except Exception:
                out.save(buf, format='JPEG', quality=95, subsampling=0, progressive=True, optimize=True)
            buf.seek(0)

            date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
            base_sanitized = (base_name or 'image')[:100]
            stamp = int(_dt.utcnow().timestamp())
            # 1) Save ORIGINAL as-is for mapping
            original_key = f"users/{uid}/originals/{date_prefix}/{base_sanitized}-{stamp}-orig{ext}"
            original_url = upload_bytes(original_key, raw, content_type=orig_ct)
            # 2) Save STYLED JPEG under /watermarked with original ext token and preset tag
            oext_token = (ext.lstrip('.') or 'jpg').lower()
            preset_tag = (style_preset or 'default').replace(' ', '_').lower()[:40]
            key = f"users/{uid}/watermarked/{date_prefix}/{base_sanitized}-{stamp}-{preset_tag}-o{oext_token}.jpg"
            url = upload_bytes(key, buf.getvalue(), content_type='image/jpeg')

            processed.append({
                "file": fname,
                "key": key,
                "url": url,
                "original_key": original_key,
                "original_url": original_url,
                "preset": style_preset or 'default',
            })
        except Exception as ex:
            logger.warning(f"style_transfer failed for {getattr(uf,'filename','')}: {ex}")
            continue

    return {"ok": True, "processed": processed}