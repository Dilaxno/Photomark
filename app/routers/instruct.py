from fastapi import FastAPI, UploadFile, Form
from fastapi.responses import FileResponse
import torch
from diffusers import DiffusionPipeline
from PIL import Image
import io

app = FastAPI()

# Load pipeline once
pipe = DiffusionPipeline.from_pretrained(
    "timbrooks/instruct-pix2pix",
    torch_dtype=torch.float16,
    safety_checker=None
).to("cuda")

@app.post("/edit")
async def edit_image(file: UploadFile, prompt: str = Form(...)):
    image_bytes = await file.read()
    input_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    result = pipe(image=input_image, prompt=prompt).images[0]
    output_path = "edited.png"
    result.save(output_path)
    return FileResponse(output_path, media_type="image/png")