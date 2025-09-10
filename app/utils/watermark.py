from typing import Optional, Tuple
import os
from PIL import Image, ImageDraw, ImageFont
from app.core.config import logger


def _compute_position(img_w: int, img_h: int, box_w: int, box_h: int, padding: int, pos: str) -> Tuple[int, int]:
    pos = (pos or 'bottom-right').lower()
    if pos == 'top-left':
        return padding, padding
    if pos == 'top-right':
        return img_w - box_w - padding, padding
    if pos == 'bottom-left':
        return padding, img_h - box_h - padding
    if pos == 'center':
        return (img_w - box_w) // 2, (img_h - box_h) // 2
    # default bottom-right
    return img_w - box_w - padding, img_h - box_h - padding


def _parse_hex_color(s: str) -> Tuple[int, int, int]:
    try:
        s = (s or '').strip().lstrip('#')
        if len(s) == 3:
            s = ''.join(ch * 2 for ch in s)
        if len(s) < 6:
            return (255, 255, 255)
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (r, g, b)
    except Exception:
        return (255, 255, 255)


def add_text_watermark(
    img: Image.Image,
    text: str,
    position: str = 'bottom-right',
    color: Optional[str] = None,
    opacity: Optional[float] = None,
    bg_box: bool = False,
) -> Image.Image:
    """Add watermark text at a chosen position; scales with image size.
    color: hex like #RRGGBB; opacity: 0..1; bg_box draws a semi-transparent rounded rectangle behind.
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    width, height = img.size
    overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)

    # Font size relative to min dimension
    base_size = max(18, int(min(width, height) * 0.05))
    font = None
    # Try common fonts in order for consistent sizing across envs
    font_candidates = [
        os.getenv("WATERMARK_TTF"),                  # explicit override
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
        "/System/Library/Fonts/Supplemental/Arial.ttf",      # macOS system Arial
        "C:/Windows/Fonts/arial.ttf",                         # Windows Arial
        "arial.ttf",                                          # current dir fallback
    ]
    for fp in font_candidates:
        if not fp:
            continue
        try:
            font = ImageFont.truetype(fp, base_size)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", base_size)
        except Exception:
            font = ImageFont.load_default()
            logger.warning("Falling back to PIL default bitmap font; watermark text may appear small. Provide WATERMARK_TTF or install DejaVuSans/Arial.")

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    padding = max(10, base_size // 2)
    x, y = _compute_position(width, height, tw, th, padding, position)

    r, g, b = _parse_hex_color(color or '#ffffff')
    a = int(max(0.0, min(1.0, opacity if opacity is not None else 0.96)) * 255)

    # Optional background box
    if bg_box:
        pad_x = max(6, int(base_size * 0.4))
        pad_y = max(4, int(base_size * 0.25))
        bx0 = max(0, x - pad_x)
        by0 = max(0, y - int(pad_y * 0.6))
        bx1 = min(width, x + tw + pad_x)
        by1 = min(height, y + th + pad_y)
        box_alpha = int(0.32 * 255)
        try:
            draw.rounded_rectangle([bx0, by0, bx1, by1], radius=int(min(bx1-bx0, by1-by0) * 0.12), fill=(0, 0, 0, box_alpha))
        except Exception:
            draw.rectangle([bx0, by0, bx1, by1], fill=(0, 0, 0, box_alpha))

    # Shadow
    shadow_offset = max(1, base_size // 10)
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=(0, 0, 0, min(200, a)))

    # Main text with stroke
    stroke_w = max(1, base_size // 14)
    draw.text((x, y), text, font=font, fill=(r, g, b, a), stroke_width=stroke_w, stroke_fill=(0, 0, 0, min(220, a)))

    watermarked = Image.alpha_composite(img, overlay)
    return watermarked.convert("RGB")


def add_signature_watermark(img: Image.Image, signature_rgba: Image.Image, position: str = 'bottom-right', bg_box: bool = False) -> Image.Image:
    """Overlay a signature PNG with alpha at a chosen position; scales to ~30% of width.
    bg_box draws a semi-transparent rounded rectangle behind the logo to increase robustness.
    """
    if img.mode != "RGBA":
        base = img.convert("RGBA")
    else:
        base = img.copy()

    width, height = base.size

    sig = signature_rgba.convert("RGBA")

    target_w = max(64, int(width * 0.30))
    scale = target_w / sig.width
    target_h = int(sig.height * scale)
    sig_resized = sig.resize((target_w, target_h), Image.LANCZOS)

    padding = max(10, int(min(width, height) * 0.02))
    x, y = _compute_position(width, height, sig_resized.width, sig_resized.height, padding, position)

    # Optional background box behind the logo
    if bg_box:
        pad = max(6, int(min(width, height) * 0.01))
        bx0 = max(0, x - pad)
        by0 = max(0, y - pad)
        bx1 = min(width, x + sig_resized.width + pad)
        by1 = min(height, y + sig_resized.height + pad)
        box_alpha = int(0.35 * 255)
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        try:
            odraw.rounded_rectangle([bx0, by0, bx1, by1], radius=int(min(bx1-bx0, by1-by0) * 0.08), fill=(0, 0, 0, box_alpha))
        except Exception:
            odraw.rectangle([bx0, by0, bx1, by1], fill=(0, 0, 0, box_alpha))
        base = Image.alpha_composite(base, overlay)

    # Shadow/glow
    try:
        alpha = sig_resized.split()[3]
        shadow = Image.new("RGBA", sig_resized.size, (0, 0, 0, 140))
        shadow.putalpha(alpha)
        base.alpha_composite(shadow, (x + 2, y + 2))
    except Exception:
        pass

    base.alpha_composite(sig_resized, (x, y))
    return base.convert("RGB")


def add_text_watermark_tiled(
    img: Image.Image,
    text: str,
    color: Optional[str] = None,
    opacity: Optional[float] = None,
    angle_deg: float = 30.0,
    spacing_rel: float = 0.3,
    scale_mul: float = 1.0,
) -> Image.Image:
    """Tile watermark text across the whole image with rotation.
    spacing_rel: 0.05..1 relative to unit box; scale_mul: 0.5..2 multiplier of base size.
    """
    if img.mode != "RGBA":
        base = img.convert("RGBA")
    else:
        base = img.copy()

    width, height = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    base_size = max(18, int(min(width, height) * 0.05))
    size = int(base_size * max(0.5, min(2.0, scale_mul or 1.0)))

    font = None
    font_candidates = [
        os.getenv("WATERMARK_TTF"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "arial.ttf",
    ]
    for fp in font_candidates:
        if not fp:
            continue
        try:
            font = ImageFont.truetype(fp, size)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", size)
        except Exception:
            font = ImageFont.load_default()

    # Render a unit tile: shadow, stroke, fill
    tmp = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(tmp)
    bbox = tdraw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    bx = max(1, size // 10)
    by = max(1, size // 10)
    unit_w = tw + max(2, size // 5)
    unit_h = th + max(2, size // 5)
    unit = Image.new("RGBA", (unit_w, unit_h), (0, 0, 0, 0))
    udraw = ImageDraw.Draw(unit)

    r, g, b = _parse_hex_color(color or '#ffffff')
    a = int(max(0.0, min(1.0, opacity if opacity is not None else 0.96)) * 255)

    # Shadow
    udraw.text((bx + max(1, size // 10), by + max(1, size // 10)), text, font=font, fill=(0, 0, 0, min(200, a)))
    # Stroke and fill
    stroke_w = max(1, size // 14)
    udraw.text((bx, by), text, font=font, fill=(r, g, b, a), stroke_width=stroke_w, stroke_fill=(0, 0, 0, min(220, a)))

    gap = max(8, int(min(unit_w, unit_h) * max(0.05, min(1.0, spacing_rel or 0.3))))
    step_x = unit_w + gap
    step_y = unit_h + gap

    # Build tiled layer by rotating context via separate image
    tiled = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    angle = float(angle_deg or 0.0)

    # We draw on a larger canvas to avoid empty corners after rotation
    big = Image.new("RGBA", (width * 3, height * 3), (0, 0, 0, 0))
    for y0 in range(0, big.size[1], step_y):
        for x0 in range(0, big.size[0], step_x):
            big.alpha_composite(unit, (x0, y0))

    rotated = big.rotate(angle, resample=Image.BICUBIC, expand=True)
    # Paste center crop to match original size
    rx, ry = rotated.size
    cx = (rx - width) // 2
    cy = (ry - height) // 2
    overlay = rotated.crop((cx, cy, cx + width, cy + height))

    out = Image.alpha_composite(base, overlay)
    return out.convert("RGB")


def add_signature_watermark_tiled(
    img: Image.Image,
    signature_rgba: Image.Image,
    angle_deg: float = 30.0,
    spacing_rel: float = 0.3,
    scale_mul: float = 1.0,
) -> Image.Image:
    """Tile a logo PNG across the whole image with rotation."""
    if img.mode != "RGBA":
        base = img.convert("RGBA")
    else:
        base = img.copy()

    width, height = base.size
    sig = signature_rgba.convert("RGBA")

    target_w = max(64, int(width * 0.15))
    target_w = int(target_w * max(0.5, min(2.0, scale_mul or 1.0)))
    scale = target_w / sig.width
    target_h = int(sig.height * scale)
    unit = sig.resize((max(1, target_w), max(1, target_h)), Image.LANCZOS)

    # Build unit with simple shadow
    try:
        alpha = unit.split()[3]
        shadow = Image.new("RGBA", unit.size, (0, 0, 0, 140))
        shadow.putalpha(alpha)
        unit_with_shadow = Image.new("RGBA", unit.size, (0, 0, 0, 0))
        unit_with_shadow.alpha_composite(shadow, (2, 2))
        unit_with_shadow.alpha_composite(unit, (0, 0))
        unit = unit_with_shadow
    except Exception:
        pass

    gap = max(8, int(min(unit.size) * max(0.05, min(1.0, spacing_rel or 0.3))))
    step_x = unit.size[0] + gap
    step_y = unit.size[1] + gap

    big = Image.new("RGBA", (width * 3, height * 3), (0, 0, 0, 0))
    for y0 in range(0, big.size[1], step_y):
        for x0 in range(0, big.size[0], step_x):
            big.alpha_composite(unit, (x0, y0))

    angle = float(angle_deg or 0.0)
    rotated = big.rotate(angle, resample=Image.BICUBIC, expand=True)
    rx, ry = rotated.size
    cx = (rx - width) // 2
    cy = (ry - height) // 2
    overlay = rotated.crop((cx, cy, cx + width, cy + height))

    out = Image.alpha_composite(base, overlay)
    return out.convert("RGB")