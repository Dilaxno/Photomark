from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse
from typing import Optional
import io
import os
import uuid

from PIL import Image

# Lazy import heavy deps inside handler to avoid startup cost if not used
pipe = None

def get_pipe():
    """Create and cache the HiDream E1 image editing pipeline using remote code."""
    global pipe
    if pipe is not None:
        return pipe

    import torch
    from diffusers import DiffusionPipeline
    from transformers import AutoTokenizer, LlamaForCausalLM

    # Prefer BF16 on CUDA if supported, else FP16 on CUDA, else FP32
    if torch.cuda.is_available():
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    else:
        dtype = torch.float32

    # Use HF token from env if provided (or rely on cached CLI login)
    hf_token = os.getenv("HUGGINGFACE_HUB_TOKEN") or os.getenv("HF_TOKEN")

    # Llama 3.1-8B is required by HiDream-E1. Ensure HF auth for access.
    tokenizer_4 = AutoTokenizer.from_pretrained(
        "meta-llama/Llama-3.1-8B-Instruct",
        token=hf_token,
        use_fast=True,
    )
    text_encoder_4 = LlamaForCausalLM.from_pretrained(
        "meta-llama/Llama-3.1-8B-Instruct",
        torch_dtype=dtype if dtype in (torch.bfloat16, torch.float16) else torch.float32,
        token=hf_token,
    )

    try:
        pipe = DiffusionPipeline.from_pretrained(
            "HiDream-ai/HiDream-E1-Full",
            trust_remote_code=True,  # crucial to load HiDreamImageEditingPipeline from model repo
            tokenizer_4=tokenizer_4,
            text_encoder_4=text_encoder_4,
            torch_dtype=dtype,
        )
    except AttributeError as e:
        # Some environments resolve custom pipeline class against `diffusers` first and fail
        if "HiDreamImageEditingPipeline" in str(e):
            from huggingface_hub import snapshot_download
            local_dir = snapshot_download(
                "HiDream-ai/HiDream-E1-Full",
                token=hf_token,
                local_files_only=False,
            )
            pipe = DiffusionPipeline.from_pretrained(
                local_dir,
                trust_remote_code=True,
                tokenizer_4=tokenizer_4,
                text_encoder_4=text_encoder_4,
                torch_dtype=dtype,
            )
        else:
            raise

    device = "cuda" if torch.cuda.is_available() else (
        "mps" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "cpu"
    )
    pipe.to(device)
    return pipe

router = APIRouter(prefix="/api/instruct", tags=["instruct"])

@router.post("/pix2pix")
async def instruct_pix2pix(
    file: UploadFile = File(...),
    prompt: str = Form(...),
    steps: int = Form(28),                 # HiDream-E1 docs suggest ~28 steps
    guidance: float = Form(5.0),          # Default per docs
    image_guidance: float = Form(4.0),    # Default per docs
    negative_prompt: Optional[str] = Form(None),
    destination: str = Form("r2"),
):
    try:
        raw = await file.read()
        if not raw:
            return JSONResponse(status_code=400, content={"error": "empty file"})

        # HiDream-E1 examples use 768x768
        img = Image.open(io.BytesIO(raw)).convert("RGB").resize((768, 768))

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

        images = p(**kwargs).images
        out = images[0]

        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)

        # Reuse existing storage util if available
        try:
            from app.utils.storage import upload_bytes
            base = os.path.splitext(file.filename or uuid.uuid4().hex[:12])[0]
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