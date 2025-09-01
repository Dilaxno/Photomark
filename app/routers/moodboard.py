import os
import uuid
import math
from typing import List

from fastapi import APIRouter, File, UploadFile, Request, Body
from fastapi.responses import JSONResponse, FileResponse
from PIL import Image
import zipfile

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


def create_moodboard(image_paths: List[str], output_path: str, grid_size=(2, 3), padding=20, bg_color=(255, 192, 203)):
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

    # Define maximum output board size (in pixels). Adjust if you want larger collages.
    MAX_BOARD_W, MAX_BOARD_H = 6000, 6000

    # Compute base tile (cell) size so the whole board fits within MAX_BOARD_* including padding.
    cell_w = max(1, (MAX_BOARD_W - (cols + 1) * padding) // cols)
    cell_h = max(1, (MAX_BOARD_H - (rows + 1) * padding) // rows)

    # Create blank canvas
    board_w = cols * cell_w + (cols + 1) * padding
    board_h = rows * cell_h + (rows + 1) * padding
    moodboard = Image.new("RGB", (board_w, board_h), bg_color)

    # Paste images preserving aspect ratio; do not upscale beyond native size.
    index = 0
    for r in range(rows):
        for c in range(cols):
            if index >= len(images):
                break
            im = images[index]
            ow, oh = sizes[index]

            # Scale image to fit inside the cell while preserving AR, no upscaling beyond original.
            scale = min(cell_w / ow, cell_h / oh)
            scale = min(scale, 1.0)
            new_w = max(1, int(ow * scale))
            new_h = max(1, int(oh * scale))

            if (new_w, new_h) != (ow, oh):
                try:
                    # High-quality resampling for downscale
                    from PIL import Image as _PILImage
                    resized = im.resize((new_w, new_h), resample=_PILImage.Resampling.LANCZOS)
                except Exception:
                    resized = im.resize((new_w, new_h))
            else:
                resized = im

            # Compute top-left corner to center the image within its cell
            cell_x = c * cell_w + (c + 1) * padding
            cell_y = r * cell_h + (r + 1) * padding
            paste_x = cell_x + (cell_w - new_w) // 2
            paste_y = cell_y + (cell_h - new_h) // 2

            moodboard.paste(resized, (paste_x, paste_y))
            index += 1

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Save with high quality settings for JPEG to reduce compression artifacts
    ext = os.path.splitext(output_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        try:
            moodboard.save(output_path, format="JPEG", quality=95, optimize=True, subsampling=0)
        except Exception:
            moodboard.save(output_path, format="JPEG", quality=95)
    else:
        moodboard.save(output_path)
    return output_path


@router.post("/moodboard/generate")
async def generate_moodboard(request: Request, files: List[UploadFile] = File(...)):
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
            # Save the zip to disk first
            zip_name = f"{uuid.uuid4()}.zip"
            zip_path = os.path.join(uploads_dir, zip_name)
            with open(zip_path, "wb") as zf:
                zf.write(await uf.read())

            # Extract zip to a unique folder
            extract_dir = os.path.join(uploads_dir, f"unzipped_{uuid.uuid4()}")
            os.makedirs(extract_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(zip_path, 'r') as z:
                    # Extract only files; ignore directories
                    z.extractall(extract_dir)
                # Walk extracted dir and collect image files
                for root, _, fnames in os.walk(extract_dir):
                    for nm in fnames:
                        e = os.path.splitext(nm)[1].lower()
                        if e in image_exts:
                            saved_paths.append(os.path.join(root, nm))
                            if len(saved_paths) >= MAX_IMAGES:
                                break
                    if len(saved_paths) >= MAX_IMAGES:
                        break
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
    create_moodboard(file_paths, output_file, grid_size=(rows, cols), padding=12, bg_color=(245, 245, 245))

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
async def export_collage_pdf(filename: str):
    """Export an existing collage (stored as JPG) to a PDF and send it for download."""
    try:
        src_path = _safe_path_in_moodboards(filename)
        if not os.path.exists(src_path):
            return JSONResponse({"error": "file not found"}, status_code=404)
        image = Image.open(src_path).convert("RGB")
        pdf_name = os.path.splitext(filename)[0] + ".pdf"
        pdf_path = _safe_path_in_moodboards(pdf_name)
        image.save(pdf_path, "PDF", resolution=300.0)  # 300 DPI
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