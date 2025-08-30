from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse
from typing import Optional
import io
import os
import uuid

from PIL import Image

# Diffusers: InstructPix2Pix model
pipe = None  # cached pipeline


def get_pipe():
    """Create and cache the InstructPix2Pix pipeline (timbrooks/instruct-pix2pix)."""
    global pipe
    if pipe is not None:
        return pipe

    import torch
    from diffusers import StableDiffusionInstructPix2PixPipeline

    # Prefer BF16/FP16 on CUDA if available; otherwise use FP32 (CPU)
    if torch.cuda.is_available():
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        device = "cuda"
    else:
        dtype = torch.float32
        device = "cpu"

    # Honor optional HF cache dirs if present
    cache_dir = (
        os.getenv("HF_CACHE_DIR")
        or os.getenv("HUGGINGFACE_HUB_CACHE")
        or os.getenv("HF_HOME")
    )
    cache_kwargs = {"cache_dir": cache_dir} if cache_dir else {}

    # Optional token (if rate-limited/private envs)
    # Support common env var names
    hf_token = (
        os.getenv("HUGGING_FACE_API_TOKEN")
        or os.getenv("HUGGINGFACE_HUB_TOKEN")
        or os.getenv("HF_TOKEN")
    )
    if hf_token:
        cache_kwargs["token"] = hf_token

    pipe = StableDiffusionInstructPix2PixPipeline.from_pretrained(
        "timbrooks/instruct-pix2pix",
        torch_dtype=dtype,
        **cache_kwargs,
    )
    pipe = pipe.to(device)

    # Light memory optimizations on small VRAM/CPU
    try:
        pipe.enable_attention_slicing()
    except Exception:
        pass

    return pipe


router = APIRouter(prefix="/api/instruct", tags=["instruct"])


@router.post("/pix2pix")
async def instruct_pix2pix(
    file: UploadFile = File(...),
    prompt: str = Form(...),
    steps: int = Form(28),
    guidance: float = Form(7.5),
    image_guidance: float = Form(1.5),
    negative_prompt: Optional[str] = Form(None),
    destination: str = Form("r2"),
):
    """Apply text-guided edits to an input image using InstructPix2Pix.
    Returns a JSON with a public URL (R2) when available, otherwise a data URL fallback.
    """
    try:
        raw = await file.read()
        if not raw:
            return JSONResponse(status_code=400, content={"error": "empty file"})

        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # InstructPix2Pix commonly uses 512x512; resize conservatively for speed/memory.
        try:
            img = img.resize((512, 512))
        except Exception:
            pass

        p = get_pipe()

        kwargs = dict(
            prompt=prompt,
            image=img,
            num_inference_steps=int(steps),
            guidance_scale=float(guidance),
            image_guidance_scale=float(image_guidance),
        )
        if negative_prompt:
            kwargs["negative_prompt"] = negative_prompt

        result = p(**kwargs)
        out = result.images[0]

        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)

        # Try R2/local static upload first for a clean URL
        try:
            from app.utils.storage import upload_bytes

            base = os.path.splitext(file.filename or uuid.uuid4().hex[:12])[0]
            key = f"retouch/instruct/{base}.png"
            url = upload_bytes(key, buf.getvalue(), content_type="image/png")
            return {"ok": True, "url": url, "key": key}
        except Exception:
            # Fallback to data URL
            import base64

            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return {"ok": True, "data_url": f"data:image/png;base64,{b64}"}

    except Exception as ex:
        return JSONResponse(status_code=500, content={"error": str(ex)})