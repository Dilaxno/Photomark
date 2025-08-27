from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional
import io
import os
import httpx
import json
import re
from PIL import Image, ImageEnhance, ImageFilter

from app.core.config import logger
from app.utils.storage import upload_bytes

from transformers import pipeline
_rmbg_pipe = None


def get_rmbg_pipe():
    global _rmbg_pipe
    if _rmbg_pipe is None:
        # Optional: pin a specific revision via env to avoid auto-updating code
        revision = os.environ.get("RMBG_MODEL_REVISION")  # e.g., a commit hash like "9f1c6f..."
        # Auto-select device: use CUDA if available
        try:
            import torch
            device = 0 if torch.cuda.is_available() else -1
        except Exception:
            device = -1
        _rmbg_pipe = pipeline(
            "image-segmentation",
            model="briaai/RMBG-1.4",
            trust_remote_code=True,
            revision=revision if revision else None,
            device=device,
        )
    return _rmbg_pipe

router = APIRouter(prefix="/api/retouch", tags=["retouch"])


async def fetch_bytes(url: str, timeout: float = 20.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


def composite_onto_background(fg: Image.Image, bg: Image.Image) -> Image.Image:
    # Resize background to match foreground bounds, center-crop if needed
    fg_w, fg_h = fg.size
    bg = bg.convert("RGBA")
    # Fill alpha of fg if missing
    if fg.mode != "RGBA":
        fg = fg.convert("RGBA")

    bg_ratio = bg.width / bg.height
    fg_ratio = fg_w / fg_h
    if bg_ratio > fg_ratio:
        # bg too wide -> height match, crop width
        new_h = fg_h
        new_w = int(bg_ratio * new_h)
    else:
        # bg too tall -> width match, crop height
        new_w = fg_w
        new_h = int(new_w / bg_ratio)
    bg_resized = bg.resize((new_w, new_h), Image.LANCZOS)
    # center-crop to fg size
    left = (new_w - fg_w) // 2
    top = (new_h - fg_h) // 2
    bg_cropped = bg_resized.crop((left, top, left + fg_w, top + fg_h))

    out = Image.new("RGBA", (fg_w, fg_h))
    out.paste(bg_cropped, (0, 0))
    out.alpha_composite(fg)
    return out


@router.post("/background")
async def background_replace(
    file: UploadFile = File(...),
    background_url: Optional[str] = Form(None),
    destination: str = Form("r2"),
):
    """
    Remove image background using Hugging Face briaai/RMBG-1.4 and optionally composite onto a provided background_url.
    Returns a stored PNG URL (preserving transparency if no background provided).
    """
    raw = await file.read()
    if not raw:
        return {"error": "empty file"}

    try:
        inp = Image.open(io.BytesIO(raw))

        # Run RMBG model
        pipe = get_rmbg_pipe()
        res = pipe(inp)

        from PIL import Image as _PILImage
        matte = None
        cut = None
        # Normalize possible outputs from the pipeline
        if isinstance(res, _PILImage.Image):
            if res.mode in ("RGBA", "LA"):
                cut = res.convert("RGBA")
            else:
                matte = res
        elif isinstance(res, dict):
            img_out = res.get("image") or res.get("result")
            if isinstance(img_out, _PILImage.Image) and img_out.mode in ("RGBA", "LA"):
                cut = img_out.convert("RGBA")
            else:
                m = res.get("mask") or res.get("matte") or res.get("output")
                if m is not None:
                    if isinstance(m, _PILImage.Image):
                        matte = m
                    else:
                        # try numpy array
                        try:
                            import numpy as _np
                            matte = _PILImage.fromarray(m.astype("uint8"))
                        except Exception:
                            matte = None
        elif isinstance(res, list) and len(res) > 0:
            item = res[0]
            if isinstance(item, _PILImage.Image):
                if item.mode in ("RGBA", "LA"):
                    cut = item.convert("RGBA")
                else:
                    matte = item
            elif isinstance(item, dict):
                img_out = item.get("image") or item.get("result")
                if isinstance(img_out, _PILImage.Image) and img_out.mode in ("RGBA", "LA"):
                    cut = img_out.convert("RGBA")
                else:
                    m = item.get("mask") or item.get("matte") or item.get("output")
                    if isinstance(m, _PILImage.Image):
                        matte = m

        if cut is None:
            if matte is None:
                raise RuntimeError("Background remover returned unexpected output format")
            if matte.mode != "L":
                matte = matte.convert("L")
            # Apply alpha matte to original
            fg = inp.convert("RGBA")
            fg.putalpha(matte)
            cut = fg

        # Composite onto provided background if requested
        if background_url:
            try:
                bg_bytes = await fetch_bytes(background_url)
                bg_img = Image.open(io.BytesIO(bg_bytes))
                out = composite_onto_background(cut, bg_img)
            except Exception as ex:
                logger.exception(f"Background fetch/composite failed: {ex}")
                return {"error": f"Background fetch/composite failed: {ex}"}
        else:
            out = cut  # keep transparency

        # Encode PNG (to preserve transparency)
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)

        # Store
        base = os.path.splitext(file.filename or "image")[0]
        key = f"retouch/ai-bg/{base}.png"
        url = upload_bytes(key, buf.getvalue(), content_type="image/png")
        return {"ok": True, "url": url, "key": key}
    except Exception as ex:
        logger.exception(f"AI background replace failed: {ex}")
        return {"error": str(ex)}





def parse_ai_recommendations(ai_response: str) -> dict:
    """
    Parse AI recommendations text and extract adjustment values.
    Returns a dictionary with standardized adjustment parameters.
    """
    adjustments = {
        "brightness": 0,  # -100 to 100
        "contrast": 0,    # -100 to 100
        "saturation": 0,  # -100 to 100
        "vibrance": 0,    # -100 to 100
        "highlights": 0,  # -100 to 100
        "shadows": 0,     # -100 to 100
        "temperature": 0, # -100 to 100 (warm/cool)
        "tint": 0,        # -100 to 100 (magenta/green)
        "sharpness": 0,   # -100 to 100
        "clarity": 0,     # -100 to 100
        "vignette": 0     # -100 to 100
    }
    
    # Convert to lowercase for easier parsing
    text = ai_response.lower()
    
    # Enhanced patterns to extract numerical values from the structured prompt format
    patterns = {
        "brightness": [
            r"exposure.*?shift.*?([+-]?\d+(?:\.\d+)?)\s*(?:ev|stops?)",
            r"brightness.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|ev|stops?)",
            r"exposure.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|ev|stops?)",
            r"brighten.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|ev|stops?)",
            r"midtones.*?lift.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|ev|stops?)"
        ],
        "contrast": [
            r"contrast.*?factor.*?([01]?\.\d+)",
            r"contrast.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?",
            r"increase contrast.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?",
            r"micro-contrast.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?"
        ],
        "saturation": [
            r"saturation.*?scaling.*?([01]?\.\d+)",
            r"saturation.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?",
            r"saturate.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?"
        ],
        "vibrance": [
            r"vibrance.*?scaling.*?([01]?\.\d+)",
            r"vibrance.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?"
        ],
        "highlights": [
            r"highlights?.*?recovery.*?([+-]?\d+(?:\.\d+)?)\s*%",
            r"highlights?.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|stops?)",
            r"recover highlights?.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|stops?)",
            r"highlights?.*?reduce.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|stops?)"
        ],
        "shadows": [
            r"shadows?.*?boost.*?([+-]?\d+(?:\.\d+)?)\s*%",
            r"shadows?.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|stops?)",
            r"lift shadows?.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|stops?)",
            r"shadows?.*?deepen.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|stops?)"
        ],
        "temperature": [
            r"temperature.*?([+-]?\d+(?:\.\d+)?)\s*(?:k|kelvin|mireds?)",
            r"white balance.*?([+-]?\d+(?:\.\d+)?)\s*(?:k|kelvin|mireds?)",
            r"warm(?:er)?.*?([+-]?\d+(?:\.\d+)?)\s*(?:k|kelvin|mireds?)",
            r"cool(?:er)?.*?([+-]?\d+(?:\.\d+)?)\s*(?:k|kelvin|mireds?)"
        ],
        "tint": [
            r"tint.*?([+-]?\d+(?:\.\d+)?)",
            r"magenta.*?([+-]?\d+(?:\.\d+)?)",
            r"green.*?([+-]?\d+(?:\.\d+)?)"
        ],
        "sharpness": [
            r"sharpening.*?amount.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?",
            r"sharp(?:en|ness).*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?",
            r"detail.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?"
        ],
        "clarity": [
            r"clarity.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?",
            r"micro-contrast.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?",
            r"structure.*?([+-]?\d+(?:\.\d+)?)\s*(?:%|factor)?"
        ]
    }
    
    # Extract values using patterns
    for param, param_patterns in patterns.items():
        for pattern in param_patterns:
            matches = re.findall(pattern, text)
            if matches:
                try:
                    value = float(matches[0])
                    
                    # Handle different value formats
                    if param == "contrast" and "factor" in pattern:
                        # Convert contrast factor (0.8-1.5) to percentage (-20 to +50)
                        value = (value - 1.0) * 100
                    elif param in ["saturation", "vibrance"] and "scaling" in pattern:
                        # Convert scaling factor (0.8-1.3) to percentage (-20 to +30)
                        value = (value - 1.0) * 100
                    elif param == "temperature" and ("k" in text or "kelvin" in text):
                        # Convert Kelvin values to percentage scale
                        # Typical range: -500K to +500K -> -50% to +50%
                        value = max(-50, min(50, value / 10))
                    elif param in ["highlights", "shadows"] and "%" in pattern:
                        # Values are already in percentage, just clamp
                        pass
                    elif "ev" in pattern or "stops" in pattern:
                        # Convert EV stops to percentage (1 stop ≈ 100% brightness change)
                        value = value * 50  # More conservative conversion
                    
                    # Clamp values to reasonable ranges
                    adjustments[param] = max(-100, min(100, value))
                    break
                except (ValueError, IndexError):
                    continue
    
    # Handle qualitative descriptions
    if "much brighter" in text or "significantly brighter" in text:
        adjustments["brightness"] = max(adjustments["brightness"], 30)
    elif "brighter" in text and adjustments["brightness"] == 0:
        adjustments["brightness"] = 15
    elif "darker" in text and adjustments["brightness"] == 0:
        adjustments["brightness"] = -15
    
    if "much more contrast" in text or "significantly more contrast" in text:
        adjustments["contrast"] = max(adjustments["contrast"], 25)
    elif "more contrast" in text and adjustments["contrast"] == 0:
        adjustments["contrast"] = 15
    elif "less contrast" in text and adjustments["contrast"] == 0:
        adjustments["contrast"] = -15
    
    if "more saturated" in text and adjustments["saturation"] == 0:
        adjustments["saturation"] = 20
    elif "less saturated" in text or "desaturated" in text and adjustments["saturation"] == 0:
        adjustments["saturation"] = -20
    
    if "warmer" in text and adjustments["temperature"] == 0:
        adjustments["temperature"] = 15
    elif "cooler" in text and adjustments["temperature"] == 0:
        adjustments["temperature"] = -15
    
    return adjustments


def apply_image_adjustments(img: Image.Image, adjustments: dict) -> Image.Image:
    """
    Apply adjustments to the image using PIL + OpenCV for advanced operations.
    """
    # Ensure RGB
    if img.mode != 'RGB':
        img = img.convert('RGB')

    # PIL-based global adjustments first (brightness/contrast/saturation/sharpness)
    if adjustments.get("brightness", 0) != 0:
        factor = 1.0 + (adjustments["brightness"] / 100.0)
        img = ImageEnhance.Brightness(img).enhance(factor)

    if adjustments.get("contrast", 0) != 0:
        factor = 1.0 + (adjustments["contrast"] / 100.0)
        img = ImageEnhance.Contrast(img).enhance(factor)

    if adjustments.get("saturation", 0) != 0:
        factor = 1.0 + (adjustments["saturation"] / 100.0)
        img = ImageEnhance.Color(img).enhance(factor)

    if adjustments.get("sharpness", 0) != 0:
        if adjustments["sharpness"] > 0:
            factor = 1.0 + (adjustments["sharpness"] / 100.0)
            img = ImageEnhance.Sharpness(img).enhance(factor)
        else:
            blur_amount = abs(adjustments["sharpness"]) / 20.0
            img = img.filter(ImageFilter.GaussianBlur(radius=blur_amount))

    # Convert to OpenCV (numpy BGR) for advanced local edits
    import numpy as np
    import cv2

    img_np = np.array(img)  # RGB
    h, w = img_np.shape[:2]
    bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

    # Temperature: shift along blue-red axis
    temp = adjustments.get("temperature", 0) / 100.0  # -1..1
    if abs(temp) > 1e-3:
        # Positive temp -> warmer (increase R, decrease B), Negative -> cooler
        r_scale = 1.0 + 0.15 * max(0.0, temp)
        b_scale = 1.0 + 0.15 * max(0.0, -temp)
        # For cooling, invert: boost B, reduce R
        r_scale = r_scale if temp >= 0 else 1.0 - 0.15 * (-temp)
        b_scale = b_scale if temp <= 0 else 1.0 - 0.15 * (temp)
        # Apply scaling with clipping
        B, G, R = cv2.split(bgr)
        R = np.clip(R.astype(np.float32) * r_scale, 0, 255).astype(np.uint8)
        B = np.clip(B.astype(np.float32) * b_scale, 0, 255).astype(np.uint8)
        bgr = cv2.merge([B, G, R])

    # Shadows/Highlights: simple tone curve in LAB
    sh = adjustments.get("shadows", 0) / 100.0  # -1..1 (negative: deepen, positive: lift)
    hi = adjustments.get("highlights", 0) / 100.0  # -1..1 (negative: recover, positive: boost)
    if abs(sh) > 1e-3 or abs(hi) > 1e-3:
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        L, A, Bc = cv2.split(lab)
        Lf = L.astype(np.float32) / 255.0
        # Apply two piecewise curves
        # Shadows: affect L < 0.5 more strongly
        shadow_mask = (Lf < 0.5).astype(np.float32)
        # Highlights: affect L > 0.5 more strongly
        highlight_mask = (Lf >= 0.5).astype(np.float32)
        # Lift/deepen shadows
        Lf = Lf + sh * shadow_mask * (0.5 - Lf) * 2.0
        # Recover/boost highlights (negative pulls toward mid)
        Lf = Lf + hi * highlight_mask * (1.0 - Lf) * 2.0
        Lf = np.clip(Lf, 0.0, 1.0)
        L2 = (Lf * 255.0).astype(np.uint8)
        lab2 = cv2.merge([L2, A, Bc])
        bgr = cv2.cvtColor(lab2, cv2.COLOR_LAB2BGR)

    # Vignette: radial darkening/brightening
    vig = adjustments.get("vignette", 0) / 100.0  # -1..1 (positive: darken edges, negative: lighten)
    if abs(vig) > 1e-3:
        yy, xx = np.mgrid[0:h, 0:w]
        cx, cy = w / 2.0, h / 2.0
        # Normalized radius 0..1
        r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
        r = np.clip(r, 0.0, 1.0)
        # Create mask: 1 at center, decreases to 0 at edges
        mask = 1.0 - r
        # Strength curve
        strength = 0.8 * abs(vig)
        if vig > 0:  # darken edges
            gain = (0.5 + 0.5 * mask)  # center ~1, edges ~0.5
            gain = 1.0 - strength * (1.0 - gain)
        else:  # lighten edges
            gain = (0.5 + 0.5 * mask)
            gain = 1.0 + strength * (1.0 - gain)
        gain = gain[..., None].astype(np.float32)
        bgr = np.clip(bgr.astype(np.float32) * gain, 0, 255).astype(np.uint8)

    # Convert back to PIL
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb)

    return img


# Note: AI retouch endpoints removed intentionally.
