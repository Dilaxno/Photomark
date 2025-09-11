from collections import OrderedDict
import io, os, hashlib, zipfile
import concurrent.futures as cf
import numpy as np
import cv2
from fastapi import APIRouter, UploadFile, File, Form, Request
from starlette.responses import StreamingResponse, JSONResponse
from typing import List, Optional, Tuple
from datetime import datetime as _dt
from PIL import Image

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.config import logger
from app.utils.storage import read_json_key, write_json_key

router = APIRouter(prefix="/api/style", tags=["style"])

# ---- Tiny in-memory caches ----
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

# ---------- Billing helpers ----------
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

# ---------- Utils ----------
def _downscale_cv2(img: np.ndarray, target: int = 512) -> np.ndarray:
  h, w = img.shape[:2]
  if max(h, w) <= target:
    return img
  scale = target / float(max(h, w))
  new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
  return cv2.resize(img, new_size, interpolation=cv2.INTER_AREA)

def _lab_stats(lab: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
  mean = lab.reshape(-1, 3).mean(axis=0)
  std = lab.reshape(-1, 3).std(axis=0)
  std = np.where(std < 1e-6, 1.0, std)
  return mean, std

def _reinhard_apply_cv2(img_bgr: np.ndarray, ref_mean: np.ndarray, ref_std: np.ndarray, preview_max_side: int|None=None) -> np.ndarray:
  # Convert to Lab
  lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
  mean, std = _lab_stats(lab)
  out = (lab - mean) / std * ref_std + ref_mean
  out[..., 0] = np.clip(out[..., 0], 0, 100)
  out[..., 1:] = np.clip(out[..., 1:], -127, 127)
  out = out.astype(np.float32)
  rgb = cv2.cvtColor(out, cv2.COLOR_LAB2BGR)
  return np.clip(rgb, 0, 255).astype(np.uint8)

def _encode_img_cv2(img_bgr: np.ndarray, fmt: str, quality: float) -> Tuple[str, bytes]:
  fmt = (fmt or 'jpg').lower()
  if fmt in ('jpg', 'jpeg'):
    q = int(max(1, min(100, round((quality or 0.92) * 100))))
    ok, buf = cv2.imencode('.jpg', img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    ext = 'jpg'
  else:
    ok, buf = cv2.imencode('.png', img_bgr)
    ext = 'png'
  if not ok:
    raise RuntimeError("Encoding failed")
  return ext, buf.tobytes()

def _process_blob_reinhard_fast(blob: bytes, filename: str, ref_mean: np.ndarray, ref_std: np.ndarray, fmt: str, quality: float, preview_max_side: int|None=None) -> Tuple[str, bytes]:
  arr = np.frombuffer(blob, np.uint8)
  img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
  if img_bgr is None:
    raise ValueError("Invalid image")
  if preview_max_side and max(img_bgr.shape[:2]) > preview_max_side:
    img_bgr = _downscale_cv2(img_bgr, preview_max_side)
  out_bgr = _reinhard_apply_cv2(img_bgr, ref_mean, ref_std)
  ext, data = _encode_img_cv2(out_bgr, fmt, quality)
  base = os.path.splitext(filename or 'image')[0]
  name = f"{base}_styled.{ext}"
  return name, data

# ---------- Fast Reinhard endpoint ----------
@router.post('/reinhard')
async def reinhard_match(
  request: Request,
  reference: UploadFile = File(...),
  files: List[UploadFile] = File(...),
  fmt: Optional[str] = Form('jpg'),
  quality: Optional[float] = Form(0.92),
  preview: Optional[int] = Form(0),
):
  eff_uid, req_uid = resolve_workspace_uid(request)
  if not eff_uid or not req_uid:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
  if not has_role_access(req_uid, eff_uid, 'convert'):
    return JSONResponse({"error": "Forbidden"}, status_code=403)

  billing_uid = eff_uid or _billing_uid_from_request(request)
  if not _is_paid_customer(billing_uid):
    if not _consume_one_free(billing_uid, 'style_transfer'):
      return JSONResponse({"error": "free_limit_reached", "message": "You have used your free generation. Upgrade to continue."}, status_code=402)

  try:
    ref_bytes = await reference.read()
    if not ref_bytes:
      return JSONResponse({"error": "empty reference"}, status_code=400)
    ref_key = hashlib.sha1(ref_bytes).hexdigest()
    cached = _cache_get(_REF_LAB_CACHE, ref_key)
    if cached is None:
      arr = np.frombuffer(ref_bytes, np.uint8)
      ref_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
      if ref_bgr is None:
        return JSONResponse({"error": "Invalid reference image"}, status_code=400)
      ref_small = _downscale_cv2(ref_bgr, 512)
      ref_lab = cv2.cvtColor(ref_small, cv2.COLOR_BGR2LAB).astype(np.float32)
      ref_mean, ref_std = _lab_stats(ref_lab)
      _cache_put(_REF_LAB_CACHE, ref_key, (ref_mean, ref_std))
    else:
      ref_mean, ref_std = cached

    inputs: List[Tuple[int, str, bytes]] = []
    for idx, f in enumerate(files):
      data = await f.read()
      if data:
        inputs.append((idx, f.filename or f"image_{idx}.jpg", data))
    if not inputs:
      return JSONResponse({"error": "No images processed"}, status_code=400)

    results: List[Tuple[int, str, bytes]] = []
    cpu = os.cpu_count() or 2
    max_workers = min(max(1, cpu), len(inputs))
    
    # Thread pool (faster startup than processes)
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
      futures = [(
        i, name,
        ex.submit(
          _process_blob_reinhard_fast,
          blob, name, ref_mean, ref_std,
          fmt or 'jpg', float(quality or 0.92),
          1600 if (preview and len(inputs) == 1) else None
        )
      ) for i, name, blob in inputs]
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
      media = 'image/jpeg' if name.lower().endswith('.jpg') else 'image/png'
      headers = {"Content-Disposition": f"attachment; filename={name}", "Access-Control-Expose-Headers": "Content-Disposition"}
      return StreamingResponse(io.BytesIO(data), media_type=media, headers=headers)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
      for _, name, data in results:
        zf.writestr(name, data)
    zip_buf.seek(0)
    headers = {"Content-Disposition": "attachment; filename=styled_batch.zip", "Access-Control-Expose-Headers": "Content-Disposition"}
    return StreamingResponse(zip_buf, media_type='application/zip', headers=headers)

  except Exception as ex:
    logger.exception(f"Reinhard matching failed: {ex}")
    return JSONResponse({"error": str(ex)}, status_code=500)
