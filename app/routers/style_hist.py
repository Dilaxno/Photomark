from fastapi import APIRouter, UploadFile, File, Form, Request
from starlette.responses import StreamingResponse, JSONResponse
from typing import List, Optional, Tuple
import io
import zipfile
import os

import numpy as np
from PIL import Image

# Keep import for reference; implementation below uses LUTs computed from downscaled images
# from skimage.exposure import match_histograms

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.config import logger

router = APIRouter(prefix="/api/style", tags=["style"])  # matches existing /api/style namespace


# ---------- Helpers ----------

def _pil_to_np_rgb(img: Image.Image) -> np.ndarray:
    if img.mode != 'RGB':
        img = img.convert('RGB')
    arr = np.asarray(img).astype(np.uint8)
    return arr


def _np_to_pil(arr: np.ndarray) -> Image.Image:
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, mode='RGB')


def _downscale(image: Image.Image, target: int = 512) -> Image.Image:
    """Downscale preserving aspect ratio so that max(width, height) == target (or smaller)."""
    if image.mode != 'RGB':
        image = image.convert('RGB')
    w, h = image.size
    if w <= target and h <= target:
        return image
    scale = target / float(max(w, h))
    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def _cdf_3x256(np_rgb: np.ndarray) -> np.ndarray:
    """Compute per-channel CDF for uint8 RGB image. Returns array shape [3,256] in [0,1]."""
    cdfs = np.zeros((3, 256), dtype=np.float64)
    for c in range(3):
        vals = np_rgb[..., c].ravel().astype(np.uint8)
        counts = np.bincount(vals, minlength=256).astype(np.float64)
        total = counts.sum()
        if total <= 0:
            # Edge case: empty or invalid -> identity CDF ramp
            cdfs[c] = np.linspace(0.0, 1.0, 256, dtype=np.float64)
            continue
        cdf = counts.cumsum() / total
        cdfs[c] = cdf
    return cdfs


def _lut_from_cdfs(src_cdf: np.ndarray, ref_cdf: np.ndarray) -> np.ndarray:
    """Build per-channel LUT (3x256 uint8) mapping src intensities to reference via CDF matching.
    Uses interpolation over unique ref CDF values for stability.
    """
    luts = np.zeros((3, 256), dtype=np.uint8)
    xp_full = np.arange(256, dtype=np.float64)
    for c in range(3):
        s = src_cdf[c]
        r = ref_cdf[c]
        # Ensure monotonic xp for interpolation
        r_unique, idxs = np.unique(r, return_index=True)
        if r_unique.size < 2:
            # Degenerate reference distribution: no variation -> identity map
            luts[c] = xp_full.astype(np.uint8)
            continue
        fp = xp_full[idxs]
        mapped = np.interp(s, r_unique, fp)
        luts[c] = np.clip(np.rint(mapped), 0, 255).astype(np.uint8)
    return luts


def _apply_lut_rgb(src_full: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Apply per-channel LUT to full-res RGB uint8 array."""
    out = np.empty_like(src_full)
    for c in range(3):
        out[..., c] = lut[c][src_full[..., c]]
    return out


@router.post('/hist-match')
async def hist_match(
    request: Request,
    reference: UploadFile = File(..., description='Reference image to copy style from'),
    files: List[UploadFile] = File(..., description='Target images to apply reference style to'),
    fmt: Optional[str] = Form('jpg'),
    quality: Optional[float] = Form(0.92),
):
    """
    Copy the color style (exposure/color distribution) from a reference image to a batch of images.

    Performance-optimized: compute per-channel CDF mapping using 512×512 downscaled versions of
    the reference and each source, then apply the resulting LUT to the full-resolution source
    image. This preserves output quality while significantly improving speed on large inputs.

    Returns a single image if one target is provided, or a ZIP archive if multiple targets are processed.
    """
    # Auth (mirror convert/style_lut behavior)
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'convert'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    try:
        # Load reference
        ref_bytes = await reference.read()
        if not ref_bytes:
            return JSONResponse({"error": "empty reference"}, status_code=400)
        ref_img_full = Image.open(io.BytesIO(ref_bytes)).convert('RGB')
        ref_small = _downscale(ref_img_full, 512)
        ref_small_np = _pil_to_np_rgb(ref_small)
        ref_cdf = _cdf_3x256(ref_small_np)

        processed_blobs: List[tuple[str, bytes]] = []

        for f in files:
            data = await f.read()
            if not data:
                continue
            try:
                src_img_full = Image.open(io.BytesIO(data)).convert('RGB')
                src_full_np = _pil_to_np_rgb(src_img_full)

                # Compute LUT on downscaled images
                src_small = _downscale(src_img_full, 512)
                src_small_np = _pil_to_np_rgb(src_small)
                src_cdf = _cdf_3x256(src_small_np)
                lut = _lut_from_cdfs(src_cdf, ref_cdf)

                # Apply LUT to full resolution
                out_np = _apply_lut_rgb(src_full_np, lut)
                out_img = _np_to_pil(out_np)

                # Encode to requested format
                buf = io.BytesIO()
                out_fmt = (fmt or 'jpg').lower()
                if out_fmt in ('jpg', 'jpeg'):
                    q = int(max(1, min(100, round((quality or 0.92) * 100))))
                    out_img.save(buf, format='JPEG', quality=q, subsampling=0, progressive=True, optimize=True)
                    ext = 'jpg'
                else:
                    out_img.save(buf, format='PNG')
                    ext = 'png'
                buf.seek(0)
                # Decide output filename
                base = os.path.splitext(f.filename or 'image')[0]
                name = f"{base}_styled.{ext}"
                processed_blobs.append((name, buf.getvalue()))
            except Exception as ex:
                logger.exception(f"Failed to process {f.filename}: {ex}")
                # Skip this file and continue others
                continue

        if not processed_blobs:
            return JSONResponse({"error": "No images processed"}, status_code=400)

        # Single image response
        if len(processed_blobs) == 1:
            name, data = processed_blobs[0]
            media = 'image/jpeg' if name.lower().endswith('.jpg') or name.lower().endswith('.jpeg') else 'image/png'
            headers = {
                "Content-Disposition": f"attachment; filename={name}",
                "Access-Control-Expose-Headers": "Content-Disposition",
            }
            return StreamingResponse(io.BytesIO(data), media_type=media, headers=headers)

        # Multiple -> ZIP
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in processed_blobs:
                zf.writestr(name, data)
        zip_buf.seek(0)
        headers = {
            "Content-Disposition": "attachment; filename=styled_batch.zip",
            "Access-Control-Expose-Headers": "Content-Disposition",
        }
        return StreamingResponse(zip_buf, media_type='application/zip', headers=headers)

    except Exception as ex:
        logger.exception(f"Histogram matching failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
