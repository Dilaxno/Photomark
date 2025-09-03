import io
import os
import uuid
import zipfile
from typing import List, Optional

from fastapi import APIRouter, UploadFile, File, Form, Request, Body
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image, ImageOps
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.pdfgen import canvas

from app.core.auth import get_uid_from_request
from app.core.config import s3, R2_BUCKET, R2_PUBLIC_BASE_URL, STATIC_DIR, logger

router = APIRouter(prefix="/api/photobook", tags=["photobook"]) 


def _public_url_for(key: str) -> Optional[str]:
    if s3 and R2_BUCKET and R2_PUBLIC_BASE_URL:
        return f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"
    return None


def _put_object(key: str, data: bytes, content_type: str) -> str:
    """Store bytes to R2 if configured, otherwise write under STATIC_DIR. Return a URL or path.
    """
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        bucket.put_object(Key=key, Body=data, ContentType=content_type, ACL='public-read')
        return _public_url_for(key) or key
    # local
    path = os.path.join(STATIC_DIR, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return f"/static/{key}"


def _list_images(prefix: str) -> List[str]:
    keys: List[str] = []
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        for obj in bucket.objects.filter(Prefix=prefix):
            k = obj.key
            if k.endswith('/'):
                continue
            if os.path.splitext(k)[1].lower() in ('.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp', '.gif'):
                keys.append(k)
    else:
        dir_path = os.path.join(STATIC_DIR, prefix)
        if os.path.isdir(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    if os.path.splitext(f)[1].lower() in ('.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp', '.gif'):
                        rel = os.path.relpath(os.path.join(root, f), STATIC_DIR).replace('\\', '/')
                        keys.append(rel)
    keys.sort()
    return keys


def _read_bytes(key: str) -> Optional[bytes]:
    if s3 and R2_BUCKET:
        try:
            obj = s3.Object(R2_BUCKET, key)
            res = obj.get()
            return res.get('Body').read()
        except Exception as ex:
            logger.warning(f"read_bytes failed for {key}: {ex}")
            return None
    path = os.path.join(STATIC_DIR, key)
    try:
        with open(path, 'rb') as f:
            return f.read()
    except Exception:
        return None


@router.post('/upload')
async def photobook_upload(request: Request, images: List[UploadFile] = File(default=[]), zipfile_in: UploadFile = File(default=None)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    pb_id = str(uuid.uuid4())[:8]
    base_prefix = f"users/{uid}/photobooks/{pb_id}/images/"
    saved_urls: List[str] = []

    # handle images list
    for uf in images or []:
        try:
            ext = os.path.splitext(uf.filename or '')[1].lower() or '.jpg'
            if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp', '.gif'):
                continue
            data = await uf.read()
            key = base_prefix + str(uuid.uuid4())[:8] + ext
            url = _put_object(key, data, f"image/{ext.lstrip('.')}")
            saved_urls.append(url)
        except Exception as ex:
            logger.warning(f"photobook upload image failed: {ex}")

    # handle zip
    if zipfile_in is not None:
        try:
            blob = await zipfile_in.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for name in zf.namelist():
                    if name.endswith('/'):
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in ('.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp', '.gif'):
                        continue
                    try:
                        data = zf.read(name)
                        key = base_prefix + str(uuid.uuid4())[:8] + ext
                        url = _put_object(key, data, f"image/{ext.lstrip('.')}")
                        saved_urls.append(url)
                    except Exception:
                        continue
        except Exception as ex:
            logger.warning(f"photobook upload zip failed: {ex}")

    # Build URLs by listing to ensure consistent results
    keys = _list_images(base_prefix)
    urls: List[str] = []
    for k in keys:
        if s3 and R2_BUCKET:
            urls.append(_public_url_for(k) or k)
        else:
            urls.append(f"/static/{k}")

    return {"id": pb_id, "images": urls}


@router.post('/build')
async def photobook_build(request: Request, payload: dict = Body(...)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    pb_id = str(payload.get('id') or '').strip()
    if not pb_id:
        return JSONResponse({"error": "id required"}, status_code=400)

    page_size_name = str(payload.get('page_size') or 'A4').upper()
    orientation = str(payload.get('orientation') or 'landscape').lower()
    layout = str(payload.get('layout') or 'single').lower()  # 'single' or 'double'
    margin = int(payload.get('margin') or 24)
    border = int(payload.get('border') or 8)

    if page_size_name == 'LETTER':
        size = LETTER
    else:
        size = A4
    if orientation == 'landscape':
        size = landscape(size)

    images_prefix = f"users/{uid}/photobooks/{pb_id}/images/"
    keys = _list_images(images_prefix)
    if not keys:
        return JSONResponse({"error": "no images uploaded"}, status_code=400)

    pdf_key = f"users/{uid}/photobooks/{pb_id}/photobook.pdf"

    # Build PDF in memory
    buff = io.BytesIO()
    c = canvas.Canvas(buff, pagesize=size)
    pw, ph = size

    def draw_image_centered(img: Image.Image, x: float, y: float, w: float, h: float):
        # Letterbox fit with border
        img = ImageOps.exif_transpose(img)
        img_ratio = img.width / img.height
        box_ratio = w / h
        if img_ratio > box_ratio:
            new_w = w - 2 * border
            new_h = new_w / img_ratio
        else:
            new_h = h - 2 * border
            new_w = new_h * img_ratio
        ix = x + (w - new_w) / 2
        iy = y + (h - new_h) / 2
        # Convert to RGB JPEG in memory for reportlab
        rgb = img.convert('RGB')
        tmp = io.BytesIO()
        rgb.save(tmp, format='JPEG', quality=90)
        tmp.seek(0)
        c.drawImage(ImageReader(tmp), ix, iy, width=new_w, height=new_h)

    # ReportLab ImageReader helper
    from reportlab.lib.utils import ImageReader

    idx = 0
    if layout == 'double':
        # place two per page
        cell_w = (pw - 3 * margin) / 2
        cell_h = ph - 2 * margin
        while idx < len(keys):
            c.setFillColorRGB(1, 1, 1)
            c.rect(0, 0, pw, ph, stroke=0, fill=1)
            for col in range(2):
                if idx >= len(keys):
                    break
                data = _read_bytes(keys[idx])
                idx += 1
                if not data:
                    continue
                try:
                    img = Image.open(io.BytesIO(data))
                except Exception:
                    continue
                x = margin + col * (cell_w + margin)
                y = margin
                draw_image_centered(img, x, y, cell_w, cell_h)
            c.showPage()
    else:
        # single per page
        cell_w = pw - 2 * margin
        cell_h = ph - 2 * margin
        for k in keys:
            data = _read_bytes(k)
            if not data:
                continue
            try:
                img = Image.open(io.BytesIO(data))
            except Exception:
                continue
            c.setFillColorRGB(1, 1, 1)
            c.rect(0, 0, pw, ph, stroke=0, fill=1)
            draw_image_centered(img, margin, margin, cell_w, cell_h)
            c.showPage()

    c.save()
    buff.seek(0)

    url = _put_object(pdf_key, buff.read(), 'application/pdf')
    return {"ok": True, "pdf": url, "id": pb_id}


@router.get('/download/{pb_id}')
async def photobook_download(request: Request, pb_id: str):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    pdf_key = f"users/{uid}/photobooks/{pb_id}/photobook.pdf"

    if s3 and R2_BUCKET:
        try:
            obj = s3.Object(R2_BUCKET, pdf_key)
            res = obj.get()
            body = res.get('Body')
            headers = {"Content-Disposition": f'attachment; filename="photobook-{pb_id}.pdf"'}
            def _iter():
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            return StreamingResponse(_iter(), media_type='application/pdf', headers=headers)
        except Exception as ex:
            return JSONResponse({"error": "Not found"}, status_code=404)
    else:
        path = os.path.join(STATIC_DIR, pdf_key)
        if not os.path.isfile(path):
            return JSONResponse({"error": "Not found"}, status_code=404)
        with open(path, 'rb') as f:
            data = f.read()
        headers = {"Content-Disposition": f'attachment; filename="photobook-{pb_id}.pdf"'}
        return StreamingResponse(io.BytesIO(data), media_type='application/pdf', headers=headers)
