import os
import uuid
from typing import List
from fastapi import FastAPI, File, UploadFile, Depends
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from colorthief import ColorThief

app = FastAPI()

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Serve static files (for accessing moodboard image URLs)
app.mount("/static", StaticFiles(directory=OUTPUT_FOLDER), name="static")


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

    moodboard.save(output_path)
    return output_path


def extract_palette(image_path: str, n_colors=5):
    try:
        ct = ColorThief(image_path)
        palette = ct.get_palette(color_count=n_colors)
        return ["#%02x%02x%02x" % rgb for rgb in palette]
    except Exception:
        return []


@app.post("/api/moodboard/generate")
async def generate_moodboard(files: List[UploadFile] = File(...)):
    # Save uploaded files
    file_paths = []
    for file in files:
        filename = str(uuid.uuid4()) + os.path.splitext(file.filename)[-1]
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())
        file_paths.append(file_path)

    # Generate moodboard
    output_file = os.path.join(OUTPUT_FOLDER, f"moodboard_{uuid.uuid4()}.jpg")
    create_moodboard(file_paths, output_file)

    # Extract color palettes
    palette_overall = extract_palette(output_file, 8)
    palette_per_image = [extract_palette(p, 5) for p in file_paths]

    # Fake textures + typography suggestions for now
    textures = {
        "top": ["smooth", "grainy", "glossy"],
        "hist": {"smooth": 5, "grainy": 3, "glossy": 2}
    }
    typography_suggestions = [
        {"name": "Montserrat", "category": "Sans-serif", "recommended_use": "Modern clean designs"},
        {"name": "Playfair Display", "category": "Serif", "recommended_use": "Elegant headings"},
        {"name": "Raleway", "category": "Sans-serif", "recommended_use": "Minimalist UI"}
    ]

    # Build response
    result = {
        "ok": True,
        "palette_overall": palette_overall,
        "palette_per_image": palette_per_image,
        "textures": textures,
        "typography_suggestions": typography_suggestions,
        "moodboard_image_url": f"/static/{os.path.basename(output_file)}"
    }

    return JSONResponse(content=result)