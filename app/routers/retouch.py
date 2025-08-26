from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional
import io
import os
import httpx
import json
import re
from PIL import Image, ImageEnhance, ImageFilter

from app.core.config import logger, GROQ_API_KEY
from app.utils.storage import upload_bytes

try:
    from rembg import remove
    REMBG_AVAILABLE = True
except Exception as ex:
    logger.warning(f"rembg not available: {ex}")
    REMBG_AVAILABLE = False

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
    Analyze image metrics and get AI recommendations using Groq API.
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

        # Extract image metrics
        img = Image.open(io.BytesIO(raw))
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Get image data for analysis
        width, height = img.size
        pixels = list(img.getdata())
        
        # Calculate metrics
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
        
        # Calculate contrast and saturation
        contrast = (max_r + max_g + max_b) / 3 - (min_r + min_g + min_b) / 3
        saturation = ((avg_r - avg_brightness) ** 2 + (avg_g - avg_brightness) ** 2 + (avg_b - avg_brightness) ** 2) ** 0.5 / avg_brightness if avg_brightness > 0 else 0
        
        # Determine dominant color
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
            "saturation": f"{saturation:.2f}",
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

        # Create system prompt for AI analysis
        system_prompt = f"""You are a professional photo retoucher and colorist with expertise in exposure correction, color grading, and natural-looking image enhancement.

Analyze the following image metrics and the user's creative vision. Then provide precise, actionable retouching recommendations expressed as numeric adjustments (with ranges when appropriate) and professional reasoning.

Image Analysis:
- Dimensions: {metrics['dimensions']['width']}x{metrics['dimensions']['height']} ({metrics['aspectRatio']} aspect ratio)
- Average Colors: R:{metrics['averageColors']['red']}, G:{metrics['averageColors']['green']}, B:{metrics['averageColors']['blue']}
- Overall Brightness: {metrics['brightness']}/255 ({'dark' if metrics['brightness'] < 85 else 'bright' if metrics['brightness'] > 170 else 'balanced'})
- Contrast Level: {metrics['contrast']}/255 ({'low' if metrics['contrast'] < 50 else 'high' if metrics['contrast'] > 150 else 'moderate'})
- Saturation: {metrics['saturation']} ({'low' if float(metrics['saturation']) < 0.3 else 'high' if float(metrics['saturation']) > 0.7 else 'moderate'})
- Dominant Color Tone: {metrics['dominantColor']}
- Exposure Distribution: {metrics['exposure']['darkPixels']}% shadows, {metrics['exposure']['midtones']}% midtones, {metrics['exposure']['brightPixels']}% highlights
- Color Range: Red({metrics['colorRange']['red']['range']}), Green({metrics['colorRange']['green']['range']}), Blue({metrics['colorRange']['blue']['range']})

User's Creative Vision:
"{user_prompt}"

Provide **detailed recommendations** for the following categories:

1. **Brightness/Exposure**
   - Suggest exposure shift in stops (EV) or percentage (+/- 0.1 to 2.0 EV).
   - Indicate whether midtones should be lifted, highlights reduced, or shadows deepened.

2. **Contrast**
   - Recommend a contrast factor (0.8–1.5, where 1.0 = no change).
   - Specify whether adjustments should be global or targeted (micro-contrast, clarity).

3. **Color Correction**
   - Recommend white balance shift (temperature in Kelvin or mireds, tint +/- 50).
   - Suggest saturation/vibrance scaling (0.8–1.3 typical).
   - Point out dominant color casts that should be neutralized.

4. **Highlights & Shadows**
   - Suggest recovery or boost percentages for highlights/shadows (-50% to +50%).
   - Indicate if local adjustments (sky, skin tones, background) are needed.

5. **Other Refinements**
   - Sharpening (radius & amount), noise reduction (luma vs chroma).
   - Cropping (aspect ratio & composition guidance).
   - Optional stylistic grading (e.g., cinematic teal/orange, matte finish, warm sunset tone).

**Important:**
- Always give numeric values (with recommended ranges) instead of vague advice.
- Explain reasoning: why each adjustment moves the image closer to the user's vision.
- Ensure results remain **natural and realistic**, unless the user explicitly requests a stylized/artistic look."""

        # Call Groq API
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
                        {"role": "user", "content": f"Please analyze this image and provide retouching recommendations to achieve: {user_prompt}"}
                    ],
                    "temperature": 0.7,
                    "max_tokens": 1000,
                }
            )
            
            if not response.is_success:
                logger.error(f"Groq API error: {response.status_code} - {response.text}")
                raise HTTPException(status_code=500, detail=f"AI analysis failed: {response.status_code}")
            
            data = response.json()
            ai_response = data.get("choices", [{}])[0].get("message", {}).get("content", "No recommendations available.")

        return {
            "ok": True,
            "metrics": metrics,
            "ai_response": ai_response
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
        "clarity": 0      # -100 to 100
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
    Apply the parsed adjustments to the PIL Image.
    """
    # Convert to RGB if needed
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    # Apply brightness
    if adjustments["brightness"] != 0:
        factor = 1.0 + (adjustments["brightness"] / 100.0)
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(factor)
    
    # Apply contrast
    if adjustments["contrast"] != 0:
        factor = 1.0 + (adjustments["contrast"] / 100.0)
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(factor)
    
    # Apply saturation
    if adjustments["saturation"] != 0:
        factor = 1.0 + (adjustments["saturation"] / 100.0)
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(factor)
    
    # Apply sharpness
    if adjustments["sharpness"] != 0:
        if adjustments["sharpness"] > 0:
            factor = 1.0 + (adjustments["sharpness"] / 100.0)
            enhancer = ImageEnhance.Sharpness(img)
            img = enhancer.enhance(factor)
        else:
            # For negative sharpness, apply blur
            blur_amount = abs(adjustments["sharpness"]) / 20.0
            img = img.filter(ImageFilter.GaussianBlur(radius=blur_amount))
    
    # Note: PIL doesn't have direct support for highlights/shadows, temperature/tint, vibrance, clarity
    # These would require more advanced image processing libraries like OpenCV or custom algorithms
    # For now, we'll log these values for future implementation
    
    advanced_adjustments = {
        "highlights": adjustments["highlights"],
        "shadows": adjustments["shadows"],
        "temperature": adjustments["temperature"],
        "tint": adjustments["tint"],
        "vibrance": adjustments["vibrance"],
        "clarity": adjustments["clarity"]
    }
    
    if any(v != 0 for v in advanced_adjustments.values()):
        logger.info(f"Advanced adjustments not yet implemented: {advanced_adjustments}")
    
    return img


@router.post("/apply")
async def apply_recommendations(
    file: UploadFile = File(...),
    ai_response: str = Form(...),
):
    """
    Parse AI recommendations and apply them to the image using image processing.
    Returns the processed image URL.
    """
    if not ai_response.strip():
        raise HTTPException(status_code=400, detail="AI response is required")

    try:
        # Read the image
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Empty file")

        img = Image.open(io.BytesIO(raw))
        
        # Parse AI recommendations into structured adjustments
        adjustments = parse_ai_recommendations(ai_response)
        logger.info(f"Parsed adjustments: {adjustments}")
        
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
            "adjustments_applied": adjustments
        }

    except HTTPException:
        raise
    except Exception as ex:
        logger.exception(f"Apply recommendations failed: {ex}")
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(ex)}")
