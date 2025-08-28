from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse
from typing import Optional
import io
import os

from PIL import Image

# Lazy import heavy deps inside handler to avoid startup cost if not used
pipe = None

def get_pipe():
    global pipe
    if pipe is not None:
        return pipe
    import torch
    from diffusers import StableDiffusionInstructPix2PixPipeline, EulerAncestralDiscreteScheduler
    dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    pipe = StableDiffusionInstructPix2PixPipeline.from_pretrained(
        "timbrooks/instruct-pix2pix", torch_dtype=dtype, safety_checker=None
    )
    device = "cuda" if torch.cuda.is_available() else ("mps" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "cpu")
    pipe.to(device)
    pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(pipe.scheduler.config)
    return pipe

router = APIRouter(prefix="/api/instruct", tags=["instruct"])

@router.post("/pix2pix")
async def instruct_pix2pix(
    file: UploadFile = File(...),
    prompt: str = Form(...),
    steps: int = Form(12),
    guidance: float = Form(1.0),
    destination: str = Form("r2"),
):
    try:
        raw = await file.read()
        if not raw:
            return JSONResponse(status_code=400, content={"error": "empty file"})
        img = Image.open(io.BytesIO(raw)).convert("RGB").resize((512, 512))

        p = get_pipe()
        images = p(prompt=prompt, image=img, num_inference_steps=int(steps), image_guidance_scale=float(guidance)).images
        out = images[0]

        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)

        # Reuse existing storage util if available
        try:
            from app.utils.storage import upload_bytes
            base = os.path.splitext(file.filename or "image")[0]
            key = f"retouch/instruct/{base}.png"
            url = upload_bytes(key, buf.getvalue(), content_type="image/png")
            return {"ok": True, "url": url, "key": key}
        except Exception:
            # Fallback: return as data URL (not ideal for large images)
            import base64
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return {"ok": True, "data_url": f"data:image/png;base64,{b64}"}
    except Exception as ex:
        return JSONResponse(status_code=500, content={"error": str(ex)})