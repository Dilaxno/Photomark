import os
import re
import io
import zipfile
import shutil
from typing import List, Tuple

import numpy as np
import torch
from fastapi import APIRouter, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from PIL import Image
from datetime import datetime as _dt
from app.core.auth import resolve_workspace_uid, has_role_access
from app.utils.storage import upload_bytes, read_json_key, write_json_key

router = APIRouter(prefix="/api", tags=["color-grading"])

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Select device based on availability
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
DTYPE = torch.float16 if DEVICE.type == "cuda" else torch.float32

# Billing/free-limit helpers (one free generation per user across tools)

def _billing_uid_from_request(request: Request) -> str:
  eff_uid, _ = resolve_workspace_uid(request)
  if eff_uid:
    return eff_uid
  try:
    ip = request.client.host if getattr(request, 'client', None) else 'unknown'
  except Exception:
    ip = 'unknown'
  return f"anon:{ip}"


def _is_paid_customer(uid: str) -> bool:
  try:
    ent = read_json_key(f"users/{uid}/billing/entitlement.json") or {}
    plan = str(ent.get('plan') or '').strip().lower()
    if plan and plan != 'free':
      return True
    return bool(ent.get('isPaid'))
  except Exception:
    return False


def _consume_one_free(uid: str, tool: str) -> bool:
  key = f"users/{uid}/billing/free_usage.json"
  try:
    data = read_json_key(key) or {}
  except Exception:
    data = {}
  count = int(data.get('count') or 0)
  if count >= 1:
    return False
  tools = data.get('tools') or {}
  tools[tool] = int(tools.get(tool) or 0) + 1
  try:
    write_json_key(key, {
      'used': True,
      'count': count + 1,
      'tools': tools,
      'updatedAt': int(_dt.utcnow().timestamp()),
    })
  except Exception:
    # Best-effort write; still allow the first run
    pass
  return True


def safe_filename(name: str) -> str:
  base = os.path.basename(name or "")
  base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
  return base or "file"


def load_cube_lut(file_path: str) -> Tuple[torch.Tensor, int]:
  """Load a .cube LUT as tensor on device.

  Supports basic .cube with LUT_3D_SIZE N lines of R G B triplets.
  If LUT_3D_SIZE is not found, we infer size from number of rows.
  """
  try:
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
      lines = [ln.strip() for ln in f.readlines()]
  except Exception as e:
    raise HTTPException(status_code=400, detail=f"Failed to read LUT: {e}")

  size = None
  data: List[List[float]] = []
  for ln in lines:
    if not ln or ln.startswith(('#', 'TITLE')):
      continue
    if ln.upper().startswith('LUT_3D_SIZE'):
      try:
        parts = ln.split()
        size = int(parts[-1])
      except Exception:
        pass
      continue
    if ln.upper().startswith('DOMAIN_'):
      # Ignored for standard 0..1 images
      continue
    # data line: r g b
    parts = ln.split()
    if len(parts) == 3:
      try:
        rgb = [float(parts[0]), float(parts[1]), float(parts[2])]
        data.append(rgb)
      except Exception:
        continue

  if not data:
    raise HTTPException(status_code=400, detail="LUT has no data rows")

  if size is None:
    # infer cubic size
    n = len(data)
    size = round(n ** (1.0/3))
    if size * size * size != n:
      raise HTTPException(status_code=400, detail="LUT data is not cubic or missing LUT_3D_SIZE")

  lut = torch.tensor(data, dtype=DTYPE, device=DEVICE)
  # Reshape to (1, C=3, D=size, H=size, W=size)
  try:
    lut = lut.view(size, size, size, 3).permute(3, 0, 1, 2).unsqueeze(0)
  except Exception:
    raise HTTPException(status_code=400, detail="Invalid LUT dimensions/content")
  return lut, size


def trilinear_lut_batch(img_tensor: torch.Tensor, lut: torch.Tensor) -> torch.Tensor:
  """Apply 3D LUT via trilinear sampling for batch NxCxHxW.
  Returns NxCxHxW in float32 0..1.
  """
  if img_tensor.device != DEVICE:
    img_tensor = img_tensor.to(DEVICE)
  # Use half on CUDA for speed/memory, float32 otherwise
  img_tensor = img_tensor.to(DTYPE)

  # Prepare grid for 3D sampling: values in [-1, 1]
  r = img_tensor[:, 0:1, :, :]
  g = img_tensor[:, 1:2, :, :]
  b = img_tensor[:, 2:3, :, :]
  grid = torch.stack([r, g, b], dim=-1)  # (N,1,H,W,3)
  grid = grid * 2.0 - 1.0  # scale to [-1,1]; out_D=1, out_H=H, out_W=W

  out = torch.nn.functional.grid_sample(
    input=lut.expand(img_tensor.shape[0], -1, -1, -1, -1),  # (N,3,D,H,W)
    grid=grid,  # (N, out_D=1, out_H=H, out_W=W, 3)
    mode='bilinear',
    align_corners=True,
  )  # -> (N,3,1,H,W)

  # Drop the singleton depth dimension
  out = out[:, :, 0, :, :]  # (N,3,H,W)
  return out.float()


def estimate_optimal_batch(image_paths: List[str]) -> int:
  """Estimate batch size. On CUDA, use ~50% VRAM heuristic; on CPU, 1."""
  if DEVICE.type != 'cuda':
    return 1
  try:
    torch.cuda.empty_cache()
    total = torch.cuda.get_device_properties(0).total_memory
    budget = int(total * 0.5)
    # Rough estimate: average HxW*3 channels * bytes per channel (DTYPE)
    sizes = []
    for p in image_paths:
      with Image.open(p) as im:
        w, h = im.size
      sizes.append(w * h * 3)
    avg_size = int(np.mean(sizes)) if sizes else (2048 * 2048 * 3)
    bytes_per = avg_size * (2 if DTYPE == torch.float16 else 4)
    return max(1, budget // max(bytes_per, 1))
  except Exception:
    return 1


def process_images_dynamic_batch(input_paths: List[str], lut: torch.Tensor, output_folder: str) -> List[str]:
  """Process images using automatic batch sizing and chunking."""
  bs = estimate_optimal_batch(input_paths)
  output_files: List[str] = []
  chunk: List[torch.Tensor] = []
  sizes: List[Tuple[int, int]] = []
  names: List[str] = []

  for path in input_paths + [None]:
    if path:
      with Image.open(path) as img:
        img = img.convert("RGB")
        arr = np.asarray(img, dtype=np.float32) / 255.0
      t = torch.from_numpy(arr).permute(2, 0, 1)  # C,H,W
      chunk.append(t)
      sizes.append((img.width, img.height))
      names.append(os.path.basename(path))

    if (len(chunk) == bs) or (path is None and chunk):
      # Pad to max height/width in chunk
      max_h = max(t.shape[1] for t in chunk)
      max_w = max(t.shape[2] for t in chunk)
      batch = []
      for t in chunk:
        pad = torch.zeros(3, max_h, max_w, dtype=torch.float32)
        pad[:, :t.shape[1], :t.shape[2]] = t
        batch.append(pad)
      batch_tensor = torch.stack(batch, dim=0).to(DEVICE)
      out_batch = trilinear_lut_batch(batch_tensor, lut)

      for i in range(len(chunk)):
        w, h = sizes[i]
        out_np = (out_batch[i, :, :h, :w].detach().cpu().numpy().transpose(1, 2, 0))
        out_img = Image.fromarray(np.clip(out_np * 255.0, 0, 255).astype(np.uint8))
        out_path = os.path.join(output_folder, f"graded_{names[i]}")
        out_img.save(out_path)
        output_files.append(out_path)

      del batch_tensor, out_batch
      if DEVICE.type == 'cuda':
        torch.cuda.empty_cache()
      chunk, sizes, names = [], [], []

  return output_files


def apply_lut_to_image_pil(img: Image.Image, lut: torch.Tensor) -> Image.Image:
  img = img.convert("RGB")
  arr = (np.asarray(img, dtype=np.float32) / 255.0)
  t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)  # 1,C,H,W
  out = trilinear_lut_batch(t, lut)[0]  # C,H,W
  out_np = (out.detach().cpu().numpy().transpose(1, 2, 0))
  out_img = Image.fromarray(np.clip(out_np * 255.0, 0, 255).astype(np.uint8))
  return out_img


@router.post("/apply-lut")
async def apply_lut(request: Request, images: List[UploadFile] = File(...), lut: UploadFile = File(...)):
  if not images or not lut:
    raise HTTPException(status_code=400, detail="Images or LUT file missing")

  # Enforce one free generation per user unless paid
  billing_uid = _billing_uid_from_request(request)
  if not _is_paid_customer(billing_uid):
    if not _consume_one_free(billing_uid, 'color_grading'):
      return JSONResponse({
        "error": "free_limit_reached",
        "message": "You have used your free generation. Upgrade to continue.",
      }, status_code=402)

  # Save LUT
  lut_name = safe_filename(lut.filename)
  lut_path = os.path.join(UPLOAD_FOLDER, lut_name)
  with open(lut_path, "wb") as f:
    shutil.copyfileobj(lut.file, f)
  lut_tensor, _ = load_cube_lut(lut_path)

  # Save images
  input_paths: List[str] = []
  for img in images:
    img_name = safe_filename(img.filename)
    img_path = os.path.join(UPLOAD_FOLDER, img_name)
    with open(img_path, "wb") as f:
      shutil.copyfileobj(img.file, f)
    input_paths.append(img_path)

  output_files = process_images_dynamic_batch(input_paths, lut_tensor, OUTPUT_FOLDER)

  if len(output_files) == 1:
    fp = output_files[0]
    return FileResponse(fp, filename=os.path.basename(fp))
  else:
    zip_path = os.path.join(OUTPUT_FOLDER, "graded_images.zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
      for f in output_files:
        zf.write(f, os.path.basename(f))
    return FileResponse(zip_path, filename="graded_images.zip")


@router.post("/color-grading/preview")
async def preview(request: Request, image: UploadFile = File(...), lut: UploadFile = File(...)):
  """Return a quick preview of a single image with the LUT applied as PNG bytes."""
  if not image or not lut:
    raise HTTPException(status_code=400, detail="Image or LUT file missing")

  # Enforce one free generation per user unless paid
  billing_uid = _billing_uid_from_request(request)
  if not _is_paid_customer(billing_uid):
    if not _consume_one_free(billing_uid, 'color_grading'):
      return JSONResponse({
        "error": "free_limit_reached",
        "message": "You have used your free generation. Upgrade to continue.",
      }, status_code=402)

  # Read LUT into temp file and load
  lut_name = safe_filename(lut.filename)
  lut_path = os.path.join(UPLOAD_FOLDER, lut_name)
  with open(lut_path, "wb") as f:
    shutil.copyfileobj(lut.file, f)
  lut_tensor, _ = load_cube_lut(lut_path)

  # Read image from stream
  try:
    img_bytes = await image.read()
    img = Image.open(io.BytesIO(img_bytes))
  except Exception as e:
    raise HTTPException(status_code=400, detail=f"Invalid image: {e}")

  try:
    out_img = apply_lut_to_image_pil(img, lut_tensor)
  except Exception as e:
    raise HTTPException(status_code=500, detail=f"Failed to apply LUT: {e}")

  buf = io.BytesIO()
  out_img.save(buf, format="PNG")
  buf.seek(0)
  return StreamingResponse(buf, media_type="image/png")


@router.post("/color-grading/apply-to-gallery")
async def apply_to_gallery(request: Request, images: List[UploadFile] = File(...), lut: UploadFile = File(...)):
  """Apply LUT to provided images and upload results to the authenticated user's gallery.
  Writes to users/{uid}/watermarked/YYYY/MM/DD/ with suffix -lut.
  """
  if not images or not lut:
    raise HTTPException(status_code=400, detail="Images or LUT file missing")

  eff_uid, req_uid = resolve_workspace_uid(request)

  # Enforce one free generation per owner workspace unless paid
  billing_uid = eff_uid or _billing_uid_from_request(request)
  if not _is_paid_customer(billing_uid):
    if not _consume_one_free(billing_uid, 'color_grading'):
      return JSONResponse({
        "error": "free_limit_reached",
        "message": "You have used your free generation. Upgrade to continue.",
      }, status_code=402)
  if not eff_uid or not req_uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  # Writing to gallery uses retouch permission (same as watermark upload)
  if not has_role_access(req_uid, eff_uid, 'retouch'):
    return JSONResponse({"error": "Forbidden"}, status_code=403)
  uid = eff_uid

  # Save & load LUT
  lut_name = safe_filename(lut.filename)
  lut_path = os.path.join(UPLOAD_FOLDER, lut_name)
  with open(lut_path, "wb") as f:
    shutil.copyfileobj(lut.file, f)
  lut_tensor, _ = load_cube_lut(lut_path)

  uploaded = []
  date_prefix = _dt.utcnow().strftime('%Y/%m/%d')

  for uf in images:
    try:
      raw = await uf.read()
      if not raw:
        continue
      img = Image.open(io.BytesIO(raw)).convert("RGB")
      out_img = apply_lut_to_image_pil(img, lut_tensor)

      # Encode as JPEG
      buf = io.BytesIO()
      try:
        import piexif  # type: ignore
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
        exif_bytes = piexif.dump(exif_dict)
        out_img.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True, exif=exif_bytes)
      except Exception:
        out_img.save(buf, format="JPEG", quality=95, subsampling=0, progressive=True, optimize=True)
      buf.seek(0)

      base = os.path.splitext(os.path.basename(uf.filename or 'image'))[0] or 'image'
      stamp = int(_dt.utcnow().timestamp())
      # carry original extension token for consistency
      orig_ext = (os.path.splitext(uf.filename or '')[1] or '.jpg').lower()
      oext_token = (orig_ext.lstrip('.') or 'jpg').lower()
      key = f"users/{uid}/watermarked/{date_prefix}/{base}-{stamp}-lut-o{oext_token}.jpg"

      url = upload_bytes(key, buf.getvalue(), content_type='image/jpeg')
      uploaded.append({"key": key, "url": url})
    except Exception:
      continue

  return {"ok": True, "uploaded": uploaded}
