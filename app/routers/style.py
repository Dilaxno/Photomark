from typing import List
from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
import os
import io
import zipfile

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.config import MAX_FILES, logger

try:
    from wand.image import Image as WandImage
    WAND_AVAILABLE = True
except Exception:
    WAND_AVAILABLE = False

router = APIRouter(prefix="/api", tags=["style"])

# Absolute path on the server where LUTs are stored (Gunicorn host)
LUTS_DIR = os.environ.get("LUTS_DIR", "/home/shadeform/luts/")


def _list_luts():
    try:
        files: List[str] = []
        if os.path.isdir(LUTS_DIR):
            for name in os.listdir(LUTS_DIR):
                if name.lower().endswith((".cube", ".3dl", ".lut", ".txt")):
                    files.append(name)
        files.sort()
        return files
    except Exception:
        return []


@router.get("/luts")
async def list_luts():
    """Return available LUT files from server directory.
    Response: { luts: [{ key, label, desc, file }...] }
    """
    files = _list_luts()
    items = []
    for f in files:
        base = os.path.splitext(os.path.basename(f))[0]
        label = base.replace("_", " ").replace("-", " ").strip().title() or base
        items.append({
            "key": f"lut:{f}",
            "label": label,
            "desc": f"Server LUT ({f})",
            "file": f,
        })
    return JSONResponse({"luts": items})


@router.post("/luts/apply")
async def apply_lut(
    request: Request,
    files: List[UploadFile] = File(...),
    lut_file: str = Form(...),  # filename inside LUTS_DIR
    fmt: str = Form("png"),
    quality: float = Form(0.92),
    artist: str | None = Form(None),
):
    """Apply a selected LUT to one or more images using ImageMagick (via Wand).
    Returns a ZIP of processed images.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'convert'):
        # reuse same permission as convert
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not WAND_AVAILABLE:
        return JSONResponse({"error": "ImageMagick/Wand not available on server"}, status_code=500)

    lut_path = os.path.join(LUTS_DIR, lut_file)
    if not os.path.isfile(lut_path):
        return JSONResponse({"error": "LUT not found"}, status_code=404)

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"too many files (max {MAX_FILES})"}, status_code=400)

    # Normalize fmt
    t = (fmt or 'png').lower().strip()
    if t == 'jpg':
        t = 'jpeg'

    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for uf in files:
            try:
                raw = await uf.read()
                if not raw:
                    continue
                # Use Wand/ImageMagick to apply LUT via -clut
                # Strategy: convert source to RGB, apply clut with second image (lut file)
                with WandImage(blob=raw) as img:
                    if len(img.sequence) > 1:
                        with WandImage(image=img.sequence[0]) as first:
                            img = first.clone()
                    with WandImage(filename=lut_path) as lut:
                        # Ensure both are in same colorspace
                        img.colorspace = 'rgb'
                        lut.colorspace = 'rgb'
                        img.clut(lut)  # applies LUT
                        img.format = t
                        out_blob = img.make_blob()

                base, _ = os.path.splitext(os.path.basename(uf.filename or 'image'))
                arcname = f"{base}.{t}"
                zf.writestr(arcname, out_blob)
            except Exception as ex:
                logger.warning("lut apply failed for %s: %s", uf.filename, ex)
                continue

    mem_zip.seek(0)
    headers = {
        "Content-Disposition": "attachment; filename=styled.zip",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }
    return StreamingResponse(mem_zip, media_type="application/zip", headers=headers)