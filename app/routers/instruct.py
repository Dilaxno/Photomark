from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse
import io
import os
import uuid

# Lazy import heavy deps inside handler to avoid startup cost if not used
pipe = None

def get_pipe():
    global pipe
    if pipe is not None:
        return pipe
    import torch
    from diffusers import DiffusionPipeline

    dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    pipe = DiffusionPipeline.from_pretrained(
        "HiDream-ai/HiDream-E1-Full", torch_dtype=dtype
    )
    device = "cuda" if torch.cuda.is_available() else (
        "mps" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "cpu"
    )
    pipe.to(device)
    return pipe

router = APIRouter(prefix="/api/instruct", tags=["instruct"])

@router.post("/pix2pix")
async def instruct_pix2pix(
    prompt: str = Form(...),
    destination: str = Form("r2"),
):
    try:
        # Generate image from text prompt using HiDream-E1-Full
        p = get_pipe()
        images = p(prompt=prompt).images
        out = images[0]

        # Buffer the image as PNG
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)

        # Reuse existing storage util if available
        try:
            from app.utils.storage import upload_bytes
            base = uuid.uuid4().hex[:12]
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