from collections import OrderedDict

# ---- Tiny in-memory caches for repeated previews with same reference ----
_REF_CDF_CACHE: "OrderedDict[str, np.ndarray]" = OrderedDict()
_REF_LAB_CACHE: "OrderedDict[str, tuple[np.ndarray, np.ndarray]]" = OrderedDict()
_CACHE_LIMIT = 32

def _cache_put(cache: "OrderedDict", key: str, value):
  cache[key] = value
  cache.move_to_end(key)
  while len(cache) > _CACHE_LIMIT:
    try:
      cache.popitem(last=False)
    except Exception:
      break

def _cache_get(cache: "OrderedDict", key: str):
  if key in cache:
    cache.move_to_end(key)
    return cache[key]
  return None

from fastapi import APIRouter, UploadFile, File, Form, Request
from starlette.responses import StreamingResponse, JSONResponse
from typing import List, Optional, Tuple
import io
import hashlib
import zipfile
import os
import concurrent.futures as cf

import numpy as np
from PIL import Image
import cv2

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.config import logger
from app.utils.storage import read_json_key, write_json_key

router = APIRouter(prefix="/api/style", tags=["style"])  # matches existing /api/style namespace

# ---- One-free-generation helpers (shared policy) ----
from datetime import datetime as _dt

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
    # Consider any non-free plan as paid to avoid entitlement lag blocking paid users
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
    pass
  return True


# ---------- Helpers ----------

def _pil_to_np_rgb(img: Image.Image) -> np.ndarray:
  if img.mode != 'RGB':
    img = img.convert('RGB')
  arr = np.asarray(img).astype(np.uint8)
  return arr


def _np_to_pil(arr: np.ndarray) -> Image.Image:
  arr = np.clip(arr, 0, 255).astype(np.uint8)
  return Image.fromarray(arr, mode='RGB')


def _downscale(image: Image.Image, target: int = 512) -> Image.Image:
  """Downscale preserving aspect ratio so that max(width, height) == target (or smaller)."""
  if image.mode != 'RGB':
    image = image.convert('RGB')
  w, h = image.size
  if w <= target and h <= target:
    return image
  scale = target / float(max(w, h))
  new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
  return image.resize(new_size, Image.Resampling.LANCZOS)


def _cdf_3x256(np_rgb: np.ndarray) -> np.ndarray:
  """Compute per-channel CDF for uint8 RGB image. Returns array shape [3,256] in [0,1]."""
  cdfs = np.zeros((3, 256), dtype=np.float64)
  for c in range(3):
    vals = np_rgb[..., c].ravel().astype(np.uint8)
    counts = np.bincount(vals, minlength=256).astype(np.float64)
    total = counts.sum()
    if total <= 0:
      # Edge case: empty or invalid -> identity CDF ramp
      cdfs[c] = np.linspace(0.0, 1.0, 256, dtype=np.float64)
      continue
    cdf = counts.cumsum() / total
    cdfs[c] = cdf
  return cdfs


def _lut_from_cdfs(src_cdf: np.ndarray, ref_cdf: np.ndarray) -> np.ndarray:
  """Build per-channel LUT (3x256 uint8) mapping src intensities to reference via CDF matching.
  Uses interpolation over unique ref CDF values for stability.
  """
  luts = np.zeros((3, 256), dtype=np.uint8)
  xp_full = np.arange(256, dtype=np.float64)
  for c in range(3):
    s = src_cdf[c]
    r = ref_cdf[c]
    # Ensure monotonic xp for interpolation
    r_unique, idxs = np.unique(r, return_index=True)
    if r_unique.size < 2:
      # Degenerate reference distribution: no variation -> identity map
      luts[c] = xp_full.astype(np.uint8)
      continue
    fp = xp_full[idxs]
    mapped = np.interp(s, r_unique, fp)
    luts[c] = np.clip(np.rint(mapped), 0, 255).astype(np.uint8)
  return luts


def _apply_lut_rgb(src_full: np.ndarray, lut: np.ndarray) -> np.ndarray:
  """Apply per-channel LUT to full-res RGB uint8 array. Uses OpenCV cv2.LUT if available (faster)."""
  try:
    # cv2.LUT expects shape (256,) per channel
    out = np.empty_like(src_full)
    for c in range(3):
      out[..., c] = cv2.LUT(src_full[..., c], lut[c].reshape(-1))
    return out
  except Exception:
    out = np.empty_like(src_full)
    for c in range(3):
      out[..., c] = lut[c][src_full[..., c]]
    return out


# ---------- Reinhard mean/std color transfer helpers ----------

def _rgb_uint8_to_float(img_np: np.ndarray) -> np.ndarray:
  """Convert uint8 [0,255] RGB to float [0,1]."""
  if img_np.dtype != np.float32 and img_np.dtype != np.float64:
    return (img_np.astype(np.float32) / 255.0).clip(0.0, 1.0)
  return img_np


def _lab_stats(lab: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
  """Return mean and std across H,W for Lab image with shape [H,W,3]."""
  mean = lab.mean(axis=(0, 1))
  std = lab.std(axis=(0, 1))
  # Guard against zero std to avoid division by zero
  std = np.where(std < 1e-6, 1.0, std)
  return mean, std


def _reinhard_apply(target_lab: np.ndarray, tgt_mean: np.ndarray, tgt_std: np.ndarray, src_mean: np.ndarray, src_std: np.ndarray) -> np.ndarray:
  """Apply Reinhard transfer in Lab space to target_lab given stats."""
  out = (target_lab - tgt_mean) / tgt_std * src_std + src_mean
  # Clip to reasonable Lab ranges: L[0,100], a/b approximately [-127,127]
  out[..., 0] = np.clip(out[..., 0], 0.0, 100.0)
  out[..., 1:] = np.clip(out[..., 1:], -127.0, 127.0)
  return out


def _rgb_to_lab_cv(rgb_float: np.ndarray) -> np.ndarray:
  """RGB float32 [0,1] -> Lab float32 where L[0,100], a/b[-128,127]."""
  if rgb_float.dtype != np.float32:
    rgb_float = rgb_float.astype(np.float32)
  return cv2.cvtColor(rgb_float, cv2.COLOR_RGB2LAB)


def _lab_to_rgb_cv(lab: np.ndarray) -> np.ndarray:
  """Lab float32 (L[0,100], a/b approx [-128,127]) -> RGB float32 [0,1]."""
  if lab.dtype != np.float32:
    lab = lab.astype(np.float32)
  rgb = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
  return np.clip(rgb, 0.0, 1.0)


def _process_blob_reinhard(
  blob: bytes,
  filename: str,
  ref_mean: np.ndarray,
  ref_std: np.ndarray,
  fmt: str,
  quality: float,
  preview_max_side: int | None = None,
) -> Tuple[str, bytes]:
  """Worker for Reinhard transfer: compute target stats on downscaled target, apply to full-res in Lab."""
  # Load full target
  tgt_img_full = Image.open(io.BytesIO(blob)).convert('RGB')
  # If preview, process a capped-resolution version for speed
  if preview_max_side and max(tgt_img_full.size) > preview_max_side:
    tgt_img_full = _downscale(tgt_img_full, preview_max_side)
  tgt_full_np_u8 = _pil_to_np_rgb(tgt_img_full)
  tgt_full_np = _rgb_uint8_to_float(tgt_full_np_u8)
  # Downscale for stats
  tgt_small = _downscale(tgt_img_full, 384)
  tgt_small_np = _rgb_uint8_to_float(_pil_to_np_rgb(tgt_small))

  # Convert to Lab
  tgt_small_lab = _rgb_to_lab_cv(tgt_small_np)
  tgt_mean, tgt_std = _lab_stats(tgt_small_lab)

  tgt_full_lab = _rgb_to_lab_cv(tgt_full_np)
  out_lab = _reinhard_apply(tgt_full_lab, tgt_mean, tgt_std, ref_mean, ref_std)
  out_rgb = _lab_to_rgb_cv(out_lab)
  out_u8 = np.clip(np.rint(out_rgb * 255.0), 0, 255).astype(np.uint8)
  out_img = _np_to_pil(out_u8)

  buf = io.BytesIO()
  out_fmt = (fmt or 'jpg').lower()
  if out_fmt in ('jpg', 'jpeg'):
    q = int(max(1, min(100, round((quality or 0.92) * 100))))
    # Faster save: avoid progressive/optimize and custom subsampling for speed
    out_img.save(buf, format='JPEG', quality=q)
    ext = 'jpg'
  else:
    out_img.save(buf, format='PNG')
    ext = 'png'
  buf.seek(0)
  base = os.path.splitext(filename or 'image')[0]
  name = f"{base}_styled.{ext}"
  return name, buf.getvalue()


def _process_blob(
  blob: bytes,
  filename: str,
  ref_cdf: np.ndarray,
  fmt: str,
  quality: float,
  preview_max_side: int | None = None,
) -> Tuple[str, bytes]:
  """Worker function: compute LUT from source small vs reference CDF, then apply to full-res and encode."""
  try:
    src_img_full = Image.open(io.BytesIO(blob)).convert('RGB')
    # For preview, process a capped-resolution version for speed
    if preview_max_side and max(src_img_full.size) > preview_max_side:
      src_img_full = _downscale(src_img_full, preview_max_side)
    src_full_np = _pil_to_np_rgb(src_img_full)

    # Compute LUT on downscaled image vs precomputed ref CDF
    src_small = _downscale(src_img_full, 512)
    src_small_np = _pil_to_np_rgb(src_small)
    src_cdf = _cdf_3x256(src_small_np)
    lut = _lut_from_cdfs(src_cdf, ref_cdf)

    # Apply LUT to full resolution
    out_np = _apply_lut_rgb(src_full_np, lut)
    out_img = _np_to_pil(out_np)

    # Encode (favor speed: avoid progressive/optimize)
    buf = io.BytesIO()
    out_fmt = (fmt or 'jpg').lower()
    if out_fmt in ('jpg', 'jpeg'):
      q = int(max(1, min(100, round((quality or 0.92) * 100))))
      out_img.save(buf, format='JPEG', quality=q)
      ext = 'jpg'
    else:
      out_img.save(buf, format='PNG')
      ext = 'png'
    buf.seek(0)
    base = os.path.splitext(filename or 'image')[0]
    name = f"{base}_styled.{ext}"
    return name, buf.getvalue()
  except Exception as ex:
    logger.exception(f"Failed to process {filename}: {ex}")
    # Reraise to let caller decide error handling, but we prefer to continue other files.
    raise


@router.post('/hist-match')
async def hist_match(
  request: Request,
  reference: UploadFile = File(..., description='Reference image to copy style from'),
  files: List[UploadFile] = File(..., description='Target images to apply reference style to'),
  fmt: Optional[str] = Form('jpg'),
  quality: Optional[float] = Form(0.92),
  preview: Optional[int] = Form(0),
):
  """
  Copy the color style (exposure/color distribution) from a reference image to a batch of images.

  Performance-optimized & parallelized:
  - Compute per-channel CDF mapping using 512×512 downscaled versions of the reference and each source.
  - Apply the resulting LUT to the full-resolution source image.
  - Use a process pool to process multiple images concurrently for large batches.

  Returns a single image if one target is provided, or a ZIP archive if multiple targets are processed.
  """
  # Auth (mirror convert/style_lut behavior)
  eff_uid, req_uid = resolve_workspace_uid(request)
  if not eff_uid or not req_uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  if not has_role_access(req_uid, eff_uid, 'convert'):
    return JSONResponse({"error": "Forbidden"}, status_code=403)

  # One-free-generation enforcement (counts against owner workspace)
  billing_uid = eff_uid or _billing_uid_from_request(request)
  if not _is_paid_customer(billing_uid):
    if not _consume_one_free(billing_uid, 'style_transfer'):
      return JSONResponse({
        "error": "free_limit_reached",
        "message": "You have used your free generation. Upgrade to continue.",
      }, status_code=402)

  try:
    # Load and prepare reference CDF (on downscaled image)
    ref_bytes = await reference.read()
    if not ref_bytes:
      return JSONResponse({"error": "empty reference"}, status_code=400)
    # Cache reference CDF by content hash to speed repeated previews
    ref_key = hashlib.sha1(ref_bytes).hexdigest()
    ref_cdf = _cache_get(_REF_CDF_CACHE, ref_key)
    if ref_cdf is None:
      ref_img_full = Image.open(io.BytesIO(ref_bytes)).convert('RGB')
      ref_small = _downscale(ref_img_full, 384)
      ref_small_np = _pil_to_np_rgb(ref_small)
      ref_cdf = _cdf_3x256(ref_small_np)
      _cache_put(_REF_CDF_CACHE, ref_key, ref_cdf)

    # Collect input bytes first (UploadFile is not picklable)
    inputs: List[Tuple[int, str, bytes]] = []
    idx = 0
    for f in files:
      data = await f.read()
      if not data:
        idx += 1
        continue
      inputs.append((idx, f.filename or f"image_{idx}.jpg", data))
      idx += 1

    if not inputs:
      return JSONResponse({"error": "No images processed"}, status_code=400)

    # Determine worker count
    cpu = os.cpu_count() or 2
    # Keep it modest to reduce overhead and memory spikes
    max_workers = min(max(1, cpu), max(1, len(inputs)))

    results: List[Tuple[int, str, bytes]] = []

    if len(inputs) == 1 or max_workers == 1:
      # Sequential path for single image or constrained env
      for i, name, blob in inputs:
        try:
          out_name, out_bytes = _process_blob(
            blob, name, ref_cdf, fmt or 'jpg', float(quality or 0.92),
            1600 if (preview and len(inputs) == 1) else None,
          )
          results.append((i, out_name, out_bytes))
        except Exception:
          continue
    else:
      # Parallel path using process pool
      with cf.ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = []
        for i, name, blob in inputs:
          futures.append((
            i, name,
            ex.submit(
              _process_blob,
              blob, name, ref_cdf, fmt or 'jpg', float(quality or 0.92),
              1600 if (preview and len(inputs) == 1) else None,
            )
          ))
        for i, name, fut in futures:
          try:
            out_name, out_bytes = fut.result()
            results.append((i, out_name, out_bytes))
          except Exception as exn:
            logger.exception(f"Processing failed for {name}: {exn}")
            continue

    if not results:
      return JSONResponse({"error": "No images processed"}, status_code=400)

    # Preserve input order
    results.sort(key=lambda t: t[0])

    # Single image response
    if len(results) == 1:
      _, name, data = results[0]
      media = 'image/jpeg' if name.lower().endswith('.jpg') or name.lower().endswith('.jpeg') else 'image/png'
      headers = {
        "Content-Disposition": f"attachment; filename={name}",
        "Access-Control-Expose-Headers": "Content-Disposition",
      }
      return StreamingResponse(io.BytesIO(data), media_type=media, headers=headers)

    # Multiple -> ZIP with unique names to avoid duplicate warnings
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
      used_names: set[str] = set()
      def _unique_name(n: str) -> str:
        base, ext = os.path.splitext(n)
        cand = n
        i = 1
        while cand in used_names:
          cand = f"{base}_{i}{ext}"
          i += 1
        used_names.add(cand)
        return cand
      for _, name, data in results:
        zf.writestr(_unique_name(name), data)
    zip_buf.seek(0)
    headers = {
      "Content-Disposition": "attachment; filename=styled_batch.zip",
      "Access-Control-Expose-Headers": "Content-Disposition",
    }
    return StreamingResponse(zip_buf, media_type='application/zip', headers=headers)

  except Exception as ex:
    logger.exception(f"Histogram matching failed: {ex}")
    return JSONResponse({"error": str(ex)}, status_code=500)


@router.post('/reinhard')
async def reinhard_match(
  request: Request,
  reference: UploadFile = File(..., description='Reference image to copy style from'),
  files: List[UploadFile] = File(..., description='Target images to apply reference style to'),
  fmt: Optional[str] = Form('jpg'),
  quality: Optional[float] = Form(0.92),
  preview: Optional[int] = Form(0),
):
  """
  Copy the color style using Reinhard mean/std transfer in Lab space.
  - Compute reference Lab mean/std on downscaled reference.
  - For each target, compute target Lab mean/std on downscaled target.
  - Apply transfer to full-res target in Lab and encode.
  """
  # Auth
  eff_uid, req_uid = resolve_workspace_uid(request)
  if not eff_uid or not req_uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  if not has_role_access(req_uid, eff_uid, 'convert'):
    return JSONResponse({"error": "Forbidden"}, status_code=403)

  # One-free-generation enforcement
  billing_uid = eff_uid or _billing_uid_from_request(request)
  if not _is_paid_customer(billing_uid):
    if not _consume_one_free(billing_uid, 'style_transfer'):
      return JSONResponse({
        "error": "free_limit_reached",
        "message": "You have used your free generation. Upgrade to continue.",
      }, status_code=402)

  try:
    # Read reference
    ref_bytes = await reference.read()
    if not ref_bytes:
      return JSONResponse({"error": "empty reference"}, status_code=400)
    # Cache reference Lab stats by content hash
    ref_key = hashlib.sha1(ref_bytes).hexdigest()
    cached = _cache_get(_REF_LAB_CACHE, ref_key)
    if cached is None:
      ref_img_full = Image.open(io.BytesIO(ref_bytes)).convert('RGB')
      ref_small = _downscale(ref_img_full, 512)
      ref_small_np = _rgb_uint8_to_float(_pil_to_np_rgb(ref_small))
      # Use OpenCV for faster Lab conversion
      ref_lab = _rgb_to_lab_cv(ref_small_np)
      ref_mean, ref_std = _lab_stats(ref_lab)
      _cache_put(_REF_LAB_CACHE, ref_key, (ref_mean, ref_std))
    else:
      ref_mean, ref_std = cached

    # Collect target bytes
    inputs: List[Tuple[int, str, bytes]] = []
    idx = 0
    for f in files:
      data = await f.read()
      if not data:
        idx += 1
        continue
      inputs.append((idx, f.filename or f"image_{idx}.jpg", data))
      idx += 1

    if not inputs:
      return JSONResponse({"error": "No images processed"}, status_code=400)

    # Workers
    cpu = os.cpu_count() or 2
    max_workers = min(max(1, cpu), max(1, len(inputs)))

    results: List[Tuple[int, str, bytes]] = []

    if len(inputs) == 1 or max_workers == 1:
      for i, name, blob in inputs:
        out_name, out_bytes = _process_blob_reinhard(
          blob, name, ref_mean, ref_std, fmt or 'jpg', float(quality or 0.92),
          1600 if (preview and len(inputs) == 1) else None,
        )
        results.append((i, out_name, out_bytes))
    else:
      with cf.ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = []
        for i, name, blob in inputs:
          futures.append((
            i, name,
            ex.submit(
              _process_blob_reinhard,
              blob, name, ref_mean, ref_std, fmt or 'jpg', float(quality or 0.92),
              1600 if (preview and len(inputs) == 1) else None,
            )
          ))
        for i, name, fut in futures:
          try:
            out_name, out_bytes = fut.result()
            results.append((i, out_name, out_bytes))
          except Exception as exn:
            logger.exception(f"Processing failed for {name}: {exn}")
            continue

    if not results:
      return JSONResponse({"error": "No images processed"}, status_code=400)

    results.sort(key=lambda t: t[0])

    if len(results) == 1:
      _, name, data = results[0]
      media = 'image/jpeg' if name.lower().endswith('.jpg') or name.lower().endswith('.jpeg') else 'image/png'
      headers = {
        "Content-Disposition": f"attachment; filename={name}",
        "Access-Control-Expose-Headers": "Content-Disposition",
      }
      return StreamingResponse(io.BytesIO(data), media_type=media, headers=headers)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
      used_names: set[str] = set()
      def _unique_name(n: str) -> str:
        base, ext = os.path.splitext(n)
        cand = n
        i = 1
        while cand in used_names:
          cand = f"{base}_{i}{ext}"
          i += 1
        used_names.add(cand)
        return cand
      for _, name, data in results:
        zf.writestr(_unique_name(name), data)
    zip_buf.seek(0)
    headers = {
      "Content-Disposition": "attachment; filename=styled_batch.zip",
      "Access-Control-Expose-Headers": "Content-Disposition",
    }
    return StreamingResponse(zip_buf, media_type='application/zip', headers=headers)

  except Exception as ex:
    logger.exception(f"Reinhard matching failed: {ex}")
    return JSONResponse({"error": str(ex)}, status_code=500)
