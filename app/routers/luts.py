from typing import List, Optional
import os
import io
import zipfile

from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse, Response

from app.core.config import MAX_FILES, logger, STATIC_DIR
from app.core.auth import resolve_workspace_uid, has_role_access
from app.utils.luts import list_luts, get_texture_2d, apply_lut

router = APIRouter(prefix="/api", tags=["luts"]) 


@router.get("/luts")
async def luts_list():
    return {"luts": list_luts()}


@router.get("/luts/{name}/texture")
async def luts_texture(name: str):
    try:
        tex = get_texture_2d(name)
        if not tex:
            return JSONResponse({"error": "not found"}, status_code=404)
        headers = {
            "Cache-Control": "public, max-age=31536000, immutable",
            "Access-Control-Expose-Headers": "Content-Disposition",
        }
        return Response(content=tex, media_type="image/png", headers=headers)
    except Exception as ex:
        logger.warning("luts_texture failed for %s: %s", name, ex)
        return JSONResponse({"error": "error"}, status_code=500)


@router.post("/luts/apply")
async def luts_apply(
    request: Request,
    file: UploadFile = File(...),
    lut: str = Form(...),
    engine: Optional[str] = Form("auto"),
):
    # Auth: use 'retouch' area for style application
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'retouch'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "no file"}, status_code=400)
    try:
        out = apply_lut(raw, lut, engine or 'auto')
        headers = {
            "Content-Disposition": f"attachment; filename=styled.jpg",
            "Access-Control-Expose-Headers": "Content-Disposition",
        }
        return Response(content=out, media_type="image/jpeg", headers=headers)
    except KeyError:
        return JSONResponse({"error": "lut not found"}, status_code=404)
    except Exception as ex:
        logger.error("luts_apply failed: %s", ex)
        return JSONResponse({"error": "error"}, status_code=500)


@router.post("/luts/apply/bulk")
async def luts_apply_bulk(
    request: Request,
    files: List[UploadFile] = File(...),
    lut: str = Form(...),
    engine: Optional[str] = Form("auto"),
):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'retouch'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not files:
        return JSONResponse({"error": "no files"}, status_code=400)
    if len(files) > MAX_FILES:
        return JSONResponse({"error": f"too many files (max {MAX_FILES})"}, status_code=400)

    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        idx = 0
        for uf in files:
            try:
                raw = await uf.read()
                if not raw:
                    continue
                out = apply_lut(raw, lut, engine or 'auto')
                base, _ = os.path.splitext(os.path.basename(uf.filename or f"image_{idx}"))
                arc = f"{base}.jpg"
                zf.writestr(arc, out)
            except Exception as ex:
                logger.warning("luts_apply_bulk: failed for %s: %s", uf.filename, ex)
            finally:
                idx += 1
    mem_zip.seek(0)
    headers = {
        "Content-Disposition": "attachment; filename=styled.zip",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }
    return StreamingResponse(mem_zip, media_type="application/zip", headers=headers)