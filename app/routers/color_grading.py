import os
import torch
import numpy as np
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
import zipfile
import shutil
from typing import List
from werkzeug.utils import secure_filename

router = APIRouter(prefix="/api", tags=["color-grading"])

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def load_cube_lut(file_path):
    """Load a .cube LUT as float16 tensor on GPU"""
    with open(file_path, 'r') as f:
        lines = f.readlines()

    lut_lines = [
        [float(x) for x in line.strip().split()]
        for line in lines
        if line.strip() and not line.startswith(('#', 'TITLE', 'LUT_3D_SIZE'))
    ]

    lut = torch.tensor(lut_lines, dtype=torch.float16)
    size = int(len(lut_lines) ** (1 / 3))
    lut = lut.view(size, size, size, 3).permute(3, 0, 1, 2).unsqueeze(0).cuda()
    return lut, size


def trilinear_lut_batch(img_tensor, lut):
    """Trilinear LUT sampling for batch NxCxHxW"""
    N, C, H, W = img_tensor.shape
    img_tensor = img_tensor.half()
    r, g, b = img_tensor[:, 0:1, :, :], img_tensor[:, 1:2, :, :], img_tensor[:, 2:3, :, :]
    grid = torch.stack([r * 2 - 1, g * 2 - 1, b * 2 - 1], dim=-1).unsqueeze(2)  # NxHx1xWx3
    output = torch.nn.functional.grid_sample(
        lut.expand(N, -1, -1, -1, -1),
        grid,
        mode='bilinear',
        align_corners=True
    )
    return output[:, :, :, 0].float()


def estimate_optimal_batch(images):
    """Estimate safe batch size based on GPU memory and image sizes"""
    torch.cuda.empty_cache()
    free_mem = torch.cuda.get_device_properties(0).total_memory
    free_mem = int(free_mem * 0.5)  # use at most 50%

    avg_size = np.mean([np.prod(Image.open(img).size) * 3 for img in images])
    bytes_per_image = avg_size * 2  # float16 = 2 bytes
    batch_size = max(1, int(free_mem // bytes_per_image))
    return batch_size


def process_images_dynamic_batch(input_paths, lut, output_folder):
    """Process images using automatic batch sizing and chunking"""
    batch_size = estimate_optimal_batch(input_paths)
    output_files, chunk, sizes, filenames = [], [], [], []

    for path in input_paths + [None]:  # add None to flush last chunk
        if path:
            img = Image.open(path).convert("RGB")
            img_tensor = torch.from_numpy(np.array(img) / 255.0).permute(2, 0, 1).float()
            chunk.append(img_tensor)
            sizes.append(img.size)
            filenames.append(os.path.basename(path))
        if len(chunk) == batch_size or (path is None and chunk):
            max_H, max_W = max(t.shape[1] for t in chunk), max(t.shape[2] for t in chunk)
            batch = []
            for t in chunk:
                pad = torch.zeros(3, max_H, max_W)
                pad[:, :t.shape[1], :t.shape[2]] = t
                batch.append(pad)
            batch_tensor = torch.stack(batch).cuda()
            output_batch = trilinear_lut_batch(batch_tensor, lut)
            for i in range(len(chunk)):
                W, H = sizes[i]
                out_img = Image.fromarray(
                    (output_batch[i, :, :H, :W].cpu().numpy().transpose(1, 2, 0) * 255).astype(np.uint8)
                )
                out_path = os.path.join(output_folder, f"graded_{filenames[i]}")
                out_img.save(out_path)
                output_files.append(out_path)
            del batch_tensor, output_batch
            torch.cuda.empty_cache()
            chunk, sizes, filenames = [], [], []

    return output_files


@router.post("/apply-lut")
async def apply_lut(images: List[UploadFile] = File(...), lut: UploadFile = File(...)):
    if not images or not lut:
        raise HTTPException(status_code=400, detail="Images or LUT file missing")

    # Save LUT
    lut_path = os.path.join(UPLOAD_FOLDER, secure_filename(lut.filename))
    with open(lut_path, "wb") as f:
        shutil.copyfileobj(lut.file, f)
    lut_tensor, _ = load_cube_lut(lut_path)
    # Save images
    input_paths = []
    for img in images:
        img_path = os.path.join(UPLOAD_FOLDER, secure_filename(img.filename))
        with open(img_path, "wb") as f:
            shutil.copyfileobj(img.file, f)
        input_paths.append(img_path)

    output_files = process_images_dynamic_batch(input_paths, lut_tensor, OUTPUT_FOLDER)

    if len(output_files) == 1:
        return FileResponse(output_files[0], filename=os.path.basename(output_files[0]))
    else:
        zip_path = os.path.join(OUTPUT_FOLDER, "graded_images.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for f in output_files:
                zf.write(f, os.path.basename(f))
        return FileResponse(zip_path, filename="graded_images.zip")