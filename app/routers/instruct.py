from fastapi import APIRouter, UploadFile, Form
from fastapi.responses import FileResponse
import torch
from diffusers import DiffusionPipeline
from PIL import Image
import io

router = APIRouter(prefix="/api/instruct", tags=["instruct"])

# Load pipeline once
pipe = DiffusionPipeline.from_pretrained(
    "timbrooks/instruct-pix2pix",
    torch_dtype=torch.float16,
    safety_checker=None
).to("cuda")

@router.post("/edit")
async def edit_image(file: UploadFile, prompt: str = Form(...)):
    # Read uploaded file
    image_bytes = await file.read()
    input_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    # Run InstructPix2Pix
    result = pipe(image=input_image, prompt=prompt).images[0]

    # Save and return result
    output_path = "edited.png"
    result.save(output_path)
    return FileResponse(output_path, media_type="image/png")