from fastapi import APIRouter, UploadFile, File, Form, Request
from starlette.responses import StreamingResponse
from typing import Optional, Tuple
import io
import os

import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.config import logger
from app.utils.storage import read_json_key, write_json_key

router = APIRouter(prefix="/api/style", tags=["style"])

# ---- One-free-generation helpers ----
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


def parse_cube_lut(text: str) -> Tuple[np.ndarray, Tuple[float, float, float], Tuple[float, float, float]]:
    """
    Parse .cube (3D LUT) text into a numpy array of shape [S, S, S, 3] with values in 0..1,
    along with DOMAIN_MIN and DOMAIN_MAX.
    """
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l and not l.startswith('#')]

    size = 0
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    values = []

    for line in lines:
        if line.startswith('TITLE'):
            continue
        if line.startswith('LUT_3D_SIZE'):
            parts = line.split()
            if len(parts) >= 2:
                size = int(parts[1])
            continue
        if line.startswith('DOMAIN_MIN'):
            parts = line.split()
            if len(parts) >= 4:
                domain_min = (float(parts[1]), float(parts[2]), float(parts[3]))
            continue
        if line.startswith('DOMAIN_MAX'):
            parts = line.split()
            if len(parts) >= 4:
                domain_max = (float(parts[1]), float(parts[2]), float(parts[3]))
            continue
        parts = line.split()
        if len(parts) == 3:
            r, g, b = float(parts[0]), float(parts[1]), float(parts[2])
            values.append([r, g, b])

    if size <= 1:
        raise ValueError('Invalid or missing LUT_3D_SIZE')
    expected = size * size * size
    if len(values) != expected:
        raise ValueError(f'Invalid LUT data length: got {len(values)}, expected {expected}')

    arr = np.asarray(values, dtype=np.float32)
    arr = arr.reshape((size, size, size, 3))  # Assume order R-major then G then B as common in .cube
    return arr, domain_min, domain_max


def to_torch_lut(volume: np.ndarray, domain_min: Tuple[float, float, float], domain_max: Tuple[float, float, float], device: torch.device) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Convert LUT numpy volume [S,S,S,3] to torch tensor [1,3,S,S,S] and provide min/max for mapping.
    """
    # Rearrange to [C,D,H,W] after setting D=R, H=G, W=B
    vol_th = torch.from_numpy(volume).to(device)  # [S,S,S,3]
    vol_th = vol_th.permute(3, 0, 1, 2).contiguous()  # [3,S,S,S]
    vol_th = vol_th.unsqueeze(0)  # [1,3,S,S,S]
    dm = torch.tensor(domain_min, dtype=torch.float32, device=device)
    dM = torch.tensor(domain_max, dtype=torch.float32, device=device)
    return vol_th, dm, dM


def apply_lut_gpu(img: Image.Image, lut_volume: torch.Tensor, domain_min: torch.Tensor, domain_max: torch.Tensor, strength: float, device: torch.device) -> Image.Image:
    if img.mode != 'RGB':
        img = img.convert('RGB')
    np_img = np.asarray(img, dtype=np.float32) / 255.0  # [H,W,3]
    h, w = np_img.shape[:2]

    # To torch [1,3,H,W]
    th_img = torch.from_numpy(np_img).to(device)
    th_img = th_img.permute(2, 0, 1).unsqueeze(0)  # [1,3,H,W]

    # Build 3D grid [1,H,W,3] in LUT index space normalized to [-1,1]
    # Map RGB from DOMAIN_MIN..DOMAIN_MAX -> 0..1 -> [-1,1]
    dm = domain_min.view(1, 1, 1, 3)
    dM = domain_max.view(1, 1, 1, 3)
    rgb = th_img.permute(0, 2, 3, 1)  # [1,H,W,3]
    rgb_norm = torch.clamp((rgb - dm) / torch.clamp(dM - dm, min=1e-6), 0.0, 1.0)
    grid = rgb_norm * 2.0 - 1.0  # [-1,1]

    # grid_sample expects input [N,C,D,H,W] and grid [N, outD, outH, outW, 3]
    # We want output size [H,W] sampled from LUT volume [S,S,S]
    # So outD=H, outH=W, outW=1 is NOT correct. Instead, use trick: use grid_sample with 5D but provide a 2D grid by unsqueezing one dim
    # Easiest is to reshape to [1,H,W,3] and use F.grid_sample with 5D by unsqueezing a dummy dimension.
    # Create dummy depth dimension of size 1 and sample with grid of shape [1,1,H,W,3]
    grid5d = grid.unsqueeze(1)  # [1,1,H,W,3]
    # Sample
    sampled = F.grid_sample(lut_volume, grid5d, mode='bilinear', padding_mode='border', align_corners=True)  # [1,3,1,H,W]
    sampled = sampled.squeeze(2)  # [1,3,H,W]

    k = float(max(0.0, min(1.0, strength)))
    out = th_img + (sampled - th_img) * k  # [1,3,H,W]
    out = torch.clamp(out, 0.0, 1.0)
    out_np = (out.squeeze(0).permute(1, 2, 0).detach().cpu().numpy() * 255.0).astype(np.uint8)
    return Image.fromarray(out_np, mode='RGB')


@router.post('/lut-apply')
async def lut_apply(
    request: Request,
    file: UploadFile = File(...),
    lut: UploadFile = File(...),
    intensity: float = Form(1.0),
    fmt: str = Form('png'),
    quality: Optional[float] = Form(0.92),
):
    """Apply a provided .cube LUT to an image using GPU if available, CPU otherwise.
    Returns the processed image as PNG/JPEG.
    """
    # AuthN/AuthZ similar to convert endpoint
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return {"error": "Unauthorized"}
    if not has_role_access(req_uid, eff_uid, 'convert'):
        return {"error": "Forbidden"}

    # One-free-generation enforcement (counts against owner workspace)
    billing_uid = eff_uid or _billing_uid_from_request(request)
    if not _is_paid_customer(billing_uid):
        if not _consume_one_free(billing_uid, 'style_lut'):
            return {"error": "free_limit_reached", "message": "You have used your free generation. Upgrade to continue."}

    raw = await file.read()
    lut_text = (await lut.read()).decode('utf-8', errors='ignore')
    if not raw:
        return {"error": "empty file"}
    if not lut_text:
        return {"error": "empty lut"}

    try:
        img = Image.open(io.BytesIO(raw)).convert('RGB')
        vol_np, dmin, dmax = parse_cube_lut(lut_text)
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        vol_th, dm_th, dM_th = to_torch_lut(vol_np, dmin, dmax, device)
        out = apply_lut_gpu(img, vol_th, dm_th, dM_th, float(intensity), device)

        buf = io.BytesIO()
        f = (fmt or 'png').lower()
        if f in ('jpg', 'jpeg'):
            q = int(max(1, min(100, round((quality or 0.92) * 100))))
            out.save(buf, format='JPEG', quality=q, subsampling=0, progressive=True, optimize=True)
            ct = 'image/jpeg'
        else:
            out.save(buf, format='PNG')
            ct = 'image/png'
        buf.seek(0)
        headers = {"Access-Control-Expose-Headers": "Content-Disposition"}
        return StreamingResponse(buf, media_type=ct, headers=headers)
    except Exception as ex:
        logger.exception(f"LUT apply failed: {ex}")
        return {"error": str(ex)}