from typing import List, Optional
import io
import os
from datetime import datetime as _dt

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image
import zipfile

from app.core.config import MAX_FILES, logger
from app.core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access
from app.utils.watermark import (
    add_text_watermark,
    add_signature_watermark,
    add_text_watermark_tiled,
    add_signature_watermark_tiled,
)
from app.utils.storage import upload_bytes, read_json_key
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
    # Visible layout mode and tiling params
    wm_layout: Optional[str] = Form("single"),
    tile_angle: Optional[float] = Form(None),
    tile_spacing: Optional[float] = Form(None),
    tile_scale: Optional[float] = Form(None),
    # Single-layout background box flag
    wm_bg_box: Optional[str] = Form(None),  # '1' to enable background box on single watermark
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

            # Build watermark (supports single or tiled layout)
            layout = (wm_layout or 'single').strip().lower()
            if use_logo:
                sig = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")  # type: ignore[arg-type]
                if layout == 'tiled':
                    out = add_signature_watermark_tiled(
                        img,
                        sig,
                        angle_deg=float(tile_angle or 0.0),
                        spacing_rel=float(tile_spacing or 0.3),
                        scale_mul=float(tile_scale or 1.0),
                    )
                else:
                    out = add_signature_watermark(
                        img,
                        sig,
                        wm_pos,
                        bg_box=((wm_bg_box or '').strip() == '1')
                    )
            else:
                if layout == 'tiled':
                    out = add_text_watermark_tiled(
                        img,
                        watermark or '',
                        color=wm_color or None,
                        opacity=wm_opacity if wm_opacity is not None else None,
                        angle_deg=float(tile_angle or 0.0),
                        spacing_rel=float(tile_spacing or 0.3),
                        scale_mul=float(tile_scale or 1.0),
                    )
                else:
                    out = add_text_watermark(
                        img,
                        watermark or '',
                        wm_pos,
                        color=wm_color or None,
                        opacity=wm_opacity if wm_opacity is not None else None,
                        bg_box=((wm_bg_box or '').strip() == '1')
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


@router.post("/api/uploads")
async def upload_external(
    request: Request,
    files: List[UploadFile] = File(...),
):
    """Upload raw user photos (no watermark) into users/{uid}/external/.
    - Requires gallery access for the effective workspace.
    - Stores files under users/{uid}/external/YYYY/MM/DD/base-stamp.ext
    - Returns list of uploaded items with keys and public URLs.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Gallery managers/owners can upload into their own external area
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)

    # Dynamic per-plan cap: photographers/agencies get higher limit if configured
    max_cap = MAX_FILES
    try:
        ent = read_json_key(f"users/{uid}/billing/entitlement.json") or {}
        plan = str(ent.get("plan") or "").lower()
        is_paid = bool(ent.get("isPaid") or False)
        import os
        paid_cap = int(os.getenv("UPLOAD_MAX_PAID", "1000"))
        free_cap = int(os.getenv("UPLOAD_MAX_FREE", str(MAX_FILES)))
        # Normalize known paid plans to get 1000 cap by default
        if is_paid and ("agenc" in plan or "photograph" in plan):
            max_cap = max(MAX_FILES, min(paid_cap, 5000))
        else:
            max_cap = free_cap if not is_paid else min(paid_cap, 5000)
    except Exception:
        max_cap = MAX_FILES

    if len(files) > max_cap:
        return JSONResponse({"error": f"too many files (max {max_cap})"}, status_code=400)

    uploaded = []
    for uf in files:
        try:
            raw = await uf.read()
            if not raw:
                continue
            # Determine original file extension and content-type
            orig_ext = (os.path.splitext(uf.filename or '')[1] or '.jpg').lower()
            if not orig_ext.startswith('.') or len(orig_ext) > 8:
                orig_ext = '.jpg'
            # Normalize some odd cases
            if orig_ext not in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff', '.gif'):
                orig_ext = '.jpg'
            ct_map = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
                '.heic': 'image/heic', '.tif': 'image/tiff', '.tiff': 'image/tiff', '.gif': 'image/gif'
            }
            orig_ct = ct_map.get(orig_ext, 'application/octet-stream')

            date_prefix = _dt.utcnow().strftime('%Y/%m/%d')
            base = os.path.splitext(os.path.basename(uf.filename or 'upload'))[0] or 'upload'
            stamp = int(_dt.utcnow().timestamp())
            key = f"users/{uid}/external/{date_prefix}/{base}-{stamp}{orig_ext}"
            url = upload_bytes(key, raw, content_type=orig_ct)
            uploaded.append({"key": key, "url": url, "name": os.path.basename(key)})
        except Exception as ex:
            logger.warning(f"external upload failed for {getattr(uf,'filename', '')}: {ex}")
            continue

    return {"ok": True, "uploaded": uploaded}


# New: process and return a ZIP without uploading anywhere
@router.post("/process/watermark-zip")
async def process_watermark_zip(
    request: Request,
    files: List[UploadFile] = File(...),
    watermark: Optional[str] = Form(None),
    wm_pos: str = Form("bottom-right"),
    signature: Optional[UploadFile] = File(None),  # legacy
    logo: Optional[UploadFile] = File(None),       # preferred
    wm_color: Optional[str] = Form(None),
    wm_opacity: Optional[float] = Form(None),
    wm_layout: Optional[str] = Form("single"),
    tile_angle: Optional[float] = Form(None),
    tile_spacing: Optional[float] = Form(None),
    tile_scale: Optional[float] = Form(None),
    wm_bg_box: Optional[str] = Form(None),
    artist: Optional[str] = Form(None),
    invisible: Optional[str] = Form(None),
):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'retouch'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)

    # Read logo/signature if provided
    logo_file = logo or signature
    logo_bytes = await (logo_file.read() if logo_file is not None else None)
    use_logo = bool(logo_bytes)

    if not use_logo and not (watermark or '').strip():
        return JSONResponse({"error": "watermark text required or provide logo"}, status_code=400)

    # Helper to process a single file and return (filename, jpeg_bytes)
    async def _process_one(uf: UploadFile) -> Optional[tuple[str, bytes]]:
        try:
            raw = await uf.read()
            if not raw:
                return None
            img = Image.open(io.BytesIO(raw)).convert("RGB")

            layout = (wm_layout or 'single').strip().lower()
            if use_logo:
                sig = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")  # type: ignore[arg-type]
                if layout == 'tiled':
                    out = add_signature_watermark_tiled(
                        img,
                        sig,
                        angle_deg=float(tile_angle or 0.0),
                        spacing_rel=float(tile_spacing or 0.3),
                        scale_mul=float(tile_scale or 1.0),
                    )
                else:
                    out = add_signature_watermark(
                        img,
                        sig,
                        wm_pos,
                        bg_box=((wm_bg_box or '').strip() == '1')
                    )
            else:
                if layout == 'tiled':
                    out = add_text_watermark_tiled(
                        img,
                        watermark or '',
                        color=wm_color or None,
                        opacity=wm_opacity if wm_opacity is not None else None,
                        angle_deg=float(tile_angle or 0.0),
                        spacing_rel=float(tile_spacing or 0.3),
                        scale_mul=float(tile_scale or 1.0),
                    )
                else:
                    out = add_text_watermark(
                        img,
                        watermark or '',
                        wm_pos,
                        color=wm_color or None,
                        opacity=wm_opacity if wm_opacity is not None else None,
                        bg_box=((wm_bg_box or '').strip() == '1')
                    )

            # Optional invisible signature
            try:
                if (invisible or '').strip() == '1':
                    payload = build_payload_for_uid(uid)
                    out = embed_invisible(out, payload)
            except Exception as _ex:
                logger.warning(f"invisible embed (zip) failed: {_ex}")

            # Encode JPEG with optional EXIF Artist
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

            base = os.path.splitext(os.path.basename(uf.filename or 'image'))[0] or 'image'
            name = f"{base}-watermarked.jpg"
            return (name, buf.getvalue())
        except Exception as ex:
            logger.warning(f"zip process failed for {getattr(uf,'filename','')}: {ex}")
            return None

    # If only one file, return the single JPEG directly
    if len(files) == 1:
        one = await _process_one(files[0])
        if not one:
            return JSONResponse({"error": "processing failed"}, status_code=400)
        name, data = one
        headers = { 'Content-Disposition': f'attachment; filename="{name}"' }
        return StreamingResponse(io.BytesIO(data), media_type='image/jpeg', headers=headers)

    # Otherwise, build a ZIP with manifest
    mem = io.BytesIO()
    mappings: list[tuple[str, str]] = []
    with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        used_names: set[str] = set()
        def _unique_name(n: str) -> str:
            base, ext = os.path.splitext(n)
            cand = n
            i = 1
            while cand in used_names:
                cand = f"{base}_{i}{ext}"
                i += 1
            used_names.add(cand)
            return cand
        for uf in files:
            res = await _process_one(uf)
            if not res:
                continue
            name, data = res
            final_name = _unique_name(name)
            orig = os.path.basename(uf.filename or '') or 'image.jpg'
            mappings.append((orig, final_name))
            zf.writestr(final_name, data)
        # Write manifest
        try:
            if mappings:
                lines = ["Original Filename -> Output Filename"] + [f"{o} -> {n}" for (o, n) in mappings]
                zf.writestr('manifest.txt', "\n".join(lines))
        except Exception:
            pass

    mem.seek(0)
    stamp = _dt.utcnow().strftime('%Y%m%d-%H%M%S')
    headers = {
        'Content-Disposition': f'attachment; filename="watermarked-{stamp}.zip"'
    }
    return StreamingResponse(mem, media_type='application/zip', headers=headers)