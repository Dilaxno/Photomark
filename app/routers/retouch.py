from fastapi import APIRouter, UploadFile, File, Form, Request
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







