from typing import List, Optional, Literal
import os
import io
import zipfile
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from app.core.config import MAX_FILES, logger
from app.core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access

try:
    from wand.image import Image as WandImage
    WAND_AVAILABLE = True
except Exception:
    WAND_AVAILABLE = False

try:
    import piexif  # type: ignore
    PIEXIF_AVAILABLE = True
except Exception:
    piexif = None  # type: ignore
    PIEXIF_AVAILABLE = False

router = APIRouter(prefix="/api", tags=["convert"])

SupportedTarget = Literal['psd', 'tiff', 'png', 'jpeg', 'jpg', 'gif', 'svg', 'eps', 'pdf']

# ==========================
# Top-level worker function
# ==========================
def convert_one(raw: bytes, filename: str, target: str, artist: Optional[str]) -> tuple[str, Optional[bytes]]:
    try:
        # Load image with Wand
        with WandImage(blob=raw) as img:
            if len(img.sequence) > 1:
                with WandImage(image=img.sequence[0]) as first:
                    img = first.clone()

            out_blob = None
            with WandImage(image=img) as out:
                out_ext = target
                if target in ('svg', 'eps'):
                    try:
                        out.format = target
                        out_blob = out.make_blob()
                        out_ext = target
                    except Exception:
                        out.format = 'pdf'
                        out_blob = out.make_blob()
                        out_ext = 'pdf'
                else:
                    out.format = target
                    out_blob = out.make_blob()
                    out_ext = target

        # Embed metadata
        if artist and out_ext in ("jpeg", "jpg"):
            _im = Image.open(io.BytesIO(out_blob)).convert("RGB")
            if PIEXIF_AVAILABLE:
                try:
                    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
                    exif_dict["0th"][piexif.ImageIFD.Artist] = artist
                    exif_bytes = piexif.dump(exif_dict)
                    buf = io.BytesIO()
                    _im.save(buf, format="JPEG", quality=95, subsampling=0,
                             progressive=True, optimize=True, exif=exif_bytes)
                    out_blob = buf.getvalue()
                except Exception:
                    pass
        elif artist and out_ext == "png":
            _im = Image.open(io.BytesIO(out_blob)).convert("RGBA")
            pnginfo = PngInfo()
            pnginfo.add_text("Artist", artist)
            buf = io.BytesIO()
            _im.save(buf, format="PNG", pnginfo=pnginfo, optimize=True)
            out_blob = buf.getvalue()

        base = os.path.splitext(os.path.basename(filename or 'image'))[0] or 'image'
        arcname = f"{base}.{out_ext}"
        return arcname, out_blob
    except Exception as ex:
        logger.error("convert_one failed for %s: %s", filename, ex)
        return filename, None

# Helper for ProcessPoolExecutor
def _convert_one_unpack(args):
    return convert_one(*args)

# ==========================
# Endpoint
# ==========================
@router.post("/convert/bulk")
async def convert_bulk(
    request: Request,
    files: List[UploadFile] = File(...),
    target: SupportedTarget = Form(...),
    artist: Optional[str] = Form(None),
):
    # Authentication / workspace check
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'convert'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not WAND_AVAILABLE:
        return JSONResponse({"error": "ImageMagick/Wand not available on server"}, status_code=500)

    t = target.lower().strip()
    if t == 'jpg':
        t = 'jpeg'

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"too many files (max {MAX_FILES})"}, status_code=400)

    # Read all file bytes first
    files_data = []
    for uf in files:
        raw = await uf.read()
        if raw:
            files_data.append((raw, uf.filename, t, artist))

    # Prepare ZIP
    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        if len(files_data) < 10:
            # Sequential
            for f in files_data:
                arcname, out_blob = convert_one(*f)
                if out_blob:
                    zf.writestr(arcname, out_blob)
        else:
            # Parallel
            with ProcessPoolExecutor(max_workers=multiprocessing.cpu_count()) as executor:
                for arcname, out_blob in executor.map(_convert_one_unpack, files_data):
                    if out_blob:
                        zf.writestr(arcname, out_blob)

    mem_zip.seek(0)
    headers = {
        "Content-Disposition": "attachment; filename=converted.zip",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }
    return StreamingResponse(mem_zip, media_type="application/zip", headers=headers)

