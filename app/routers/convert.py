from typing import List, Optional, Literal
import os
import io
import zipfile
from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from app.core.config import MAX_FILES, logger
from app.core.auth import get_uid_from_request

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


@router.post("/convert/bulk")
async def convert_bulk(
    request: Request,
    files: List[UploadFile] = File(...),
    target: SupportedTarget = Form(...),
    artist: Optional[str] = Form(None),
):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not WAND_AVAILABLE:
        return JSONResponse({"error": "ImageMagick/Wand not available on server"}, status_code=500)

    t = target.lower().strip()
    if t == 'jpg':
        t = 'jpeg'

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"too many files (max {MAX_FILES})"}, status_code=400)

    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for uf in files:
            try:
                raw = await uf.read()
                if not raw:
                    continue

                with WandImage(blob=raw) as img:
                    if len(img.sequence) > 1:
                        with WandImage(image=img.sequence[0]) as first:
                            img = first.clone()

                    out_blob = None
                    with WandImage(image=img) as out:
                        out_ext = t
                        if t in ('svg', 'eps'):
                            try:
                                out.format = t
                                out_blob = out.make_blob()
                                out_ext = t
                            except Exception:
                                out.format = 'pdf'
                                out_blob = out.make_blob()
                                out_ext = 'pdf'
                        else:
                            out.format = t
                            out_blob = out.make_blob()
                            out_ext = t

                try:
                    if artist and out_ext in ("jpeg", "jpg"):
                        _im = Image.open(io.BytesIO(out_blob)).convert("RGB")
                        if PIEXIF_AVAILABLE:
                            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
                            exif_dict["0th"][piexif.ImageIFD.Artist] = artist
                            exif_bytes = piexif.dump(exif_dict)
                            buf = io.BytesIO()
                            _im.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True, exif=exif_bytes)
                            out_blob = buf.getvalue()
                        else:
                            buf = io.BytesIO()
                            _im.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True)
                            out_blob = buf.getvalue()
                    elif artist and out_ext == "png":
                        _im = Image.open(io.BytesIO(out_blob)).convert("RGBA")
                        pnginfo = PngInfo()
                        pnginfo.add_text("Artist", artist)
                        buf = io.BytesIO()
                        _im.save(buf, format="PNG", pnginfo=pnginfo, optimize=True)
                        out_blob = buf.getvalue()
                except Exception as __ex:
                    logger.warning("metadata embed failed for %s: %s", uf.filename, __ex)

                base = os.path.splitext(os.path.basename(uf.filename or 'image'))[0] or 'image'
                arcname = f"{base}.{out_ext}"
                zf.writestr(arcname, out_blob)
            except Exception as ex:
                logger.warning("convert failed for %s: %s", uf.filename, ex)
                continue

    mem_zip.seek(0)
    headers = {
        "Content-Disposition": "attachment; filename=converted.zip",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }
    return StreamingResponse(mem_zip, media_type="application/zip", headers=headers)