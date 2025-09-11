from fastapi import APIRouter, UploadFile, File, Form, Request
from typing import Optional, List
import io
import asyncio
import os
from datetime import datetime as _dt
from starlette.concurrency import run_in_threadpool
from PIL import Image

from app.core.config import MAX_FILES, logger
from app.core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access
from app.utils.storage import upload_bytes
from app.utils.watermark import add_text_watermark, add_signature_watermark
from app.utils.invisible_mark import detect_signature, payload_matches_uid, PAYLOAD_LEN

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
    artist: str | None = Form(None),
    source: str | None = Form(None),
    no_original: str | None = Form(None),
):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return {"error": "Unauthorized"}
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return {"error": "Forbidden"}
    uid = eff_uid

    try:
        raw = await file.read()
        if not raw:
            return {"error": "empty file"}

        fname = file.filename or "image"
        orig_ext = (os.path.splitext(fname)[1] or '.jpg').lower()
        if orig_ext not in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff'):
            orig_ext = orig_ext if len(orig_ext) <= 6 and orig_ext.startswith('.') else '.bin'

        ct_map = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
            '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff', '.bin': 'application/octet-stream'
        }
        orig_ct = ct_map.get(orig_ext, 'application/octet-stream')

        # Open & convert image in threadpool (non-blocking)
        img = await run_in_threadpool(Image.open, io.BytesIO(raw))
        img = await run_in_threadpool(img.convert, 'RGB')

        # Encode to JPEG (faster settings: quality=85, no optimize/progressive)
        buf = io.BytesIO()
        async def _save_image():
            try:
                if PIEXIF_AVAILABLE and (artist or '').strip():
                    import piexif
                    from piexif import ImageIFD
                    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
                    exif_dict["0th"][ImageIFD.Artist] = artist
                    exif_bytes = piexif.dump(exif_dict)
                    img.save(buf, format='JPEG', quality=85, subsampling=0, exif=exif_bytes)
                else:
                    img.save(buf, format='JPEG', quality=85, subsampling=0)
            except Exception:
                img.save(buf, format='JPEG', quality=85, subsampling=0)
        await run_in_threadpool(_save_image)
        buf.seek(0)

        date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
        base = os.path.splitext(os.path.basename(fname))[0][:100] or 'image'
        stamp = int(_dt.utcnow().timestamp())

        src = (source or '').strip().lower()
        is_edited = src == 'edited'
        skip_original = (no_original or '').strip() == '1'
        save_original = not is_edited and not skip_original

        original_key = None
        tasks = []

        # Prepare upload tasks
        if save_original:
            original_key = f"users/{uid}/originals/{date_prefix}/{base}-{stamp}-orig{orig_ext}"
            tasks.append(asyncio.to_thread(upload_bytes, original_key, raw, content_type=orig_ct))

        oext_token = (orig_ext.lstrip('.') or 'jpg').lower()
        suffix = 'edit' if is_edited else 'txt'
        key = f"users/{uid}/watermarked/{date_prefix}/{base}-{stamp}-{suffix}-o{oext_token}.jpg"
        tasks.append(asyncio.to_thread(upload_bytes, key, buf.getvalue(), content_type='image/jpeg'))

        # Run uploads concurrently
        results = await asyncio.gather(*tasks)
        resp = {"ok": True, "key": key, "url": results[-1]}  # last result = watermarked

        if save_original and original_key:
            resp.update({"original_key": original_key, "original_url": results[0]})

        return resp

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

    # Determine original file extension and content-type
    fname = file.filename or "image"
    orig_ext = (os.path.splitext(fname)[1] or '.jpg').lower()
    if orig_ext not in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff'):
        orig_ext = orig_ext if len(orig_ext) <= 6 and orig_ext.startswith('.') else '.bin'
    ct_map = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
        '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff', '.bin': 'application/octet-stream'
    }
    orig_ct = ct_map.get(orig_ext, 'application/octet-stream')

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
    base = os.path.splitext(os.path.basename(fname))[0][:100] or 'image'
    stamp = int(_dt.utcnow().timestamp())
    suffix = "sig" if (use_signature and signature_bytes) else "txt"

    # 1) Save ORIGINAL as-is
    original_key = f"users/{uid}/originals/{date_prefix}/{base}-{stamp}-orig{orig_ext}"
    original_url = upload_bytes(original_key, raw, content_type=orig_ct)

    # 2) Save WATERMARKED with original ext token for mapping
    oext_token = (orig_ext.lstrip('.') or 'jpg').lower()
    key = f"users/{uid}/watermarked/{date_prefix}/{base}-{stamp}-{suffix}-o{oext_token}.jpg"

    url = upload_bytes(key, buf.getvalue(), content_type="image/jpeg")
    return {"key": key, "url": url, "original_key": original_key, "original_url": original_url}


