from typing import List, Optional, Tuple
import io
import os
import time
from collections import Counter

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from PIL import Image, ImageDraw, ImageFont

from app.core.config import MAX_FILES, logger
from app.core.auth import resolve_workspace_uid, has_role_access
from app.utils.clip_utils import best_clip_label
from app.utils.storage import upload_bytes

router = APIRouter(prefix="/api", tags=["moodboard"]) 


# --- Helpers ---
def _hex(c: Tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % c


def extract_palette(im: Image.Image, k: int = 6) -> List[str]:
    """Fast dominant palette using Pillow's adaptive palette.
    Returns list of hex colors.
    """
    if im.mode != "RGB":
        im = im.convert("RGB")
    # Downscale for speed
    w, h = im.size
    scale = max(1, int(max(w, h) / 512))
    if scale > 1:
        im_small = im.resize((w // scale, h // scale), Image.LANCZOS)
    else:
        im_small = im
    pal = im_small.convert("P", palette=Image.ADAPTIVE, colors=k)
    pal = pal.convert("RGB")
    # Get most frequent colors
    colors = pal.getcolors(maxcolors=256 * 256) or []
    colors.sort(key=lambda x: x[0], reverse=True)
    hexes = []
    for count, col in colors[:k]:
        hexes.append(_hex(col))
    return hexes


def texture_label(im: Image.Image) -> str:
    """Use CLIP zero-shot labels to suggest a texture/style label."""
    labels = [
        "minimal clean",
        "matte soft",
        "grainy film",
        "high contrast gritty",
        "vintage faded",
        "glossy sharp",
        "pastel airy",
        "moody dark",
        "warm earthy",
        "cold clinical",
    ]
    label, score = best_clip_label(im, labels)
    return label or "minimal clean"


def suggest_typography(top_textures: List[str], palette: List[str]) -> List[dict]:
    """Map textures and palette contrast to font suggestions."""
    # Simple mapping rules
    texture_to_fonts = {
        "minimal clean": [
            {"name": "Inter", "category": "Sans", "recommended_use": "UI and headings"},
            {"name": "Montserrat", "category": "Sans", "recommended_use": "Headlines"},
        ],
        "matte soft": [
            {"name": "Avenir", "category": "Sans", "recommended_use": "Headings & captions"},
            {"name": "Rubik", "category": "Sans", "recommended_use": "Body"},
        ],
        "grainy film": [
            {"name": "Playfair Display", "category": "Serif", "recommended_use": "Editorial feel"},
            {"name": "Futura", "category": "Geometric Sans", "recommended_use": "Accents"},
        ],
        "high contrast gritty": [
            {"name": "Bebas Neue", "category": "Display", "recommended_use": "Bold headers"},
            {"name": "Oswald", "category": "Condensed Sans", "recommended_use": "Labels"},
        ],
        "vintage faded": [
            {"name": "Bodoni", "category": "Serif", "recommended_use": "Headlines"},
            {"name": "Libre Baskerville", "category": "Serif", "recommended_use": "Body"},
        ],
        "glossy sharp": [
            {"name": "Helvetica Neue", "category": "Sans", "recommended_use": "Headlines"},
            {"name": "Poppins", "category": "Sans", "recommended_use": "UI"},
        ],
        "pastel airy": [
            {"name": "Nunito", "category": "Rounded Sans", "recommended_use": "UI and body"},
            {"name": "Quicksand", "category": "Rounded Sans", "recommended_use": "Captions"},
        ],
        "moody dark": [
            {"name": "Cormorant Garamond", "category": "Serif", "recommended_use": "Display"},
            {"name": "Work Sans", "category": "Sans", "recommended_use": "Body"},
        ],
        "warm earthy": [
            {"name": "Merriweather", "category": "Serif", "recommended_use": "Body"},
            {"name": "Source Sans 3", "category": "Sans", "recommended_use": "UI"},
        ],
        "cold clinical": [
            {"name": "SF Pro", "category": "Sans", "recommended_use": "UI"},
            {"name": "IBM Plex Sans", "category": "Sans", "recommended_use": "Body"},
        ],
    }

    # Palette-based tweak: if palette is very high contrast, add a modern grotesk
    def contrast(palette: List[str]) -> float:
        import re
        def parse_hex(h):
            h = h.lstrip('#')
            r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
            return r, g, b
        cols = [parse_hex(h) for h in palette[:2]] if palette else []
        if len(cols) < 2:
            return 0.0
        (r1,g1,b1), (r2,g2,b2) = cols
        return ((r1-r2)**2 + (g1-g2)**2 + (b1-b2)**2) ** 0.5 / 441.67  # normalize ~ [0..1]

    picks: List[dict] = []
    for t in top_textures[:2]:
        picks.extend(texture_to_fonts.get(t, texture_to_fonts["minimal clean"]))
    if contrast(palette) > 0.5:
        picks.append({"name": "Söhne / Graphik", "category": "Grotesk Sans", "recommended_use": "Bold headings"})

    # Deduplicate by name
    seen = set()
    uniq = []
    for p in picks:
        if p["name"] not in seen:
            seen.add(p["name"])
            uniq.append(p)
    return uniq[:6]


def build_moodboard_collage(images: List[Image.Image], palette: List[str]) -> bytes:
    """Generate a landscape collage with images grid and palette swatches."""
    W, H = 1800, 1000
    canvas = Image.new("RGB", (W, H), (20, 20, 22))
    draw = ImageDraw.Draw(canvas)

    # Grid: 5 columns x 2 rows for up to 10 images
    cols, rows = 5, 2
    pad = 10
    grid_w = W - 360  # leave right panel for palette/notes
    cell_w = (grid_w - (cols + 1) * pad) // cols
    cell_h = (H - (rows + 1) * pad) // rows

    i = 0
    for r in range(rows):
        for c in range(cols):
            if i >= len(images):
                break
            im = images[i]
            if im.mode != "RGB":
                im = im.convert("RGB")
            # fit into cell while preserving aspect
            im_copy = im.copy()
            im_copy.thumbnail((cell_w, cell_h), Image.LANCZOS)
            x = pad + c * (cell_w + pad)
            y = pad + r * (cell_h + pad)
            # center in cell
            ox = x + (cell_w - im_copy.width) // 2
            oy = y + (cell_h - im_copy.height) // 2
            # background
            draw.rounded_rectangle([x, y, x + cell_w, y + cell_h], radius=12, fill=(32, 32, 36))
            canvas.paste(im_copy, (ox, oy))
            i += 1

    # Right panel
    panel_x = grid_w + pad
    draw.rounded_rectangle([panel_x, pad, W - pad, H - pad], radius=16, fill=(28, 28, 32))

    # Title
    title = "Moodboard"
    try:
        font_title = ImageFont.truetype("arial.ttf", 34)
        font_small = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font_title = ImageFont.load_default()
        font_small = ImageFont.load_default()
    draw.text((panel_x + 20, 25), title, fill=(240, 240, 245), font=font_title)

    # Palette swatches
    sx, sy = panel_x + 20, 80
    sw, sh = (W - panel_x - 40), 40
    for idx, hx in enumerate(palette[:8]):
        y0 = sy + idx * (sh + 12)
        # parse color
        r = int(hx[1:3], 16); g = int(hx[3:5], 16); b = int(hx[5:7], 16)
        draw.rounded_rectangle([sx, y0, sx + sw, y0 + sh], radius=10, fill=(r, g, b))
        # label
        text_color = (0,0,0) if (r*0.299+g*0.587+b*0.114) > 160 else (255,255,255)
        draw.text((sx + 10, y0 + 10), hx.upper(), fill=text_color, font=font_small)

    # Export
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


@router.post("/moodboard/generate")
async def generate_moodboard(
    request: Request,
    files: List[UploadFile] = File(..., description="Upload 10 images of your work"),
):
    # Auth
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'retouch'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not files or len(files) < 3:
        return JSONResponse({"error": "Please upload at least 3 images (10 recommended)."}, status_code=400)
    if len(files) > min(MAX_FILES, 20):
        return JSONResponse({"error": f"Too many files (max {min(MAX_FILES,20)})"}, status_code=400)

    # Read and load images
    pil_images: List[Image.Image] = []
    per_palettes: List[List[str]] = []
    textures: List[str] = []

    for uf in files[:20]:
        raw = await uf.read()
        if not raw:
            continue
        try:
            im = Image.open(io.BytesIO(raw))
            pil_images.append(im)
            per_palettes.append(extract_palette(im, k=6))
            textures.append(texture_label(im))
        except Exception as ex:
            logger.warning(f"Failed to process image {uf.filename}: {ex}")

    if not pil_images:
        return JSONResponse({"error": "Failed to read any image."}, status_code=400)

    # Aggregate palette: count most frequent colors from per-image palettes
    counter = Counter()
    for pal in per_palettes:
        counter.update(pal)
    overall_palette = [c for c, _ in counter.most_common(8)]

    # Aggregate textures
    texture_counts = Counter(textures)
    top_textures = [t for t, _ in texture_counts.most_common(3)]

    # Typography suggestions
    type_suggestions = suggest_typography(top_textures, overall_palette)

    # Build collage and upload
    collage_bytes = build_moodboard_collage(pil_images[:10], overall_palette)
    ts = int(time.time())
    key = f"users/{eff_uid}/moodboards/moodboard_{ts}.jpg"
    url = upload_bytes(key, collage_bytes, content_type="image/jpeg")

    return {
        "ok": True,
        "palette_overall": overall_palette,
        "palette_per_image": per_palettes,
        "textures": {
            "top": top_textures,
            "hist": dict(texture_counts),
        },
        "typography_suggestions": type_suggestions,
        "moodboard_image_url": url,
    }