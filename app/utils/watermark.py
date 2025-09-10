from typing import Optional, Tuple
import os
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from app.core.config import logger

# Torch + Kornia (GPU-batchable)
try:
    import torch
    import torch.nn.functional as F
    import kornia as K
    import kornia.geometry.transform as KG
    import kornia.filters as KF
except Exception as _ex:
    torch = None  # type: ignore
    K = None  # type: ignore
    KG = None  # type: ignore
    KF = None  # type: ignore
    logger.warning("PyTorch/Kornia not available; falling back to PIL-only logic where needed. Install torch + kornia for GPU acceleration.")


def _pil_to_tensor_rgba(img: Image.Image, device: Optional[str] = None) -> Optional["torch.Tensor"]:
    """Convert PIL RGBA image to torch float tensor CHW in [0,1]. Returns None if torch not available."""
    if torch is None:
        return None
    arr = np.array(img.convert('RGBA'), dtype=np.uint8)  # HWC RGBA
    t = torch.from_numpy(arr).permute(2, 0, 1).float() / 255.0  # CHW
    if device:
        t = t.to(device)
    return t


def _tensor_to_pil_rgb(t: "torch.Tensor") -> Image.Image:
    """Convert torch tensor CHW in [0,1] to PIL RGB."""
    t = t.clamp(0, 1)
    arr = (t * 255.0).byte().permute(1, 2, 0).cpu().numpy()
    return Image.fromarray(arr, mode='RGB')

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
    """Add watermark text at a chosen position using Torch for compositing (GPU if available).
    color: hex like #RRGGBB; opacity: 0..1; bg_box draws a semi-transparent rounded rectangle behind.
    """
    # Prepare base image (RGBA for correct alpha handling)
    if img.mode != "RGBA":
        base_pil = img.convert("RGBA")
    else:
        base_pil = img.copy()

    width, height = base_pil.size
    overlay = Image.new("RGBA", base_pil.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)

    # Font size relative to min dimension
    base_size = max(18, int(min(width, height) * 0.05))
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

    # Shadow and stroke/fill on overlay
    shadow_offset = max(1, base_size // 10)
    draw.text((x + shadow_offset, y + shadow_offset), text, font=font, fill=(0, 0, 0, min(200, a)))
    stroke_w = max(1, base_size // 14)
    draw.text((x, y), text, font=font, fill=(r, g, b, a), stroke_width=stroke_w, stroke_fill=(0, 0, 0, min(220, a)))

    # Torch compositing
    if torch is None:
        return Image.alpha_composite(base_pil, overlay).convert("RGB")

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    base = _pil_to_tensor_rgba(base_pil, device=device)
    overlay_t = _pil_to_tensor_rgba(overlay, device=device)

    base_rgb = base[:3]
    base_a = base[3:4]
    over_rgb = overlay_t[:3]
    over_a = overlay_t[3:4]

    # Porter-Duff over: out_rgb = over_rgb*over_a + base_rgb*(1 - over_a)
    out_rgb = over_rgb * over_a + base_rgb * (1.0 - over_a)
    return _tensor_to_pil_rgb(out_rgb)


def add_signature_watermark(img: Image.Image, signature_rgba: Image.Image, position: str = 'bottom-right', bg_box: bool = False) -> Image.Image:
    """Overlay a signature PNG with alpha using Torch composition; scales to ~30% width, optional bg box and shadow via Kornia blur."""
    # Prepare base and logo tensors
    base_rgba = img.convert('RGBA')
    W, H = base_rgba.size

    if torch is None:
        # Fallback to PIL path (original logic)
        base = base_rgba.copy()
        width, height = base.size
        sig = signature_rgba.convert("RGBA")
        target_w = max(64, int(width * 0.30))
        scale = target_w / sig.width
        target_h = int(sig.height * scale)
        sig_resized = sig.resize((target_w, target_h), Image.LANCZOS)
        padding = max(10, int(min(width, height) * 0.02))
        x, y = _compute_position(width, height, sig_resized.width, sig_resized.height, padding, position)
        if bg_box:
            pad = max(6, int(min(width, height) * 0.01))
            bx0 = max(0, x - pad); by0 = max(0, y - pad)
            bx1 = min(width, x + sig_resized.width + pad); by1 = min(height, y + sig_resized.height + pad)
            box_alpha = int(0.35 * 255)
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            odraw = ImageDraw.Draw(overlay)
            try:
                odraw.rounded_rectangle([bx0, by0, bx1, by1], radius=int(min(bx1-bx0, by1-by0) * 0.08), fill=(0, 0, 0, box_alpha))
            except Exception:
                odraw.rectangle([bx0, by0, bx1, by1], fill=(0, 0, 0, box_alpha))
            base = Image.alpha_composite(base, overlay)
        try:
            alpha = sig_resized.split()[3]
            shadow = Image.new("RGBA", sig_resized.size, (0, 0, 0, 140))
            shadow.putalpha(alpha)
            base.alpha_composite(shadow, (x + 2, y + 2))
        except Exception:
            pass
        base.alpha_composite(sig_resized, (x, y))
        return base.convert('RGB')

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    base = _pil_to_tensor_rgba(base_rgba, device=device)

    # Logo tensor
    sig_rgba = signature_rgba.convert('RGBA')
    sw, sh = sig_rgba.size
    logo = _pil_to_tensor_rgba(sig_rgba, device=device)

    # Resize logo to ~30% of width
    target_w = max(64, int(W * 0.30))
    scale = target_w / float(sw)
    target_h = max(1, int(round(sh * scale)))
    logo = KG.resize(logo.unsqueeze(0), (target_h, target_w), interpolation='bilinear', align_corners=False).squeeze(0)

    # Optional bg box (drawn on separate overlay)
    lw, lh = target_w, target_h
    padding = max(10, int(min(W, H) * 0.02))
    x, y = _compute_position(W, H, lw, lh, padding, position)

    overlay = torch.zeros((4, H, W), device=device)
    if bg_box:
        pad = max(6, int(min(W, H) * 0.01))
        bx0 = max(0, x - pad); by0 = max(0, y - pad)
        bx1 = min(W, x + lw + pad); by1 = min(H, y + lh + pad)
        overlay[:, by0:by1, bx0:bx1] = torch.tensor([0.0, 0.0, 0.0, 0.35], device=device).view(4, 1, 1)

    # Shadow via gaussian blur of alpha
    alpha = logo[3:4]
    if KF is not None:
        shadow = KF.gaussian_blur2d(alpha.unsqueeze(0), (7, 7), (2.0, 2.0)).squeeze(0)
    else:
        shadow = alpha
    # Paint shadow offset by (2,2)
    sx0 = min(W, max(0, x + 2)); sy0 = min(H, max(0, y + 2))
    sx1 = min(W, sx0 + lw); sy1 = min(H, sy0 + lh)
    shw = sx1 - sx0; shh = sy1 - sy0
    if shw > 0 and shh > 0:
        overlay[0:3, sy0:sy1, sx0:sx1] = overlay[0:3, sy0:sy1, sx0:sx1]  # no-op color for shadow (black)
        overlay[3:4, sy0:sy1, sx0:sx1] = torch.maximum(overlay[3:4, sy0:sy1, sx0:sx1], shadow[:, :shh, :shw] * 0.55)

    # Paste logo into overlay at (x,y)
    x0 = max(0, x); y0 = max(0, y)
    x1 = min(W, x0 + lw); y1 = min(H, y0 + lh)
    cw = x1 - x0; ch = y1 - y0
    if cw > 0 and ch > 0:
        overlay[:, y0:y1, x0:x1] = torch.maximum(overlay[:, y0:y1, x0:x1], logo[:, :ch, :cw])

    # Composite
    base_rgb = base[:3]
    over_rgb = overlay[:3]
    over_a = overlay[3:4]
    out_rgb = over_rgb * over_a + base_rgb * (1.0 - over_a)
    return _tensor_to_pil_rgb(out_rgb)


def add_text_watermark_tiled(
    img: Image.Image,
    text: str,
    color: Optional[str] = None,
    opacity: Optional[float] = None,
    angle_deg: float = 30.0,
    spacing_rel: float = 0.3,
    scale_mul: float = 1.0,
) -> Image.Image:
    """Tile watermark text across the whole image; use Torch for tiling/compositing and Kornia for rotation."""
    base_rgba = img.convert('RGBA')
    W, H = base_rgba.size

    # Build text unit tile via PIL (rasterize text), then Torch for the rest
    base_size = max(18, int(min(W, H) * 0.05))
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

    tmp = Image.new('RGBA', (1, 1), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(tmp)
    bbox = tdraw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    bx = max(1, size // 10)
    by = max(1, size // 10)
    unit_w = tw + max(2, size // 5)
    unit_h = th + max(2, size // 5)
    unit = Image.new('RGBA', (unit_w, unit_h), (0, 0, 0, 0))
    udraw = ImageDraw.Draw(unit)
    r, g, b = _parse_hex_color(color or '#ffffff')
    a = int(max(0.0, min(1.0, opacity if opacity is not None else 0.96)) * 255)
    # Shadow, stroke, fill
    udraw.text((bx + max(1, size // 10), by + max(1, size // 10)), text, font=font, fill=(0, 0, 0, min(200, a)))
    stroke_w = max(1, size // 14)
    udraw.text((bx, by), text, font=font, fill=(r, g, b, a), stroke_width=stroke_w, stroke_fill=(0, 0, 0, min(220, a)))

    if torch is None:
        # Fallback to PIL implementation if torch missing
        gap = max(8, int(min(unit_w, unit_h) * max(0.05, min(1.0, spacing_rel or 0.3))))
        step_x = unit_w + gap
        step_y = unit_h + gap
        big = Image.new('RGBA', (W * 3, H * 3), (0, 0, 0, 0))
        for y0 in range(0, big.size[1], step_y):
            for x0 in range(0, big.size[0], step_x):
                big.alpha_composite(unit, (x0, y0))
        rotated = big.rotate(float(angle_deg or 0.0), resample=Image.BICUBIC, expand=True)
        rx, ry = rotated.size
        cx = (rx - W) // 2; cy = (ry - H) // 2
        overlay = rotated.crop((cx, cy, cx + W, cy + H))
        return Image.alpha_composite(base_rgba, overlay).convert('RGB')

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    base = _pil_to_tensor_rgba(base_rgba, device=device)
    # Unit tile tensor
    uh, uw = unit.size[1], unit.size[0]
    unit_t = _pil_to_tensor_rgba(unit, device=device)

    gap = max(8, int(min(unit_w, unit_h) * max(0.05, min(1.0, spacing_rel or 0.3))))
    step_x = unit_w + gap
    step_y = unit_h + gap

    bigW, bigH = W * 3, H * 3
    overlay = torch.zeros((4, bigH, bigW), device=device)
    for y0 in range(0, bigH, step_y):
        for x0 in range(0, bigW, step_x):
            y1 = min(bigH, y0 + uh); x1 = min(bigW, x0 + uw)
            h = y1 - y0; w = x1 - x0
            if h > 0 and w > 0:
                overlay[:, y0:y1, x0:x1] = torch.maximum(overlay[:, y0:y1, x0:x1], unit_t[:, :h, :w])

    # Rotate overlay via Kornia
    angle = float(angle_deg or 0.0)
    overlay = overlay.unsqueeze(0)
    if KG is not None:
        overlay = KG.rotate(overlay, torch.tensor([angle], device=device), align_corners=False)
    else:
        # simple nearest fallback
        pass
    overlay = overlay.squeeze(0)

    # Center crop to W x H
    BH, BW = overlay.shape[1], overlay.shape[2]
    cx = (BW - W) // 2; cy = (BH - H) // 2
    overlay = overlay[:, cy:cy+H, cx:cx+W]

    base_rgb = base[:3]; over_rgb = overlay[:3]; over_a = overlay[3:4]
    out_rgb = over_rgb * over_a + base_rgb * (1.0 - over_a)
    out = (out_rgb.clamp(0, 1) * 255.0).byte().permute(1, 2, 0).cpu().numpy()
    return Image.fromarray(out, mode='RGB')


def add_signature_watermark_tiled(
    img: Image.Image,
    signature_rgba: Image.Image,
    angle_deg: float = 30.0,
    spacing_rel: float = 0.3,
    scale_mul: float = 1.0,
) -> Image.Image:
    """Tile a logo PNG across the whole image; Torch for tiling/compositing and Kornia for rotation/resize/blur."""
    base_rgba = img.convert('RGBA')
    W, H = base_rgba.size
    sig = signature_rgba.convert('RGBA')

    # Determine unit size
    target_w = max(64, int(W * 0.15))
    target_w = int(target_w * max(0.5, min(2.0, scale_mul or 1.0)))
    scale = target_w / sig.width
    target_h = max(1, int(sig.height * scale))
    unit = sig.resize((max(1, target_w), target_h), Image.LANCZOS)

    if torch is None:
        # PIL fallback
        try:
            alpha = unit.split()[3]
            shadow = Image.new('RGBA', unit.size, (0, 0, 0, 140))
            shadow.putalpha(alpha)
            unit_with_shadow = Image.new('RGBA', unit.size, (0, 0, 0, 0))
            unit_with_shadow.alpha_composite(shadow, (2, 2))
            unit_with_shadow.alpha_composite(unit, (0, 0))
            unit = unit_with_shadow
        except Exception:
            pass
        gap = max(8, int(min(unit.size) * max(0.05, min(1.0, spacing_rel or 0.3))))
        step_x = unit.size[0] + gap
        step_y = unit.size[1] + gap
        big = Image.new('RGBA', (W * 3, H * 3), (0, 0, 0, 0))
        for y0 in range(0, big.size[1], step_y):
            for x0 in range(0, big.size[0], step_x):
                big.alpha_composite(unit, (x0, y0))
        rotated = big.rotate(float(angle_deg or 0.0), resample=Image.BICUBIC, expand=True)
        rx, ry = rotated.size
        cx = (rx - W) // 2; cy = (ry - H) // 2
        overlay = rotated.crop((cx, cy, cx + W, cy + H))
        return Image.alpha_composite(base_rgba, overlay).convert('RGB')

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    base = torch.as_tensor(bytearray(base_rgba.tobytes()), dtype=torch.uint8).view(H, W, 4).permute(2, 0, 1).float() / 255.0
    base = base.to(device)
    uh, uw = unit.size[1], unit.size[0]
    unit_t = torch.as_tensor(bytearray(unit.tobytes()), dtype=torch.uint8).view(uh, uw, 4).permute(2, 0, 1).float() / 255.0
    unit_t = unit_t.to(device)

    # Blur alpha for subtle shadow
    if KF is not None:
        blurred_a = KF.gaussian_blur2d(unit_t[3:4].unsqueeze(0), (7, 7), (2, 2)).squeeze(0)
        shadow = torch.zeros_like(unit_t)
        shadow[3:4] = blurred_a * 0.55
        shadow_rgb = torch.zeros_like(unit_t[:3])  # black shadow
        unit_t = torch.maximum(unit_t, torch.cat([shadow_rgb, shadow[3:4]], dim=0))

    gap = max(8, int(min(unit.size) * max(0.05, min(1.0, spacing_rel or 0.3))))
    step_x = uw + gap
    step_y = uh + gap

    bigW, bigH = W * 3, H * 3
    overlay = torch.zeros((4, bigH, bigW), device=device)
    for y0 in range(0, bigH, step_y):
        for x0 in range(0, bigW, step_x):
            y1 = min(bigH, y0 + uh); x1 = min(bigW, x0 + uw)
            h = y1 - y0; w = x1 - x0
            if h > 0 and w > 0:
                overlay[:, y0:y1, x0:x1] = torch.maximum(overlay[:, y0:y1, x0:x1], unit_t[:, :h, :w])

    # Rotate overlay
    overlay = overlay.unsqueeze(0)
    if KG is not None:
        overlay = KG.rotate(overlay, torch.tensor([float(angle_deg or 0.0)], device=device), align_corners=False)
    overlay = overlay.squeeze(0)

    # Center crop
    BH, BW = overlay.shape[1], overlay.shape[2]
    cx = (BW - W) // 2; cy = (BH - H) // 2
    overlay = overlay[:, cy:cy+H, cx:cx+W]

    base_rgb = base[:3]; over_rgb = overlay[:3]; over_a = overlay[3:4]
    out_rgb = over_rgb * over_a + base_rgb * (1.0 - over_a)
    out = (out_rgb.clamp(0, 1) * 255.0).byte().permute(1, 2, 0).cpu().numpy()
    return Image.fromarray(out, mode='RGB')