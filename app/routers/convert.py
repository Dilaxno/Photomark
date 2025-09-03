from typing import List, Optional, Literal
import os
import io
import zipfile
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from fastapi import APIRouter, Request, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from app.core.config import MAX_FILES, logger
from app.core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access, get_user_email_from_uid

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

# Optional: pyvips for high-performance letterboxing (requested)
try:
    import pyvips  # type: ignore
    PYVIPS_AVAILABLE = True
except Exception:
    pyvips = None  # type: ignore
    PYVIPS_AVAILABLE = False

router = APIRouter(prefix="/api", tags=["convert"])

SupportedTarget = Literal['psd', 'tiff', 'png', 'jpeg', 'jpg', 'gif', 'svg', 'eps', 'pdf']

# =============================
# Aspect ratio (letterbox) util
# =============================

def parse_ratio(r: str) -> Optional[float]:
    r = (r or '').strip().lower()
    if not r:
        return None
    if r == 'a4':
        # A4 portrait 210x297 mm
        return 210.0 / 297.0
    if ':' in r:
        parts = r.split(':', 1)
        try:
            a = float(parts[0].strip())
            b = float(parts[1].strip())
            if a > 0 and b > 0:
                return a / b
        except Exception:
            return None
    try:
        v = float(r)
        return v if v > 0 else None
    except Exception:
        return None


def letterbox_embed_raw(raw: bytes, ratio_str: str, bg_hex: str = '#000000') -> Optional[bytes]:
    if not PYVIPS_AVAILABLE:
        raise RuntimeError('pyvips is not available on server')
    ar = parse_ratio(ratio_str)
    if not ar:
        raise ValueError(f'invalid ratio: {ratio_str}')

    img = pyvips.Image.new_from_buffer(raw, '')  # auto-detect
    w = int(img.width)
    h = int(img.height)
    if w <= 0 or h <= 0:
        raise ValueError('invalid image size')

    cur_ar = w / h
    # Compute target canvas size preserving original dimensions and adding padding only
    if abs(cur_ar - ar) < 1e-6:
        target_w, target_h = w, h
        left, top = 0, 0
    elif cur_ar > ar:
        # too wide -> increase height
        target_w = w
        target_h = int(round(w / ar))
        left = 0
        top = max(0, (target_h - h) // 2)
    else:
        # too tall -> increase width
        target_h = h
        target_w = int(round(h * ar))
        top = 0
        left = max(0, (target_w - w) // 2)

    # Background color
    def hex_to_rgb(hexs: str):
        s = hexs.lstrip('#')
        if len(s) == 3:
            s = ''.join([c*2 for c in s])
        try:
            r = int(s[0:2], 16)
            g = int(s[2:4], 16)
            b = int(s[4:6], 16)
            return [r, g, b]
        except Exception:
            return [0, 0, 0]
    bg = hex_to_rgb(bg_hex or '#000000')

    # Ensure 3-channel RGB for safe embed; handle alpha by flatten onto bg
    base = img
    if 'alpha' in img.get_fields():
        base = img.flatten(background=bg)
    elif img.bands == 1:
        base = img.colourspace('b-w').colourspace('srgb')
    elif img.bands >= 3:
        base = img.colourspace('srgb')

    # Embed onto background canvas
    out = base.embed(left, top, target_w, target_h, extend='background', background=bg)
    # Encode as PNG to preserve lossless and background
    buf = out.write_to_buffer('.png')
    return buf

# For executor mapping
def _letterbox_unpack(args):
    return letterbox_embed_raw(*args)

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
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    target: SupportedTarget = Form(...),
    artist: Optional[str] = Form(None),
    email_result: Optional[bool] = Form(False),
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

    # Prepare ZIP builder
    def build_zip_bytes() -> bytes:
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
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
        mem.seek(0)
        return mem.read()

    # If email requested or large payload, upload to storage and email link
    want_email = bool(email_result)
    large_batch = len(files_data) >= 100  # heuristic threshold for long-running jobs

    if want_email or large_batch:
        try:
            from datetime import datetime
            from app.utils.storage import upload_bytes
            from app.utils.emailing import render_email, send_email_smtp

            # Run the heavy work after returning response
            def do_upload_and_email():
                try:
                    zip_bytes = build_zip_bytes()
                    ts = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
                    key = f"users/{eff_uid}/convert/converted-{ts}.zip"
                    url = upload_bytes(key, zip_bytes, content_type="application/zip")
                    to_email = get_user_email_from_uid(req_uid) or ""
                    if to_email:
                        html = render_email(
                            "email_basic.html",
                            title="Your converted images are ready",
                            intro=f"Your bulk conversion has completed. You can download your ZIP here: <a href=\"{url}\">Download ZIP</a>.",
                            button_url=url,
                            button_label="Download ZIP",
                            footer_note="This link may expire after a period of time."
                        )
                        send_email_smtp(to_email, "Converted images ready", html)
                except Exception as ex2:
                    logger.warning(f"convert background task failed: {ex2}")

            background_tasks.add_task(do_upload_and_email)
            return JSONResponse({"status": "queued"}, status_code=202)
        except Exception as ex:
            logger.warning(f"email delivery fallback failed: {ex}")
            # If email flow fails, fall back to streaming

    # Normal flow: stream zip
    mem_zip = io.BytesIO(build_zip_bytes())
    headers = {
        "Content-Disposition": "attachment; filename=converted.zip",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }
    return StreamingResponse(mem_zip, media_type="application/zip", headers=headers)


@router.post('/convert/aspect-batch')
async def aspect_batch(
    request: Request,
    files: List[UploadFile] = File(...),
    ratios: List[str] = Form(...),
    bg_color: Optional[str] = Form('#000000'),
):
    # Auth check (same as convert)
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'convert'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not PYVIPS_AVAILABLE:
        return JSONResponse({"error": "pyvips not available on server"}, status_code=500)

    # sanitize ratios
    ratios_clean = []
    for r in ratios:
        try:
            r = (r or '').strip().lower()
            if not r:
                continue
            if r == 'letter':
                r = '8.5:11'
            if r == 'a4':
                ratios_clean.append('a4')
            else:
                _ = parse_ratio(r)
                if _:
                    ratios_clean.append(r)
        except Exception:
            continue
    ratios_clean = list(dict.fromkeys(ratios_clean))  # dedupe
    if not ratios_clean:
        return JSONResponse({"error": "No valid ratios provided"}, status_code=400)

    # Read files
    tasks = []
    for uf in files:
        raw = await uf.read()
        if not raw:
            continue
        base = os.path.splitext(os.path.basename(uf.filename or 'image'))[0]
        for r in ratios_clean:
            tasks.append((raw, r, bg_color or '#000000', base, r))

    if not tasks:
        return JSONResponse({"error": "No files provided"}, status_code=400)

    # Build zip
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        # Parallelize by CPU
        args = [(raw, r, bg, ) for (raw, r, bg, _, __) in tasks]
        with ProcessPoolExecutor(max_workers=max(1, multiprocessing.cpu_count())) as ex:
            for (raw, r, bg, base, rstr), out_buf in zip(tasks, ex.map(_letterbox_unpack, args)):
                if not out_buf:
                    continue
                safe_r = rstr.replace(':', 'x').replace('.', '_')
                arc = f"{base}_ar-{safe_r}.png"
                zf.writestr(arc, out_buf)
    mem.seek(0)
    headers = {
        "Content-Disposition": "attachment; filename=aspect_letterbox.zip",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }
    return StreamingResponse(mem, media_type='application/zip', headers=headers)

