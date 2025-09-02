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


from app.core.config import logger, s3, R2_BUCKET, R2_PUBLIC_BASE_URL

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


# ---------------- LUT SUPPORT (R2 + PyTorch) ----------------
try:
    import torch
except Exception as _e:
    torch = None  # Fallback if torch not installed; endpoint will error accordingly

_lut_cache = {}


def _parse_cube_text(cube_text: str):
    """Parse .cube text and return (lut_np[N,N,N,3], size). Raises on error."""
    size = None
    data = []
    for line in cube_text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0].upper() == 'LUT_3D_SIZE':
            try:
                size = int(parts[1])
            except Exception:
                raise ValueError('Invalid LUT_3D_SIZE')
            continue
        # skip known header tokens
        if parts[0].upper() in {'TITLE', 'DOMAIN_MIN', 'DOMAIN_MAX'}:
            continue
        # data row: r g b (floats)
        if len(parts) >= 3:
            try:
                r, g, b = float(parts[0]), float(parts[1]), float(parts[2])
            except Exception:
                continue
            data.append((r, g, b))
    if size is None:
        raise ValueError('LUT_3D_SIZE not found')
    expected = size * size * size
    if len(data) < expected:
        raise ValueError(f'LUT has {len(data)} entries, expected {expected}')
    import numpy as _np
    arr = _np.array(data[:expected], dtype=_np.float32)
    # .cube is usually ordered blue-fastest or red-fastest; common convention is r major then g then b.
    # Many .cube files layout as for r in 0..N-1, g in 0..N-1, b in 0..N-1: write rgb.
    # We'll reshape accordingly: (N, N, N, 3)
    arr = arr.reshape((size, size, size, 3))
    return arr, size


def _load_lut_from_r2(lut_key: str):
    """Load LUT text from R2 and return CPU torch tensor shaped (N^3, 3) and size.
    Tries S3 API first; falls back to public URL if available.
    """
    global _lut_cache
    if lut_key in _lut_cache:
        return _lut_cache[lut_key]

    from typing import Optional
    text: Optional[str] = None

    # Try S3 API if configured
    if s3 is not None:
        bucket_name = R2_BUCKET or 'luts'
        try:
            obj = s3.Object(bucket_name, lut_key)
            body = obj.get()['Body'].read()
            text = body.decode('utf-8', errors='ignore')
        except Exception as e:
            logger.warning(f'S3 fetch failed for {lut_key}, will try public URL if configured: {e}')

    # Fallback to public URL (supports absolute URL or base + key)
    if text is None:
        try:
            import httpx
            url = None
            lk = (lut_key or '').strip()
            if lk.lower().startswith('http://') or lk.lower().startswith('https://'):
                url = lk
            elif R2_PUBLIC_BASE_URL:
                url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{lk.lstrip('/')}"
            if url:
                with httpx.Client(timeout=15.0) as client:
                    r = client.get(url)
                    r.raise_for_status()
                    text = r.text
        except Exception as e:
            logger.exception(f'HTTP fetch failed for LUT {lut_key}: {e}')
            raise

    if text is None:
        raise RuntimeError('Unable to load LUT: missing S3 config/public URL or object not found')

    arr, size = _parse_cube_text(text)
    import torch as _torch
    lut_tensor = _torch.from_numpy(arr.reshape(size * size * size, 3))  # CPU tensor (float32)
    _lut_cache[lut_key] = (lut_tensor, size)
    return lut_tensor, size


@router.get('/luts')
async def list_luts():
    """List available .cube LUT keys under the 'luts/' prefix.
    Order of attempts:
    1) If S3 configured and R2_BUCKET set, list objects with prefix 'luts/'.
    2) Else if R2_PUBLIC_BASE_URL available, try to fetch a JSON index at 'luts/index.json' containing an array of keys.
    3) Fallback: empty list.
    """
    try:
        # 1) S3 listing
        if s3 is not None and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            keys = []
            for obj in bucket.objects.filter(Prefix='luts/'):
                key = obj.key
                if key.lower().endswith('.cube'):
                    keys.append(key)
            return keys

        # 2) Public URL index
        if R2_PUBLIC_BASE_URL:
            import httpx
            url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/luts/index.json"
            try:
                with httpx.Client(timeout=10.0) as client:
                    r = client.get(url)
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list):
                            return [str(k) for k in data if str(k).lower().endswith('.cube')]
            except Exception:
                pass
        return []
    except Exception as e:
        logger.exception(f'Failed to list LUTs: {e}')
        return []


@router.post('/apply_lut')
async def apply_lut(
    file: UploadFile = File(...),
    lut: str = Form(...),  # key or filename of LUT in the bucket
):
    """Apply a 3D .cube LUT using PyTorch (CUDA if available). Returns PNG."""
    if torch is None:
        return {"error": "torch not available on server"}
    raw = await file.read()
    if not raw:
        return {"error": "empty file"}
    try:
        # Load image
        img = Image.open(io.BytesIO(raw)).convert('RGB')
        img_np = np.array(img, dtype=np.uint8)
        h, w, _ = img_np.shape

        lut_key = lut if lut.lower().endswith('.cube') else (lut + '.cube')
        lut_tensor_cpu, size = _load_lut_from_r2(lut_key)

        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        # Move image to device
        img_t = torch.from_numpy(img_np).to(device=device, dtype=torch.float32) / 255.0  # (H,W,3)
        # Compute indices 0..size-1 for nearest-neighbor lookup
        idx = torch.clamp(torch.round(img_t * (size - 1)), 0, size - 1).to(torch.int64)  # (H,W,3)
        r_idx = idx[..., 0]
        g_idx = idx[..., 1]
        b_idx = idx[..., 2]

        # Prepare LUT on device
        lut_flat = lut_tensor_cpu.to(device)
        # Linearize 3D indices -> 1D
        lin_idx = (r_idx * size + g_idx) * size + b_idx  # (H,W)
        lin_idx_flat = lin_idx.view(-1)
        out_flat = lut_flat.index_select(0, lin_idx_flat)  # (H*W, 3)
        out = out_flat.view(h, w, 3)
        out = torch.clamp(out, 0.0, 1.0)
        out_u8 = (out * 255.0 + 0.5).to(torch.uint8).to('cpu').numpy()

        out_img = Image.fromarray(out_u8, mode='RGB')
        buf = io.BytesIO()
        out_img.save(buf, format='PNG')
        buf.seek(0)
        return StreamingResponse(buf, media_type='image/png')
    except Exception as ex:
        logger.exception(f'LUT apply failed: {ex}')
        return {"error": str(ex)}

