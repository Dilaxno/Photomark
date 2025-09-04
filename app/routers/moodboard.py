import os
import uuid
import math
from typing import List, Optional

from fastapi import APIRouter, File, UploadFile, Request, Body, Form
from fastapi.responses import JSONResponse, FileResponse
from PIL import Image
import zipfile
from io import BytesIO

from app.core.config import STATIC_DIR

router = APIRouter(prefix="/api", tags=["moodboard"])  # included by app.main


def _safe_path_in_moodboards(filename: str) -> str:
    # prevent path traversal and restrict to moodboards dir
    base_dir = os.path.abspath(os.path.join(STATIC_DIR, "moodboards"))
    name = os.path.basename(filename or "").strip()
    if not name:
        raise ValueError("invalid filename")
    path = os.path.abspath(os.path.join(base_dir, name))
    if not path.startswith(base_dir):
        raise ValueError("invalid path")
    return path


def create_moodboard(image_paths: List[str], output_path: str, grid_size=(2, 3), padding=20, bg_color=(255, 192, 203), fast: bool = True, board_max: Optional[int] = None, quality: int = 88):
    """
    Create a collage while preserving as much image resolution as possible.
    - Uses high-quality downscaling (LANCZOS)
    - Avoids upscaling images beyond their native resolution
    - Chooses tile size based on a max board size constraint
    """
    rows, cols = grid_size

    # Load images in RGB
    images = [Image.open(img).convert("RGB") for img in image_paths]
    sizes = [im.size for im in images]  # (w, h)

    # Define maximum output board size (in pixels).
    # Fast mode defaults to a smaller canvas for speed; can be overridden with board_max.
    MAX_BOARD_W = MAX_BOARD_H = int(board_max) if board_max else (2400 if fast else 6000)

    # Compute base tile (cell) size so the whole board fits within MAX_BOARD_* including padding.
    cell_w = max(1, (MAX_BOARD_W - (cols + 1) * padding) // cols)
    cell_h = max(1, (MAX_BOARD_H - (rows + 1) * padding) // rows)

    # Create blank canvas
    board_w = cols * cell_w + (cols + 1) * padding
    board_h = rows * cell_h + (rows + 1) * padding
    moodboard = Image.new("RGB", (board_w, board_h), bg_color)

    # Paste images preserving aspect ratio; fill each cell (cover), center-cropped to equal size.
    index = 0
    for r in range(rows):
        for c in range(cols):
            if index >= len(images):
                break
            im = images[index]
            ow, oh = sizes[index]

            # Scale image to cover the cell; allows upscaling so all tiles are equal size
            scale = max(cell_w / ow, cell_h / oh)
            new_w = max(1, int(math.ceil(ow * scale)))
            new_h = max(1, int(math.ceil(oh * scale)))

            try:
                from PIL import Image as _PILImage
                resample_method = getattr(_PILImage.Resampling, "BILINEAR", 2) if fast else getattr(_PILImage.Resampling, "LANCZOS", 1)
                resized = im.resize((new_w, new_h), resample=resample_method)
            except Exception:
                resized = im.resize((new_w, new_h))

            # Center-crop to exactly cell size
            left = max(0, (new_w - cell_w) // 2)
            top = max(0, (new_h - cell_h) // 2)
            right = left + cell_w
            bottom = top + cell_h
            tile = resized.crop((left, top, right, bottom))

            # Top-left corner of the cell
            cell_x = c * cell_w + (c + 1) * padding
            cell_y = r * cell_h + (r + 1) * padding

            moodboard.paste(tile, (cell_x, cell_y))
            index += 1

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Save with high quality settings for JPEG to reduce compression artifacts
    ext = os.path.splitext(output_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        try:
            moodboard.save(output_path, format="JPEG", quality=quality, optimize=True, subsampling=0)
        except Exception:
            moodboard.save(output_path, format="JPEG", quality=quality)
    else:
        moodboard.save(output_path)
    return output_path


@router.post("/moodboard/generate")
async def generate_moodboard(
    request: Request,
    files: List[UploadFile] = File(...),
    fast: bool = Form(True),
    pages: bool = Form(False),
    board_max: Optional[int] = Form(None),
):
    # Save uploaded files to a temp area inside static so the final URL works with main static mount
    uploads_dir = os.path.join(STATIC_DIR, "tmp", "uploads")
    outputs_dir = os.path.join(STATIC_DIR, "moodboards")
    os.makedirs(uploads_dir, exist_ok=True)
    os.makedirs(outputs_dir, exist_ok=True)

    # Enforce max 20
    MAX_IMAGES = 20
    if not files or len(files) == 0:
        return JSONResponse({"error": "Please upload at least 1 image or a ZIP."}, status_code=400)

    # Collect images from direct uploads and ZIP(s)
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
    saved_paths: List[str] = []

    for uf in files:
        original_name = (uf.filename or "").lower()
        ext = os.path.splitext(original_name)[1]

        # If it's a ZIP, extract images
        if ext == ".zip":
            try:
                data = await uf.read()
                with zipfile.ZipFile(BytesIO(data), 'r') as z:
                    for info in z.infolist():
                        if info.is_dir():
                            continue
                        e = os.path.splitext(info.filename)[1].lower()
                        if e not in image_exts:
                            continue
                        try:
                            with z.open(info) as srcf:
                                content = srcf.read()
                            fname = f"{uuid.uuid4()}{e or '.jpg'}"
                            fpath = os.path.join(uploads_dir, fname)
                            with open(fpath, 'wb') as out:
                                out.write(content)
                            saved_paths.append(fpath)
                            if len(saved_paths) >= MAX_IMAGES:
                                break
                        except Exception:
                            continue
            except zipfile.BadZipFile:
                return JSONResponse({"error": "Invalid ZIP file."}, status_code=400)
        else:
            # Regular file upload: only accept images
            if ext.lower() not in image_exts:
                # ignore non-image non-zip files
                continue
            filename = str(uuid.uuid4()) + ext
            file_path = os.path.join(uploads_dir, filename)
            with open(file_path, "wb") as f:
                f.write(await uf.read())
            saved_paths.append(file_path)
            if len(saved_paths) >= MAX_IMAGES:
                break

    if len(saved_paths) == 0:
        return JSONResponse({"error": "No valid images found (accepted: JPG, PNG, WEBP, BMP, TIFF)."}, status_code=400)

    # Limit to MAX_IMAGES
    file_paths = saved_paths[:MAX_IMAGES]

    # Generate moodboard
    out_name = f"moodboard_{uuid.uuid4()}.jpg"
    output_file = os.path.join(outputs_dir, out_name)

    # Dynamic grid: pick near-square grid
    n = len(file_paths)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    # Bound extremes for aesthetics
    cols = max(3, min(cols, 10))
    rows = max(2, min(rows, 10))

    # Slightly reduce padding so tiles appear larger without increasing board size
    create_moodboard(file_paths, output_file, grid_size=(rows, cols), padding=12, bg_color=(245, 245, 245), fast=fast, board_max=board_max, quality=88)

    # Optionally save per-image pages for PDF export (one image per PDF page)
    if pages:
        stem = os.path.splitext(out_name)[0]
        pages_dir = os.path.join(outputs_dir, f"{stem}_pages")
        os.makedirs(pages_dir, exist_ok=True)
        for idx, src in enumerate(file_paths, start=1):
            try:
                im = Image.open(src).convert("RGB")
                MAX_W, MAX_H = (3000 if fast else 5000), (3000 if fast else 5000)
                w, h = im.size
                scale = min(MAX_W / w, MAX_H / h, 1.0)
                if scale < 1.0:
                    try:
                        from PIL import Image as _PILImage
                        resample_method = getattr(_PILImage.Resampling, "BILINEAR", 2) if fast else getattr(_PILImage.Resampling, "LANCZOS", 1)
                        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), resample=resample_method)
                    except Exception:
                        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
                im.save(os.path.join(pages_dir, f"{idx:03d}.jpg"), format="JPEG", quality=88, subsampling=0, optimize=True)
            except Exception:
                pass

    # Build response: include absolute URL to avoid origin path issues
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    base = f"{scheme}://{host}" if host else ""

    result = {
        "ok": True,
        "palette_overall": [],
        "palette_per_image": [],
        "textures": {"top": [], "hist": {}},
        "typography_suggestions": [],
        "moodboard_image_url": f"{base}/static/moodboards/{out_name}",
        "moodboard_filename": out_name,
    }

    return JSONResponse(content=result)


@router.get("/moodboard/export/pdf")
async def export_collage_pdf(
    filename: str,
    page: str = "letter",            # "letter" or "a4"
    orientation: str = "portrait",    # "portrait" or "landscape"
    margin_mm: int = 15,               # uniform margins in millimeters
    dpi: int = 300                     # target DPI for page sizing
):
    """Export a PDF with standard page size, margins, and auto-fit images.

    - If a "<stem>_pages" folder exists (generated during collage), create a multi-page PDF
      with one image per page, auto-fitted inside margins on a white background.
    - Otherwise, export the collage centered on a single page.

    Optional query parameters: page (letter|a4), orientation (portrait|landscape), margin_mm, dpi.
    Defaults: letter, portrait, 15mm, 300dpi.
    """
    try:
        src_path = _safe_path_in_moodboards(filename)
        if not os.path.exists(src_path):
            return JSONResponse({"error": "file not found"}, status_code=404)

        # Resolve page size in inches
        page_key = (page or "letter").lower()
        orientation_key = (orientation or "portrait").lower()
        if page_key not in ("letter", "a4"):
            page_key = "letter"
        if orientation_key not in ("portrait", "landscape"):
            orientation_key = "portrait"

        if page_key == "a4":
            # A4: 8.27 x 11.69 inches
            pw_in, ph_in = 8.27, 11.69
        else:
            # Letter: 8.5 x 11 inches
            pw_in, ph_in = 8.5, 11.0
        if orientation_key == "landscape":
            pw_in, ph_in = ph_in, pw_in

        # Convert to pixels
        dpi = max(72, min(int(dpi), 600))  # clamp DPI to reasonable range
        page_w = int(round(pw_in * dpi))
        page_h = int(round(ph_in * dpi))

        # Margins
        try:
            margin_mm_val = max(0, int(margin_mm))
        except Exception:
            margin_mm_val = 15
        margin_in = margin_mm_val / 25.4
        margin_px = int(round(margin_in * dpi))
        # Ensure content area is positive
        content_w = max(1, page_w - 2 * margin_px)
        content_h = max(1, page_h - 2 * margin_px)

        def build_page(img: Image.Image) -> Image.Image:
            """Create a white page and paste the image auto-fitted within margins."""
            iw, ih = img.size
            # Scale to fit inside content area, preserve AR
            scale = min(content_w / iw, content_h / ih)
            new_w = max(1, int(math.floor(iw * scale)))
            new_h = max(1, int(math.floor(ih * scale)))
            try:
                from PIL import Image as _PILImage
                fitted = img.resize((new_w, new_h), resample=_PILImage.Resampling.LANCZOS)
            except Exception:
                fitted = img.resize((new_w, new_h))
            page_img = Image.new("RGB", (page_w, page_h), (255, 255, 255))
            off_x = margin_px + (content_w - new_w) // 2
            off_y = margin_px + (content_h - new_h) // 2
            page_img.paste(fitted, (off_x, off_y))
            return page_img

        stem = os.path.splitext(filename)[0]
        base_dir = os.path.abspath(os.path.join(STATIC_DIR, "moodboards"))
        pages_dir = os.path.join(base_dir, f"{stem}_pages")

        pdf_name = f"{stem}.pdf"
        pdf_path = _safe_path_in_moodboards(pdf_name)

        pages: List[Image.Image] = []
        if os.path.isdir(pages_dir):
            # Collect image pages sorted
            page_files = [os.path.join(pages_dir, fn) for fn in sorted(os.listdir(pages_dir)) if fn.lower().endswith((".jpg", ".jpeg", ".png"))]
            for p in page_files:
                try:
                    im = Image.open(p).convert("RGB")
                    pages.append(build_page(im))
                except Exception:
                    continue

        if not pages:
            # Fallback: single-page PDF of the collage, centered on the page
            image = Image.open(src_path).convert("RGB")
            pages = [build_page(image)]

        # Save PDF
        first, rest = pages[0], pages[1:]
        first.save(pdf_path, save_all=True, append_images=rest, format="PDF", resolution=float(dpi))
        return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_name)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.get("/moodboard/export/jpg")
async def export_collage_jpg(filename: str):
    """Return the JPG collage for download with a Content-Disposition header."""
    try:
        src_path = _safe_path_in_moodboards(filename)
        if not os.path.exists(src_path):
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(src_path, media_type="image/jpeg", filename=filename)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)