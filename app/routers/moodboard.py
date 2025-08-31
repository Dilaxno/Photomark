import os
import uuid
import math
from typing import List

from fastapi import APIRouter, File, UploadFile, Request, Body
from fastapi.responses import JSONResponse, FileResponse
from PIL import Image

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
    rows, cols = grid_size
    images = [Image.open(img).convert("RGB") for img in image_paths]

    # Resize all images to same size
    img_width, img_height = 400, 400
    resized_images = [img.resize((img_width, img_height)) for img in images]

    # Create blank canvas
    board_width = cols * img_width + (cols + 1) * padding
    board_height = rows * img_height + (rows + 1) * padding
    moodboard = Image.new("RGB", (board_width, board_height), bg_color)

    # Paste images
    index = 0
    for r in range(rows):
        for c in range(cols):
            if index >= len(resized_images):
                break
            x = c * img_width + (c + 1) * padding
            y = r * img_height + (r + 1) * padding
            moodboard.paste(resized_images[index], (x, y))
            index += 1

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
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
        return JSONResponse({"error": "Please upload at least 1 image."}, status_code=400)
    if len(files) > MAX_IMAGES:
        return JSONResponse({"error": f"You selected {len(files)} images. The limit per collage is {MAX_IMAGES}."}, status_code=400)

    # Save uploaded files
    file_paths: List[str] = []
    for file in files:
        filename = str(uuid.uuid4()) + os.path.splitext(file.filename or "")[1]
        file_path = os.path.join(uploads_dir, filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())
        file_paths.append(file_path)

    # Generate moodboard
    out_name = f"moodboard_{uuid.uuid4()}.jpg"
    output_file = os.path.join(outputs_dir, out_name)

    # Dynamic grid for up to 50 images: pick near-square grid
    n = len(file_paths)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    # Bound extremes for aesthetics
    cols = max(3, min(cols, 10))
    rows = max(2, min(rows, 10))

    create_moodboard(file_paths, output_file, grid_size=(rows, cols), padding=20, bg_color=(245, 245, 245))

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