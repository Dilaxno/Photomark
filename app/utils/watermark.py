from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageFont


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
) -> Image.Image:
    """Add watermark text at a chosen position; scales with image size.
    color: hex like #RRGGBB; opacity: 0..1
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    width, height = img.size
    overlay = Image.new("RGBA", img.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)

    # Font size relative to min dimension
    base_size = max(18, int(min(width, height) * 0.05))
    try:
        font = ImageFont.truetype("arial.ttf", base_size)
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    padding = max(10, base_size // 2)
    x, y = _compute_position(width, height, tw, th, padding, position)

    r, g, b = _parse_hex_color(color or '#ffffff')
    a = int(max(0.0, min(1.0, opacity if opacity is not None else 0.96)) * 255)

    # Shadow
    shadow_offset = max(1, base_size // 10)
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=(0, 0, 0, min(200, a)))

    # Main text with stroke
    stroke_w = max(1, base_size // 14)
    draw.text((x, y), text, font=font, fill=(r, g, b, a), stroke_width=stroke_w, stroke_fill=(0, 0, 0, min(220, a)))

    watermarked = Image.alpha_composite(img, overlay)
    return watermarked.convert("RGB")


def add_signature_watermark(img: Image.Image, signature_rgba: Image.Image, position: str = 'bottom-right') -> Image.Image:
    """Overlay a signature PNG with alpha at a chosen position; scales to ~30% of width."""
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