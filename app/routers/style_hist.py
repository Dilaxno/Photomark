from fastapi import APIRouter, UploadFile, File, Form, Request
from starlette.responses import StreamingResponse, JSONResponse
from typing import List, Optional
import io
import zipfile
import os

import numpy as np
from PIL import Image

from skimage.exposure import match_histograms

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.config import logger

router = APIRouter(prefix="/api/style", tags=["style"])  # matches existing /api/style namespace


def _pil_to_np_rgb(img: Image.Image) -> np.ndarray:
    if img.mode != 'RGB':
        img = img.convert('RGB')
    arr = np.asarray(img).astype(np.uint8)
    return arr


def _np_to_pil(arr: np.ndarray) -> Image.Image:
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, mode='RGB')


@router.post('/hist-match')
async def hist_match(
    request: Request,
    reference: UploadFile = File(..., description='Reference image to copy style from'),
    files: List[UploadFile] = File(..., description='Target images to apply reference style to'),
    fmt: Optional[str] = Form('jpg'),
    quality: Optional[float] = Form(0.92),
):
    """
    Copy the color style (exposure/color distribution) from a reference image to a batch of images
    via histogram matching using skimage.exposure.match_histograms. Returns a single image if one
    target is provided, or a ZIP archive if multiple targets are processed.
    """
    # Auth (mirror convert/style_lut behavior)
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'convert'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    try:
        # Load reference
        ref_bytes = await reference.read()
        if not ref_bytes:
            return JSONResponse({"error": "empty reference"}, status_code=400)
        ref_img = Image.open(io.BytesIO(ref_bytes))
        ref_np = _pil_to_np_rgb(ref_img)

        processed_blobs: List[tuple[str, bytes]] = []

        for f in files:
            data = await f.read()
            if not data:
                continue
            try:
                img = Image.open(io.BytesIO(data))
                src_np = _pil_to_np_rgb(img)
                # Histogram match RGB channels jointly
                out_np = match_histograms(src_np, ref_np, channel_axis=-1)
                out_img = _np_to_pil(out_np)

                # Encode to requested format
                buf = io.BytesIO()
                out_fmt = (fmt or 'jpg').lower()
                if out_fmt in ('jpg', 'jpeg'):
                    q = int(max(1, min(100, round((quality or 0.92) * 100))))
                    out_img.save(buf, format='JPEG', quality=q, subsampling=0, progressive=True, optimize=True)
                    ext = 'jpg'
                else:
                    out_img.save(buf, format='PNG')
                    ext = 'png'
                buf.seek(0)
                # Decide output filename
                base = os.path.splitext(f.filename or 'image')[0]
                name = f"{base}_styled.{ext}"
                processed_blobs.append((name, buf.getvalue()))
            except Exception as ex:
                logger.exception(f"Failed to process {f.filename}: {ex}")
                # Skip this file and continue others
                continue

        if not processed_blobs:
            return JSONResponse({"error": "No images processed"}, status_code=400)

        # Single image response
        if len(processed_blobs) == 1:
            name, data = processed_blobs[0]
            media = 'image/jpeg' if name.lower().endswith('.jpg') or name.lower().endswith('.jpeg') else 'image/png'
            headers = {
                "Content-Disposition": f"attachment; filename={name}",
                "Access-Control-Expose-Headers": "Content-Disposition",
            }
            return StreamingResponse(io.BytesIO(data), media_type=media, headers=headers)

        # Multiple -> ZIP
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in processed_blobs:
                zf.writestr(name, data)
        zip_buf.seek(0)
        headers = {
            "Content-Disposition": "attachment; filename=styled_batch.zip",
            "Access-Control-Expose-Headers": "Content-Disposition",
        }
        return StreamingResponse(zip_buf, media_type='application/zip', headers=headers)

    except Exception as ex:
        logger.exception(f"Histogram matching failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
