import os
import uuid
from typing import List

from fastapi import APIRouter, File, UploadFile, Request
from fastapi.responses import JSONResponse
from PIL import Image

from app.core.config import STATIC_DIR

router = APIRouter(prefix="/api", tags=["moodboard"])  # included by app.main


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

    # Save uploaded files
    file_paths: List[str] = []
    for file in files:
        filename = str(uuid.uuid4()) + os.path.splitext(file.filename or "")[1]
        file_path = os.path.join(uploads_dir, filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())
        file_paths.append(file_path)

    if not file_paths:
        return JSONResponse({"error": "no files"}, status_code=400)

    # Generate moodboard
    out_name = f"moodboard_{uuid.uuid4()}.jpg"
    output_file = os.path.join(outputs_dir, out_name)
    # Default to a 2x3 grid; increase rows/cols if more images
    rows, cols = 2, 3
    n = len(file_paths)
    if n <= 6:
        grid = (2, 3)
    elif n <= 8:
        grid = (2, 4)
    else:
        grid = (3, 4)
    create_moodboard(file_paths, output_file, grid_size=grid, padding=20, bg_color=(245, 245, 245))

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
    }

    return JSONResponse(content=result)