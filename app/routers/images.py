from fastapi import APIRouter, UploadFile, File, Form, Request
from typing import Optional, List
import io
import os
from datetime import datetime as _dt
from PIL import Image

from app.core.config import MAX_FILES, logger
from app.core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access
from app.utils.storage import upload_bytes
from app.utils.watermark import add_text_watermark, add_signature_watermark

try:
    import piexif  # type: ignore
    PIEXIF_AVAILABLE = True
except Exception:
    piexif = None  # type: ignore
    PIEXIF_AVAILABLE = False

router = APIRouter(prefix="/api", tags=["images"])


@router.post("/images/upload")
async def images_upload(
    request: Request,
    file: UploadFile = File(...),
    destination: str = Form("r2"),
    artist: Optional[str] = Form(None),
):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return {"error": "Unauthorized"}
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return {"error": "Forbidden"}
    uid = eff_uid
    try:
        raw = await file.read()
        fname = file.filename or "image"
        ext = os.path.splitext(fname)[1].lower().lstrip(".") or "jpg"
        content_type = file.content_type or (
            "image/jpeg" if ext in ("jpg", "jpeg") else ("image/png" if ext == "png" else "application/octet-stream")
        )
        date_prefix = _dt.utcnow().strftime("%Y/%m/%d")
        base = os.path.splitext(os.path.basename(fname))[0][:100]
        stamp = int(_dt.utcnow().timestamp())
        final_ext = "jpg" if ext in ("jpg", "jpeg") else ("png" if ext == "png" else ext)
        key = f"users/{uid}/watermarked/{date_prefix}/{base}-{stamp}.{final_ext}"
        url = upload_bytes(key, raw, content_type=content_type)
        return {"ok": True, "key": key, "url": url}
    except Exception as ex:
        logger.exception(f"Upload failed: {ex}")
        return {"error": str(ex)}


@router.post("/images/watermark")
async def images_watermark(
    request: Request,
    file: UploadFile = File(...),
    watermark_text: Optional[str] = Form(None),
    position: str = Form("bottom-right"),
    signature: Optional[UploadFile] = File(None),
    use_signature: Optional[bool] = Form(False),
    color: Optional[str] = Form(None),
    opacity: Optional[float] = Form(None),
    artist: Optional[str] = Form(None),
):
    """Apply text or signature watermark and store the result.
    - If use_signature=True and a signature file is provided, overlay the signature.
    - Else if watermark_text is provided, overlay text.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return {"error": "Unauthorized"}
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return {"error": "Forbidden"}
    uid = eff_uid

    raw = await file.read()
    if not raw:
        return {"error": "empty file"}

    # Optional signature bytes
    signature_bytes = await signature.read() if (use_signature and signature is not None) else None

    img = Image.open(io.BytesIO(raw)).convert("RGB")

    if use_signature and signature_bytes:
        sig = Image.open(io.BytesIO(signature_bytes)).convert("RGBA")
        out = add_signature_watermark(img, sig, position)
    else:
        if not watermark_text:
            return {"error": "watermark_text required when not using signature"}
        out = add_text_watermark(
            img,
            watermark_text,
            position,
            color=color or None,
            opacity=opacity if opacity is not None else None,
        )

    # Encode JPEG with optional EXIF Artist
    buf = io.BytesIO()
    try:
        if PIEXIF_AVAILABLE and artist:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
            exif_dict["0th"][piexif.ImageIFD.Artist] = artist
            exif_bytes = piexif.dump(exif_dict)
            out.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True, exif=exif_bytes)
        else:
            out.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True)
    except Exception:
        out.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True)
    buf.seek(0)

    date_prefix = _dt.utcnow().strftime("%Y/%m/%d")
    base = os.path.splitext(os.path.basename(file.filename or "image"))[0]
    stamp = int(_dt.utcnow().timestamp())
    suffix = "sig" if (use_signature and signature_bytes) else "txt"
    key = f"users/{uid}/watermarked/{date_prefix}/{base}-{stamp}-{suffix}.jpg"

    url = upload_bytes(key, buf.getvalue(), content_type="image/jpeg")
    return {"key": key, "url": url}