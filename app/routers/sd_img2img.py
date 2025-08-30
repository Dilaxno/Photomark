from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse
from typing import Optional
import io
import os
import uuid

from PIL import Image

# Cached Diffusers pipeline
_pipe = None


def _get_pipe():
    """Create and cache a Stable Diffusion img2img pipeline matching the provided snippet.
    Model: runwayml/stable-diffusion-v1-5
    Prefers CUDA + float16 if available; falls back to CPU + float32.
    """
    global _pipe
    if _pipe is not None:
        return _pipe

    import torch
    from diffusers import StableDiffusionImg2ImgPipeline

    # Device/dtype selection consistent with the snippet's intent
    if torch.cuda.is_available():
        dtype = torch.float16
        device = "cuda"
    else:
        dtype = torch.float32
        device = "cpu"

    # Honor optional Hugging Face cache and token envs
    cache_dir = (
        os.getenv("HF_CACHE_DIR")
        or os.getenv("HUGGINGFACE_HUB_CACHE")
        or os.getenv("HF_HOME")
    )
    cache_kwargs = {"cache_dir": cache_dir} if cache_dir else {}

    hf_token = (
        os.getenv("HUGGING_FACE_API_TOKEN")
        or os.getenv("HUGGINGFACE_HUB_TOKEN")
        or os.getenv("HF_TOKEN")
    )
    if hf_token:
        cache_kwargs["token"] = hf_token

    pipe = StableDiffusionImg2ImgPipeline.from_pretrained(
        "runwayml/stable-diffusion-v1-5",
        torch_dtype=dtype,
        **cache_kwargs,
    ).to(device)

    # Light memory optimization (safe no-op if unsupported)
    try:
        pipe.enable_attention_slicing()
    except Exception:
        pass

    _pipe = pipe
    return _pipe


router = APIRouter(prefix="/api/retouch", tags=["retouch"])


@router.post("/img2img")
async def sd_img2img(
    file: UploadFile = File(...),
    prompt: str = Form(...),
    strength: float = Form(0.8),
    guidance_scale: float = Form(7.5),
    destination: str = Form("r2"),
):
    """Run Stable Diffusion img2img using the exact model and call pattern from the snippet.
    Returns a JSON with a public URL when available, or a data URL fallback.
    """
    try:
        raw = await file.read()
        if not raw:
            return JSONResponse(status_code=400, content={"error": "empty file"})

        init_image = Image.open(io.BytesIO(raw)).convert("RGB")

        pipe = _get_pipe()
        # Match the snippet call signature
        result = pipe(
            prompt=prompt,
            image=init_image,
            strength=float(strength),
            guidance_scale=float(guidance_scale),
        )
        out = result.images[0]

        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)

        # Try to upload to object storage for a clean URL
        try:
            from app.utils.storage import upload_bytes

            base = os.path.splitext(file.filename or uuid.uuid4().hex[:12])[0]
            key = f"retouch/img2img/{base}.png"
            url = upload_bytes(key, buf.getvalue(), content_type="image/png")
            return {"ok": True, "url": url, "key": key}
        except Exception:
            # Fallback to data URL if storage isn't configured
            import base64

            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return {"ok": True, "data_url": f"data:image/png;base64,{b64}"}

    except Exception as ex:
        return JSONResponse(status_code=500, content={"error": str(ex)})