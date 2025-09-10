from typing import Optional, Tuple
import os
import numpy as np
import cv2
from PIL import Image
from app.core.config import logger

try:
    # FreeType support is provided by opencv-contrib builds
    import cv2.freetype as cv2_freetype  # type: ignore
    HAS_FT = True
except Exception:
    cv2_freetype = None  # type: ignore
    HAS_FT = False


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


def _font_candidates() -> list[str | None]:
    return [
        os.getenv("WATERMARK_TTF"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "arial.ttf",
    ]


def _resolve_ttf() -> Optional[str]:
    for fp in _font_candidates():
        if not fp:
            continue
        if os.path.exists(fp):
            return fp
    # last resort: try DejaVuSans from path resolution
    try:
        import matplotlib
        dv = os.path.join(os.path.dirname(matplotlib.__file__), 'mpl-data', 'fonts', 'ttf', 'DejaVuSans.ttf')
        if os.path.exists(dv):
            return dv
    except Exception:
        pass
    return None


def _pil_to_bgra(img: Image.Image) -> np.ndarray:
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    arr = np.array(img)  # RGBA
    b,g,r,a = cv2.split(arr)
    return cv2.merge([b,g,r,a])


def _bgra_to_pil(arr: np.ndarray) -> Image.Image:
    b,g,r,a = cv2.split(arr)
    rgba = cv2.merge([r,g,b,a])
    return Image.fromarray(rgba, mode='RGBA').convert('RGB')


def _draw_text_ft(canvas_bgra: np.ndarray, text: str, org: Tuple[int,int], ttf_path: str, font_px: int, color_bgr: Tuple[int,int,int], alpha: float = 0.96, stroke: int = 0) -> Tuple[int,int]:
    """Draw text using FreeType on a BGRA canvas; returns text size (w,h)."""
    if not HAS_FT:
        raise RuntimeError('cv2.freetype not available')
    ft = cv2_freetype.createFreeType2()  # type: ignore[attr-defined]
    ft.loadFontData(fontFileName=ttf_path, id=0)
    # Measure
    (w, h), baseline = ft.getTextSize(text, fontHeight=font_px, thickness=stroke)  # type: ignore
    x, y = org
    # Shadow
    sh = max(1, font_px // 10)
    shadow_col = (0,0,0)
    ft.putText(canvas_bgra, text, (x + sh, y + sh + h), fontHeight=font_px, color=shadow_col + (int(min(200, alpha*255)),), thickness=stroke, line_type=cv2.LINE_AA, bottomLeftOrigin=False)  # type: ignore
    # Main text
    b,g,r = color_bgr
    ft.putText(canvas_bgra, text, (x, y + h), fontHeight=font_px, color=(b,g,r,int(alpha*255)), thickness=stroke, line_type=cv2.LINE_AA, bottomLeftOrigin=False)  # type: ignore
    return (w, h)


def add_text_watermark(
    img: Image.Image,
    text: str,
    position: str = 'bottom-right',
    color: Optional[str] = None,
    opacity: Optional[float] = None,
    bg_box: bool = False,
) -> Image.Image:
    """Add watermark text with OpenCV+FreeType (fallback to PIL if unavailable)."""
    width, height = img.size
    base_size = max(18, int(min(width, height) * 0.05))
    padding = max(10, base_size // 2)
    r, g, b = _parse_hex_color(color or '#ffffff')
    a = float(max(0.0, min(1.0, opacity if opacity is not None else 0.96)))

    ttf = _resolve_ttf()
    if not HAS_FT or not ttf:
        logger.warning("cv2.freetype or TTF not available; falling back to PIL for text watermark")
        # Fallback to PIL rendering by delegating to previous algorithm via simple path
        from PIL import ImageDraw, ImageFont  # local import to reduce global PIL use
        im_rgba = img.convert('RGBA')
        overlay = Image.new('RGBA', im_rgba.size, (255,255,255,0))
        draw = ImageDraw.Draw(overlay)
        try:
            font = ImageFont.truetype(ttf or 'DejaVuSans.ttf', base_size)
        except Exception:
            font = ImageFont.load_default()
        tw, th = draw.textbbox((0,0), text, font=font)[2:4]
        x, y = _compute_position(width, height, tw, th, padding, position)
        if bg_box:
            pad_x = max(6, int(base_size * 0.4))
            pad_y = max(4, int(base_size * 0.25))
            bx0 = max(0, x - pad_x)
            by0 = max(0, y - int(pad_y * 0.6))
            bx1 = min(width, x + tw + pad_x)
            by1 = min(height, y + th + pad_y)
            try:
                draw.rounded_rectangle([bx0, by0, bx1, by1], radius=int(min(bx1-bx0, by1-by0) * 0.12), fill=(0,0,0,int(0.32*255)))
            except Exception:
                draw.rectangle([bx0, by0, bx1, by1], fill=(0,0,0,int(0.32*255)))
        shadow_offset = max(1, base_size // 10)
        draw.text((x+shadow_offset, y+shadow_offset), text, font=font, fill=(0,0,0,int(min(200, a*255))))
        stroke_w = max(1, base_size // 14)
        draw.text((x,y), text, font=font, fill=(r,g,b,int(a*255)), stroke_width=stroke_w, stroke_fill=(0,0,0,int(min(220, a*255))))
        out = Image.alpha_composite(im_rgba, overlay)
        return out.convert('RGB')

    # OpenCV path
    base = _pil_to_bgra(img)
    h, w = base.shape[:2]
    alpha_layer = base[:, :, 3]
    overlay = np.zeros_like(base)

    # Estimate text size by drawing off-screen
    tmp = np.zeros_like(base)
    tw, th = _draw_text_ft(tmp, text, (0,0), ttf, base_size, (b,g,r), alpha=a, stroke=max(1, base_size//14))
    x, y = _compute_position(w, h, tw, th, padding, position)

    # Optional background box
    if bg_box:
        pad_x = max(6, int(base_size * 0.4))
        pad_y = max(4, int(base_size * 0.25))
        bx0 = max(0, x - pad_x)
        by0 = max(0, y - int(pad_y * 0.6))
        bx1 = min(w, x + tw + pad_x)
        by1 = min(h, y + th + pad_y)
        box = overlay[by0:by1, bx0:bx1]
        if box.size:
            # semi-transparent black with rounded corners approximation
            cv2.rectangle(box, (0,0), (bx1-bx0-1, by1-by0-1), (0,0,0,int(0.32*255)), thickness=-1)

    # Draw text (includes shadow and main)
    _draw_text_ft(overlay, text, (x, y), ttf, base_size, (b,g,r), alpha=a, stroke=max(1, base_size//14))

    # Alpha composite
    out = base.copy()
    # Pre-multiplied alpha blending
    ov_a = overlay[:,:,3:4].astype(np.float32)/255.0
    bg_a = out[:,:,3:4].astype(np.float32)/255.0
    out[:,:,:3] = (overlay[:,:,:3].astype(np.float32)*ov_a + out[:,:,:3].astype(np.float32)*(1-ov_a)).astype(np.uint8)
    out[:,:,3] = np.clip((ov_a + bg_a*(1-ov_a))*255.0, 0, 255).astype(np.uint8).squeeze()
    return _bgra_to_pil(out)


def add_signature_watermark(img: Image.Image, signature_rgba: Image.Image, position: str = 'bottom-right', bg_box: bool = False) -> Image.Image:
    """Overlay signature PNG using OpenCV alpha blending; scales to ~30% width."""
    base = _pil_to_bgra(img)
    sig = signature_rgba.convert('RGBA')
    sig_bgra = _pil_to_bgra(sig)
    h, w = base.shape[:2]
    target_w = max(64, int(w * 0.30))
    scale = target_w / sig_bgra.shape[1]
    target_h = int(sig_bgra.shape[0] * scale)
    sig_resized = cv2.resize(sig_bgra, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)
    padding = max(10, int(min(w, h) * 0.02))
    x, y = _compute_position(w, h, sig_resized.shape[1], sig_resized.shape[0], padding, position)

    if bg_box:
        pad = max(6, int(min(w, h) * 0.01))
        bx0 = max(0, x - pad); by0 = max(0, y - pad)
        bx1 = min(w, x + sig_resized.shape[1] + pad); by1 = min(h, y + sig_resized.shape[0] + pad)
        cv2.rectangle(base, (bx0, by0), (bx1, by1), (0,0,0,int(0.35*255)), thickness=-1)

    # Shadow
    try:
        shadow = sig_resized.copy()
        shadow[:,:,:3] = 0
        shadow[:,:,3] = (shadow[:,:,3].astype(np.float32) * (140.0/255.0)).astype(np.uint8)
        sx, sy = x+2, y+2
        roi = base[sy:sy+shadow.shape[0], sx:sx+shadow.shape[1]]
        if roi.shape[:2] == shadow.shape[:2]:
            a = (shadow[:,:,3:4].astype(np.float32)/255.0)
            roi[:,:,:3] = (shadow[:,:,:3].astype(np.float32)*a + roi[:,:,:3].astype(np.float32)*(1-a)).astype(np.uint8)
            roi[:,:,3] = np.clip((a + roi[:,:,3:4].astype(np.float32)/255.0*(1-a))*255.0, 0, 255).astype(np.uint8).squeeze()
            base[sy:sy+shadow.shape[0], sx:sx+shadow.shape[1]] = roi
    except Exception:
        pass

    # Blend logo
    roi = base[y:y+sig_resized.shape[0], x:x+sig_resized.shape[1]]
    if roi.shape[:2] == sig_resized.shape[:2]:
        a = (sig_resized[:,:,3:4].astype(np.float32)/255.0)
        roi[:,:,:3] = (sig_resized[:,:,:3].astype(np.float32)*a + roi[:,:,:3].astype(np.float32)*(1-a)).astype(np.uint8)
        roi[:,:,3] = np.clip((a + roi[:,:,3:4].astype(np.float32)/255.0*(1-a))*255.0, 0, 255).astype(np.uint8).squeeze()
        base[y:y+sig_resized.shape[0], x:x+sig_resized.shape[1]] = roi
    return _bgra_to_pil(base)


def add_text_watermark_tiled(
    img: Image.Image,
    text: str,
    color: Optional[str] = None,
    opacity: Optional[float] = None,
    angle_deg: float = 30.0,
    spacing_rel: float = 0.3,
    scale_mul: float = 1.0,
) -> Image.Image:
    """Tile watermark text with OpenCV+FreeType; rotation via cv2.warpAffine."""
    if not HAS_FT:
        logger.warning("cv2.freetype not available; falling back to single watermark via PIL path")
        return add_text_watermark(img, text, position='center', color=color, opacity=opacity, bg_box=False)

    ttf = _resolve_ttf()
    if not ttf:
        logger.warning("TTF font not found; falling back to single watermark")
        return add_text_watermark(img, text, position='center', color=color, opacity=opacity, bg_box=False)

    base = _pil_to_bgra(img)
    h, w = base.shape[:2]
    base_size = max(18, int(min(w, h) * 0.05))
    size = int(base_size * max(0.5, min(2.0, scale_mul or 1.0)))
    r, g, b = _parse_hex_color(color or '#ffffff')
    a = float(max(0.0, min(1.0, opacity if opacity is not None else 0.96)))

    # Render unit tile on its own BGRA canvas
    tmp = np.zeros((1,1,4), dtype=np.uint8)
    unit_dummy = np.zeros((size*3, size*10, 4), dtype=np.uint8)
    tw, th = _draw_text_ft(unit_dummy, text, (0,0), ttf, size, (b,g,r), alpha=a, stroke=max(1,size//14))
    bx = max(1, size // 10)
    by = max(1, size // 10)
    unit_w = tw + max(2, size // 5)
    unit_h = th + max(2, size // 5)
    unit = np.zeros((unit_h, unit_w, 4), dtype=np.uint8)
    _draw_text_ft(unit, text, (bx, by), ttf, size, (b,g,r), alpha=a, stroke=max(1,size//14))

    gap = max(8, int(min(unit_w, unit_h) * max(0.05, min(1.0, spacing_rel or 0.3))))
    step_x = unit_w + gap
    step_y = unit_h + gap

    big = np.zeros((h*3, w*3, 4), dtype=np.uint8)
    for y0 in range(0, big.shape[0], step_y):
        for x0 in range(0, big.shape[1], step_x):
            uy, ux = unit.shape[0], unit.shape[1]
            if y0+uy <= big.shape[0] and x0+ux <= big.shape[1]:
                roi = big[y0:y0+uy, x0:x0+ux]
                a_u = (unit[:,:,3:4].astype(np.float32)/255.0)
                roi[:,:,:3] = (unit[:,:,:3].astype(np.float32)*a_u + roi[:,:,:3].astype(np.float32)*(1-a_u)).astype(np.uint8)
                roi[:,:,3] = np.clip((a_u + roi[:,:,3:4].astype(np.float32)/255.0*(1-a_u))*255.0, 0, 255).astype(np.uint8).squeeze()

    angle = float(angle_deg or 0.0)
    M = cv2.getRotationMatrix2D((big.shape[1]/2, big.shape[0]/2), angle, 1.0)
    rotated = cv2.warpAffine(big, M, (big.shape[1], big.shape[0]), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_TRANSPARENT)
    cx = (rotated.shape[1] - w)//2
    cy = (rotated.shape[0] - h)//2
    overlay = rotated[cy:cy+h, cx:cx+w]

    out = base.copy()
    a_ov = (overlay[:,:,3:4].astype(np.float32)/255.0)
    out[:,:,:3] = (overlay[:,:,:3].astype(np.float32)*a_ov + out[:,:,:3].astype(np.float32)*(1-a_ov)).astype(np.uint8)
    out[:,:,3] = np.clip((a_ov + out[:,:,3:4].astype(np.float32)/255.0*(1-a_ov))*255.0, 0, 255).astype(np.uint8).squeeze()
    return _bgra_to_pil(out)


def add_signature_watermark_tiled(
    img: Image.Image,
    signature_rgba: Image.Image,
    angle_deg: float = 30.0,
    spacing_rel: float = 0.3,
    scale_mul: float = 1.0,
) -> Image.Image:
    """Tile a logo PNG across the whole image with rotation using OpenCV."""
    base = _pil_to_bgra(img)
    h, w = base.shape[:2]
    sig = signature_rgba.convert('RGBA')
    sig_bgra = _pil_to_bgra(sig)

    target_w = max(64, int(w * 0.15))
    target_w = int(target_w * max(0.5, min(2.0, scale_mul or 1.0)))
    scale = target_w / sig_bgra.shape[1]
    target_h = int(sig_bgra.shape[0] * scale)
    unit = cv2.resize(sig_bgra, (max(1, target_w), max(1, target_h)), interpolation=cv2.INTER_LANCZOS4)

    # Shadow for unit
    try:
        shadow = unit.copy()
        shadow[:,:,:3] = 0
        shadow[:,:,3] = (shadow[:,:,3].astype(np.float32) * (140.0/255.0)).astype(np.uint8)
        unit_with_shadow = np.zeros_like(unit)
        a_s = (shadow[:,:,3:4].astype(np.float32)/255.0)
        unit_with_shadow[:,:,:3] = (shadow[:,:,:3].astype(np.float32)*a_s + unit[:,:,:3].astype(np.float32)*(1-a_s)).astype(np.uint8)
        unit_with_shadow[:,:,3] = np.clip((a_s + unit[:,:,3:4].astype(np.float32)/255.0*(1-a_s))*255.0,0,255).astype(np.uint8).squeeze()
        unit = unit_with_shadow
    except Exception:
        pass

    gap = max(8, int(min(unit.shape[0], unit.shape[1]) * max(0.05, min(1.0, spacing_rel or 0.3))))
    step_x = unit.shape[1] + gap
    step_y = unit.shape[0] + gap

    big = np.zeros((h*3, w*3, 4), dtype=np.uint8)
    for y0 in range(0, big.shape[0], step_y):
        for x0 in range(0, big.shape[1], step_x):
            uy, ux = unit.shape[0], unit.shape[1]
            if y0+uy <= big.shape[0] and x0+ux <= big.shape[1]:
                roi = big[y0:y0+uy, x0:x0+ux]
                a_u = (unit[:,:,3:4].astype(np.float32)/255.0)
                roi[:,:,:3] = (unit[:,:,:3].astype(np.float32)*a_u + roi[:,:,:3].astype(np.float32)*(1-a_u)).astype(np.uint8)
                roi[:,:,3] = np.clip((a_u + roi[:,:,3:4].astype(np.float32)/255.0*(1-a_u))*255.0, 0, 255).astype(np.uint8).squeeze()

    angle = float(angle_deg or 0.0)
    M = cv2.getRotationMatrix2D((big.shape[1]/2, big.shape[0]/2), angle, 1.0)
    rotated = cv2.warpAffine(big, M, (big.shape[1], big.shape[0]), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_TRANSPARENT)
    cx = (rotated.shape[1] - w)//2
    cy = (rotated.shape[0] - h)//2
    overlay = rotated[cy:cy+h, cx:cx+w]

    out = base.copy()
    a_ov = (overlay[:,:,3:4].astype(np.float32)/255.0)
    out[:,:,:3] = (overlay[:,:,:3].astype(np.float32)*a_ov + out[:,:,:3].astype(np.float32)*(1-a_ov)).astype(np.uint8)
    out[:,:,3] = np.clip((a_ov + out[:,:,3:4].astype(np.float32)/255.0*(1-a_ov))*255.0, 0, 255).astype(np.uint8).squeeze()
    return _bgra_to_pil(out)