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
    """Apply a selected LUT to one or more images using ffmpeg's lut3d filter.
    Returns a ZIP of processed images.
    """
    import tempfile
    import subprocess

    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'convert'):
        # reuse same permission as convert
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    # Resolve and validate LUT path to prevent traversal
    base_dir = os.path.abspath(LUTS_DIR)
    lut_path = os.path.abspath(os.path.join(base_dir, lut_file))
    if not lut_path.startswith(base_dir + os.sep):
        return JSONResponse({"error": "Invalid LUT path"}, status_code=400)
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

    def jpeg_q_from_quality(q: float) -> int:
        # map [0..1] where 1 is best, to ffmpeg q:v [2..31] where 2 is best
        try:
            q = max(0.0, min(1.0, float(q)))
        except Exception:
            q = 0.92
        return max(2, min(31, int(round((1.0 - q) * 25)) + 2))

    mem_zip = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(mem_zip, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
            for uf in files:
                try:
                    raw = await uf.read()
                    if not raw:
                        continue

                    # Write input to a temp file
                    in_name = os.path.basename(uf.filename or 'image')
                    if not in_name:
                        in_name = 'image.png'
                    in_path = os.path.join(tmpdir, in_name)
                    with open(in_path, 'wb') as f:
                        f.write(raw)

                    base, _ = os.path.splitext(os.path.basename(in_name))
                    ext_for_archive = 'jpg' if t == 'jpeg' else t
                    out_name = f"{base}.{ext_for_archive}"
                    out_path = os.path.join(tmpdir, out_name)

                    vf_arg = f"lut3d={lut_path}"
                    cmd = [
                        'ffmpeg', '-y', '-loglevel', 'error',
                        '-i', in_path,
                        '-vf', vf_arg,
                    ]
                    if t == 'jpeg':
                        cmd += ['-q:v', str(jpeg_q_from_quality(quality))]
                    cmd += [out_path]

                    try:
                        subprocess.run(cmd, check=True)
                    except Exception as ex:
                        logger.warning("ffmpeg failed for %s: %s", in_name, ex)
                        continue

                    # Read output
                    try:
                        with open(out_path, 'rb') as f:
                            out_blob = f.read()
                    except Exception as ex:
                        logger.warning("output read failed for %s: %s", out_name, ex)
                        continue

                    zf.writestr(out_name, out_blob)
                except Exception as ex:
                    logger.warning("lut apply failed for %s: %s", getattr(uf, 'filename', 'image'), ex)
                    continue

    mem_zip.seek(0)
    headers = {
        "Content-Disposition": "attachment; filename=styled.zip",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }
    return StreamingResponse(mem_zip, media_type="application/zip", headers=headers)