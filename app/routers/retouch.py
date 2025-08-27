from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional
import io
import os
import httpx
import json
import re
from PIL import Image, ImageEnhance, ImageFilter
import asyncio

from app.core.config import logger, GROQ_API_KEY, HUGGINGFACE_API_TOKEN
from app.utils.storage import upload_bytes

try:
    from rembg import remove
    REMBG_AVAILABLE = True
except Exception as ex:
    logger.warning(f"rembg not available: {ex}")
    REMBG_AVAILABLE = False

router = APIRouter(prefix="/api/retouch", tags=["retouch"]) 

HF_PIX2PIX_URL = "https://api-inference.huggingface.co/models/timbrooks/instruct-pix2pix"


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
    Remove image background using rembg and optionally composite onto a provided background_url.
    Returns a stored PNG URL (preserving transparency if no background provided).
    """
    if not REMBG_AVAILABLE:
        return {"error": "rembg not installed on server"}

    raw = await file.read()
    if not raw:
        return {"error": "empty file"}

    try:
        inp = Image.open(io.BytesIO(raw))
        # rembg returns PIL Image if input is PIL Image
        cut = remove(inp)

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


@router.post("/analyze")
async def ai_analyze(
    file: UploadFile = File(...),
    user_prompt: str = Form(...),
):
    """
    Analyze image metrics (basic + advanced) and get machine-readable AI adjustments via Groq API.
    """
    if not GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="Groq API key not configured")

    if not user_prompt.strip():
        raise HTTPException(status_code=400, detail="User prompt is required")

    try:
        # Read and analyze the image
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file")

        # Extract image metrics (basic)
        img = Image.open(io.BytesIO(raw))
        if img.mode != 'RGB':
            img = img.convert('RGB')

        width, height = img.size
        pixels = list(img.getdata())

        total_r = total_g = total_b = 0
        min_r = min_g = min_b = 255
        max_r = max_g = max_b = 0
        total_brightness = 0
        dark_pixels = bright_pixels = 0

        for r, g, b in pixels:
            total_r += r
            total_g += g
            total_b += b

            min_r = min(min_r, r)
            max_r = max(max_r, r)
            min_g = min(min_g, g)
            max_g = max(max_g, g)
            min_b = min(min_b, b)
            max_b = max(max_b, b)

            brightness = (r + g + b) / 3
            total_brightness += brightness

            if brightness < 85:
                dark_pixels += 1
            if brightness > 170:
                bright_pixels += 1

        pixel_count = len(pixels)
        avg_r = total_r / pixel_count
        avg_g = total_g / pixel_count
        avg_b = total_b / pixel_count
        avg_brightness = total_brightness / pixel_count

        contrast = (max_r + max_g + max_b) / 3 - (min_r + min_g + min_b) / 3
        saturation_basic = ((avg_r - avg_brightness) ** 2 + (avg_g - avg_brightness) ** 2 + (avg_b - avg_brightness) ** 2) ** 0.5 / avg_brightness if avg_brightness > 0 else 0

        dominant_color = 'neutral'
        if avg_r > avg_g and avg_r > avg_b:
            dominant_color = 'warm/red'
        elif avg_g > avg_r and avg_g > avg_b:
            dominant_color = 'green'
        elif avg_b > avg_r and avg_b > avg_g:
            dominant_color = 'cool/blue'

        metrics = {
            "dimensions": {"width": width, "height": height},
            "aspectRatio": f"{width / height:.2f}",
            "averageColors": {"red": round(avg_r), "green": round(avg_g), "blue": round(avg_b)},
            "brightness": round(avg_brightness),
            "contrast": round(contrast),
            "saturation": f"{saturation_basic:.2f}",
            "dominantColor": dominant_color,
            "exposure": {
                "darkPixels": round((dark_pixels / pixel_count) * 100),
                "brightPixels": round((bright_pixels / pixel_count) * 100),
                "midtones": round(((pixel_count - dark_pixels - bright_pixels) / pixel_count) * 100)
            },
            "colorRange": {
                "red": {"min": min_r, "max": max_r, "range": max_r - min_r},
                "green": {"min": min_g, "max": max_g, "range": max_g - min_g},
                "blue": {"min": min_b, "max": max_b, "range": max_b - min_b}
            }
        }

        # Advanced metrics using OpenCV and scikit-image
        import numpy as np
        import cv2
        from skimage import color as skcolor, restoration as skrestoration

        img_np = np.array(img)  # RGB uint8
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        gray_f = gray.astype(np.float32) / 255.0

        # Brightness and contrast (normalized 0..1 for prompt readability)
        brightness_norm = float(gray_f.mean())
        contrast_norm = float(gray_f.std())
        contrast_norm_clamped = float(max(0.0, min(1.0, contrast_norm / 0.25)))  # heuristic scale

        # Saturation via HSV
        hsv = cv2.cvtColor(img_np, cv2.COLOR_RGB2HSV)
        sat_norm = float(hsv[..., 1].mean() / 255.0)

        # Sharpness via variance of Laplacian
        lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        sharpness_norm = float(max(0.0, min(1.0, lap_var / 1000.0)))  # heuristic scale

        # White balance / temperature bias (simple gray-world proxy)
        avg_rgb = img_np.reshape(-1, 3).mean(axis=0)
        temperature_bias = float((avg_rgb[0] - avg_rgb[2]) / (avg_rgb[0] + avg_rgb[2] + 1e-6))  # +warm, -cool

        # Skin tone detection (YCrCb thresholds)
        ycrcb = cv2.cvtColor(img_np, cv2.COLOR_RGB2YCrCb)
        Y, Cr, Cb = cv2.split(ycrcb)
        skin_mask = cv2.inRange(ycrcb, (0, 133, 77), (255, 173, 127))  # common thresholds
        skin_ratio = float(skin_mask.mean() / 255.0)  # 0..1
        skin_pct = skin_ratio * 100.0
        mean_skin_hue = None
        if skin_ratio > 0:
            skin_hsv = hsv[skin_mask > 0]
            if skin_hsv.size > 0:
                mean_skin_hue = float(np.mean(skin_hsv[:, 0]) / 179.0)  # 0..1

        # Noise level estimation on grayscale
        try:
            sigma = float(skrestoration.estimate_sigma(gray_f, channel_axis=None))
        except Exception:
            sigma = 0.0
        noise_norm = float(max(0.0, min(1.0, sigma / 0.1)))  # heuristic scale

        # Histogram stats & clipping
        hist_stats = {}
        clipped_low = clipped_high = 0
        total_px = gray.size
        for i, ch_name in enumerate(["red", "green", "blue"]):
            ch = img_np[..., i]
            p1, p99 = np.percentile(ch, [1, 99]).tolist()
            hist_stats[ch_name] = {
                "mean": float(ch.mean()),
                "std": float(ch.std()),
                "p01": float(p1),
                "p99": float(p99),
            }
            clipped_low += int((ch <= 2).sum())
            clipped_high += int((ch >= 253).sum())
        clip_shadows_pct = float((clipped_low / (total_px * 3)) * 100.0)
        clip_highlights_pct = float((clipped_high / (total_px * 3)) * 100.0)

        advanced_metrics = {
            "histogram": hist_stats,
            "sharpness": {"laplacianVar": lap_var, "score": sharpness_norm},
            "whiteBalance": {"avgRGB": [float(x) for x in avg_rgb.tolist()], "temperatureBias": temperature_bias},
            "skin": {"coveragePct": skin_pct, "meanHue01": mean_skin_hue},
            "noise": {"sigma": sigma, "score": noise_norm},
            "dynamicRange": {"clippedShadowsPct": clip_shadows_pct, "clippedHighlightsPct": clip_highlights_pct},
            "saturation01": sat_norm,
            "brightness01": brightness_norm,
            "contrast01": contrast_norm,
        }

        # Optional CLIP analysis for style labels
        clip_labels = [
            "cinematic", "moody", "vibrant", "natural", "warm", "cool",
            "high contrast", "low contrast", "desaturated", "soft",
            "portrait", "landscape", "studio lighting", "backlit",
        ]
        try:
            from app.utils.clip_utils import best_clip_label, clip_image_text_scores
            best_label, best_score = best_clip_label(img, clip_labels)
            clip_scores = clip_image_text_scores(img, clip_labels)
            advanced_metrics["clip"] = {
                "bestLabel": best_label,
                "bestScore": best_score,
                "labels": clip_labels,
                "scores": clip_scores,
            }
        except Exception as _ex:
            # Keep endpoint robust if CLIP isn't available
            advanced_metrics["clip"] = {"error": str(_ex)}

        # Compose concise analysis bullets for the prompt
        def label_level(val: float, low=0.33, high=0.66):
            return "low" if val < low else ("high" if val > high else "moderate")

        bullets = [
            f"- Brightness: {brightness_norm:.2f} ({'dark' if brightness_norm < 0.4 else 'bright' if brightness_norm > 0.6 else 'balanced'})",
            f"- Contrast: {contrast_norm_clamped:.2f} ({label_level(contrast_norm_clamped)})",
            f"- Saturation: {sat_norm:.2f} ({label_level(sat_norm)})",
            f"- Sharpness: {sharpness_norm:.2f} ({label_level(sharpness_norm)})",
            f"- White balance bias (R vs B): {temperature_bias:.2f}",
            f"- Skin coverage: {skin_pct:.1f}%",
            f"- Noise level: {noise_norm:.2f}",
            f"- Dynamic range clipping: shadows {clip_shadows_pct:.1f}%, highlights {clip_highlights_pct:.1f}%",
            f"- CLIP best label: {advanced_metrics.get('clip', {}).get('bestLabel', 'n/a')} ({advanced_metrics.get('clip', {}).get('bestScore', 0):.2f})",
        ]

        # Overwrite system prompt to force machine-readable output per spec
        system_prompt = (
            "You are a professional photo retoucher.\n\n"
            + "The following image analysis has been extracted:\n"
            + "\n".join(bullets)
            + f"\n\nThe user wants: \"{user_prompt}\"\n\n"
            + "Return only structured adjustments in JSON with values from -1.0 to +1.0 for:\n"
            + "[brightness, contrast, saturation, sharpness, temperature, vignette, shadows, highlights]\n"
            + "Example output:\n"
            + "{\n"
            + "  \"brightness\": 0.1,\n"
            + "  \"contrast\": 0.4,\n"
            + "  \"saturation\": -0.2,\n"
            + "  \"sharpness\": 0.0,\n"
            + "  \"temperature\": -0.1,\n"
            + "  \"vignette\": 0.0,\n"
            + "  \"shadows\": 0.3,\n"
            + "  \"highlights\": -0.2\n"
            + "}\n"
            + "Optional: Use a vision model"
        )

        # Call Groq API for structured adjustments
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": "Return JSON only with the specified keys and ranges."},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 350,
                },
            )

            if not response.is_success:
                logger.error(f"Groq API error: {response.status_code} - {response.text}")
                raise HTTPException(status_code=500, detail=f"AI analysis failed: {response.status_code}")

            data = response.json()
            ai_response = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")

        # Best-effort parse of AI JSON
        parsed = None
        try:
            parsed = json.loads(ai_response)
        except Exception:
            try:
                start = ai_response.find('{')
                end = ai_response.rfind('}')
                if start != -1 and end != -1 and end > start:
                    parsed = json.loads(ai_response[start:end+1])
            except Exception:
                parsed = None

        return {
            "ok": True,
            "metrics": metrics,
            "advancedMetrics": advanced_metrics,
            "ai_response": ai_response,
            "ai_adjustments": parsed,
        }

    except HTTPException:
        raise
    except Exception as ex:
        logger.exception(f"AI analyze failed: {ex}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(ex)}")


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


@router.post("/apply")
async def apply_recommendations(
    file: UploadFile = File(...),
    ai_response: Optional[str] = Form(None),
    ai_json: Optional[str] = Form(None),
):
    """
    Apply AI recommendations to the image.
    - Prefer JSON adjustments in [-1.0..+1.0] (ai_json).
    - Fallback to free-form text (ai_response) using parser.
    Returns the processed image URL.
    """
    if (not ai_json or not ai_json.strip()) and (not ai_response or not ai_response.strip()):
        raise HTTPException(status_code=400, detail="Provide either ai_json (preferred) or ai_response")

    try:
        # Read the image
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file")

        img = Image.open(io.BytesIO(raw))

        # Build adjustments
        adjustments = None
        used_source = None

        def to_internal(v: float) -> int:
            try:
                return int(max(-100, min(100, float(v) * 100.0)))
            except Exception:
                return 0

        if ai_json and ai_json.strip():
            try:
                payload = json.loads(ai_json)
                # Map expected keys; defaults to 0
                adjustments = {
                    "brightness": to_internal(payload.get("brightness", 0)),
                    "contrast": to_internal(payload.get("contrast", 0)),
                    "saturation": to_internal(payload.get("saturation", 0)),
                    "sharpness": to_internal(payload.get("sharpness", 0)),
                    "highlights": to_internal(payload.get("highlights", 0)),
                    "shadows": to_internal(payload.get("shadows", 0)),
                    "temperature": to_internal(payload.get("temperature", 0)),
                    "vignette": to_internal(payload.get("vignette", 0)),
                    # Unused/advanced placeholders for compatibility
                    "tint": 0,
                    "vibrance": 0,
                    "clarity": 0,
                }
                used_source = "json"
            except Exception as ex:
                logger.warning(f"Failed to parse ai_json; falling back to text parse: {ex}")

        if adjustments is None:
            # Fallback to free-form text parse
            adjustments = parse_ai_recommendations(ai_response or "")
            used_source = "text"

        logger.info(f"Adjustments ({used_source}): {adjustments}")

        # Apply adjustments to the image
        processed_img = apply_image_adjustments(img, adjustments)

        # Save processed image
        buf = io.BytesIO()
        processed_img.save(buf, format="JPEG", quality=95)
        buf.seek(0)

        # Store the processed image
        base = os.path.splitext(file.filename or "image")[0]
        key = f"retouch/processed/{base}_retouched.jpg"
        url = upload_bytes(key, buf.getvalue(), content_type="image/jpeg")

        return {
            "ok": True,
            "url": url,
            "key": key,
            "source": used_source,
            "adjustments_applied": adjustments,
        }

    except HTTPException:
        raise
    except Exception as ex:
        logger.exception(f"Apply recommendations failed: {ex}")
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(ex)}")


@router.post("/hf-apply")
async def hf_apply(
    file: UploadFile = File(...),
    instruction: str = Form(...),
):
    """
    Apply text instruction to the image using Hugging Face Inference API (instruct-pix2pix).
    Returns a processed JPG uploaded to storage.
    """
    if not HUGGINGFACE_API_TOKEN:
        raise HTTPException(status_code=500, detail="Hugging Face API token not configured")
    instruction = (instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="Instruction is required")

    try:
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file")

        # Send multipart to HF Inference API
        # 'inputs' is the text instruction, 'image' is the image bytes
        headers = {"Authorization": f"Bearer {HUGGINGFACE_API_TOKEN}"}
        form = {
            "inputs": instruction,
        }
        files = {
            "image": (file.filename or "image.jpg", raw, file.content_type or "image/jpeg"),
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(HF_PIX2PIX_URL, headers=headers, data=form, files=files)

        if resp.status_code == 503:
            # Space is starting; surface a friendly message
            try:
                payload = resp.json()
                est = payload.get("estimated_time")
                raise HTTPException(status_code=503, detail=f"Model loading, retry in ~{est or 20}s")
            except Exception:
                raise HTTPException(status_code=503, detail="Model loading, please retry shortly")

        if not resp.is_success:
            ct = resp.headers.get("content-type", "")
            detail = resp.text
            if "application/json" in ct:
                try:
                    detail = resp.json()
                except Exception:
                    pass
            logger.error(f"HF error {resp.status_code}: {detail}")
            raise HTTPException(status_code=resp.status_code, detail="Hugging Face request failed")

        # Expect image bytes back
        content_type = resp.headers.get("content-type", "application/octet-stream")
        result_bytes = resp.content
        if not result_bytes:
            raise HTTPException(status_code=500, detail="Empty response from model")

        # Ensure JPEG output
        try:
            out_img = Image.open(io.BytesIO(result_bytes))
            buf = io.BytesIO()
            out_img.convert("RGB").save(buf, format="JPEG", quality=95)
            buf.seek(0)
            out_bytes = buf.getvalue()
        except Exception:
            # If already binary jpg/png, just store it
            out_bytes = result_bytes
            content_type = content_type or "image/jpeg"

        base = os.path.splitext(file.filename or "image")[0]
        key = f"retouch/hf/{base}_pix2pix.jpg"
        url = upload_bytes(key, out_bytes, content_type="image/jpeg")

        return {"ok": True, "url": url, "key": key}
    except HTTPException:
        raise
    except Exception as ex:
        logger.exception(f"HF apply failed: {ex}")
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(ex)}")
