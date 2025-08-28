from typing import List, Optional
import io
import os
from datetime import datetime as _dt

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from PIL import Image

from app.core.config import MAX_FILES, logger
from app.core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access
from app.utils.watermark import add_text_watermark, add_signature_watermark
from app.utils.storage import upload_bytes
from app.utils.invisible_mark import embed_signature as embed_invisible, build_payload_for_uid

# Import vault helpers to update vaults after upload
from app.routers.vaults import (
    _read_vault, _write_vault, _vault_key,
    _read_vault_meta, _write_vault_meta, _unlock_vault,
    _vault_salt, _hash_password
)

router = APIRouter(prefix="", tags=["upload"])  # no prefix to serve /upload


@router.post("/upload")
async def upload(
    request: Request,
    files: List[UploadFile] = File(...),
    watermark: Optional[str] = Form(None),
    wm_pos: str = Form("bottom-right"),
    signature: Optional[UploadFile] = File(None),  # legacy field name
    logo: Optional[UploadFile] = File(None),       # new preferred field name
    wm_color: Optional[str] = Form(None),
    wm_opacity: Optional[float] = Form(None),
    artist: Optional[str] = Form(None),
    invisible: Optional[str] = Form(None),  # '1' to embed invisible signature
    # Destination options
    vault_mode: str = Form("all"),  # 'all' | 'existing' | 'new'
    vault_name: Optional[str] = Form(None),
    vault_protect: Optional[str] = Form(None),  # '1' to protect new vault
    vault_password: Optional[str] = Form(None),
):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Upload writes to user's watermarked area; allowed for admin and retoucher roles
    if not has_role_access(req_uid, eff_uid, 'retouch'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"too many files (max {MAX_FILES})"}, status_code=400)

    # Read logo/signature if provided (support both for backward compatibility)
    logo_file = logo or signature
    logo_bytes = await logo_file.read() if logo_file is not None else None
    use_logo = bool(logo_bytes)

    # Validate text mode
    if not use_logo and not (watermark or '').strip():
        return JSONResponse({"error": "watermark text required or provide logo"}, status_code=400)

    uploaded = []

    idx = 0
    for uf in files:
        try:
            raw = await uf.read()
            if not raw:
                continue
            img = Image.open(io.BytesIO(raw)).convert("RGB")

            # Determine original file extension and content-type
            orig_ext = (os.path.splitext(uf.filename or '')[1] or '.jpg').lower()
            # Normalize some odd cases
            if orig_ext not in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff'):
                orig_ext = orig_ext if len(orig_ext) <= 6 and orig_ext.startswith('.') else '.bin'
            ct_map = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
                '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff', '.bin': 'application/octet-stream'
            }
            orig_ct = ct_map.get(orig_ext, 'application/octet-stream')

            # Build watermark
            if use_logo:
                sig = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")  # type: ignore[arg-type]
                out = add_signature_watermark(img, sig, wm_pos)
            else:
                out = add_text_watermark(
                    img,
                    watermark or '',
                    wm_pos,
                    color=wm_color or None,
                    opacity=wm_opacity if wm_opacity is not None else None,
                )

            # Optionally embed invisible signature linked to the account uid
            try:
                if (invisible or '').strip() == '1':
                    payload = build_payload_for_uid(uid)
                    out = embed_invisible(out, payload)
            except Exception as _ex:
                logger.warning(f"invisible embed failed: {_ex}")

            # Encode watermarked JPEG with optional EXIF Artist
            buf = io.BytesIO()
            try:
                import piexif  # type: ignore
                exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
                if (artist or '').strip():
                    exif_dict["0th"][piexif.ImageIFD.Artist] = artist  # type: ignore[attr-defined]
                exif_bytes = piexif.dump(exif_dict)
                out.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True, exif=exif_bytes)
            except Exception:
                out.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True)
            buf.seek(0)

            date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
            base = os.path.splitext(os.path.basename(uf.filename or 'image'))[0] or 'image'
            stamp = int(_dt.utcnow().timestamp())
            suffix = 'logo' if use_logo else 'txt'

            # 1) Upload ORIGINAL as-is under /originals with deterministic name including original ext
            original_key = f"users/{uid}/originals/{date_prefix}/{base}-{stamp}-orig{orig_ext}"
            original_url = upload_bytes(original_key, raw, content_type=orig_ct)

            # 2) Upload WATERMARKED jpeg under /watermarked and encode original ext token into name for mapping
            oext_token = (orig_ext.lstrip('.') or 'jpg').lower()
            key = f"users/{uid}/watermarked/{date_prefix}/{base}-{stamp}-{suffix}-o{oext_token}.jpg"
            url = upload_bytes(key, buf.getvalue(), content_type='image/jpeg')

            uploaded.append({"key": key, "url": url, "original_key": original_key, "original_url": original_url})
            idx += 1
        except Exception as ex:
            logger.warning(f"upload failed for {getattr(uf,'filename', '')}: {ex}")
            continue

    # Vault handling
    final_vault = None
    try:
        vm = (vault_mode or 'all').strip().lower()
        if vm == 'existing':
            name = (vault_name or '').strip()
            if name and uploaded:
                exist = _read_vault(uid, name)
                merged = sorted(set(exist) | {u['key'] for u in uploaded})
                _write_vault(uid, name, merged)
                final_vault = _vault_key(uid, name)[1]
        elif vm == 'new':
            name = (vault_name or '').strip()
            if name:
                keys_now = [u['key'] for u in uploaded]
                _write_vault(uid, name, keys_now)
                prot = (vault_protect or '').strip() == '1'
                if prot and (vault_password or '').strip():
                    salt = _vault_salt(uid, name)
                    _write_vault_meta(uid, name, {"protected": True, "hash": _hash_password(vault_password or '', salt)})
                final_vault = _vault_key(uid, name)[1]
    except Exception as ex:
        logger.warning(f"vault update failed: {ex}")

    return {"ok": True, "uploaded": uploaded, "vault": final_vault}