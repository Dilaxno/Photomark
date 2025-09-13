from fastapi import APIRouter, UploadFile, File, Form, Request

from starlette.responses import StreamingResponse
from typing import Optional, Tuple, List, Dict, Any
import io
import os

import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F

# PyLUT for generating .cube files from programmatic transforms
try:
    # Newer versions publish as PyLUT with class LUT3D in pylut
    from pylut import LUT3D
except Exception:
    try:
        # Some environments expose it under pylut.lut
        from pylut.lut import LUT3D  # type: ignore
    except Exception:
        LUT3D = None  # handled at runtime

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.config import logger
from app.utils.storage import read_json_key, write_json_key

router = APIRouter(prefix="/api/style", tags=["style"])  # includes /lut-apply and /lut/generate

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


def _eval_curve(points: List[Dict[str, float]], x: float) -> float:
    # Linear interpolation between sorted points
    if not points:
        return x
    pts = sorted(points, key=lambda p: p['x'])
    if x <= pts[0]['x']:
        return pts[0]['y']
    if x >= pts[-1]['x']:
        return pts[-1]['y']
    for i in range(len(pts)-1):
        a, b = pts[i], pts[i+1]
        if a['x'] <= x <= b['x']:
            t = (x - a['x']) / max(1e-6, (b['x'] - a['x']))
            return a['y'] * (1-t) + b['y'] * t
    return x


def _apply_settings_to_rgb(r: float, g: float, b: float, s: Dict[str, Any]) -> Tuple[float, float, float]:
    # exposure (EV)
    k_exp = 2.0 ** float(s.get('exposure', 0.0))
    r *= k_exp; g *= k_exp; b *= k_exp
    # contrast
    c = float(s.get('contrast', 1.0))
    r = 0.5 + (r - 0.5) * c
    g = 0.5 + (g - 0.5) * c
    b = 0.5 + (b - 0.5) * c
    # gamma
    gamma = max(0.01, float(s.get('gamma', 1.0)))
    r = r ** (1.0 / gamma); g = g ** (1.0 / gamma); b = b ** (1.0 / gamma)
    # hue/sat/vibrance (simple HSV-like approx)
    hue = float(s.get('hue', 0.0))
    sat = float(s.get('saturation', 1.0))
    vib = float(s.get('vibrance', 1.0))
    # convert to HSL
    mx, mn = max(r,g,b), min(r,g,b)
    l = (mx + mn) / 2.0
    d = mx - mn
    if d == 0:
        h = 0.0; s_hsl = 0.0
    else:
        s_hsl = d / (1 - abs(2*l - 1) + 1e-6)
        if mx == r:
            h = ((g - b) / (d + 1e-6)) % 6
        elif mx == g:
            h = (b - r) / (d + 1e-6) + 2
        else:
            h = (r - g) / (d + 1e-6) + 4
        h *= 60
    # apply hue
    h = (h + hue) % 360
    # apply sat/vibrance (vibrance boosts more when saturation is low)
    s_boost = sat * (1 + (vib - 1) * (1 - s_hsl))
    s_hsl = max(0.0, min(1.0, s_hsl * s_boost))
    # back to RGB
    c_h = (1 - abs(2*l - 1)) * s_hsl
    x_h = c_h * (1 - abs(((h/60) % 2) - 1))
    m = l - c_h/2
    rp=gp=bp=0.0
    if 0<=h<60: rp=c_h; gp=x_h; bp=0
    elif 60<=h<120: rp=x_h; gp=c_h; bp=0
    elif 120<=h<180: rp=0; gp=c_h; bp=x_h
    elif 180<=h<240: rp=0; gp=x_h; bp=c_h
    elif 240<=h<300: rp=x_h; gp=0; bp=c_h
    else: rp=c_h; gp=0; bp=x_h
    r = rp + m; g = gp + m; b = bp + m
    # curves
    curves = s.get('curves', {})
    r = _eval_curve(curves.get('r', [{'x':0,'y':0},{'x':1,'y':1}]), r)
    g = _eval_curve(curves.get('g', [{'x':0,'y':0},{'x':1,'y':1}]), g)
    b = _eval_curve(curves.get('b', [{'x':0,'y':0},{'x':1,'y':1}]), b)
    mcurve = curves.get('master', [{'x':0,'y':0},{'x':1,'y':1}])
    r = _eval_curve(mcurve, r); g = _eval_curve(mcurve, g); b = _eval_curve(mcurve, b)
    # clamp
    r = float(max(0.0, min(1.0, r)))
    g = float(max(0.0, min(1.0, g)))
    b = float(max(0.0, min(1.0, b)))
    return r, g, b


def _build_lut_volume_from_settings(settings: Dict[str, Any], size: int = 33) -> Tuple[np.ndarray, Tuple[float, float, float], Tuple[float, float, float]]:
    """
    Build a 3D LUT volume [S,S,S,3] in 0..1 by evaluating _apply_settings_to_rgb
    at grid points in RGB domain [0..1]. Returns (volume, domain_min, domain_max).
    """
    try:
        s = int(settings.get('resolution') or size)
        size = s if s in (17, 33, 65) else size
    except Exception:
        size = size
    vol = np.zeros((size, size, size, 3), dtype=np.float32)
    # Grid over 0..1 inclusive
    grid = np.linspace(0.0, 1.0, size, dtype=np.float32)
    # Iterate over grid; small sizes keep this acceptable
    for ri, r in enumerate(grid):
        for gi, g in enumerate(grid):
            for bi, b in enumerate(grid):
                rr, gg, bb = _apply_settings_to_rgb(float(r), float(g), float(b), settings)
                vol[ri, gi, bi, 0] = rr
                vol[ri, gi, bi, 1] = gg
                vol[ri, gi, bi, 2] = bb
    return vol, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)


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


@router.post('/lut/generate')
async def lut_generate(request: Request, payload: Dict[str, Any]):
    """
    Generate a .cube LUT from UI settings using PyLUT and return as a downloadable file.
    Expects payload with keys: resolution, exposure, contrast, gamma, hue, saturation, vibrance, curves{r,g,b,master}.
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

    if LUT3D is None:
        return {"error": "PyLUT not installed"}

    try:
        size = int(payload.get('resolution') or 33)
        size = size if size in (17,33,65) else 33
        # Create identity LUT
        # LUT3D.from_func expects a mapping function f(r,g,b)->(r,g,b) over [0..1]
        def map_fn(r: float, g: float, b: float):
            rr, gg, bb = _apply_settings_to_rgb(float(r), float(g), float(b), payload)
            return rr, gg, bb
        lut = LUT3D.from_func(size=size, func=map_fn)
        # Serialize to .cube text
        cube_text = lut.to_cube()
        buf = io.BytesIO(cube_text.encode('utf-8'))
        headers = {
            'Content-Disposition': 'attachment; filename="custom.cube"',
            'Access-Control-Expose-Headers': 'Content-Disposition',
        }
        return StreamingResponse(buf, media_type='text/plain', headers=headers)
    except Exception as ex:
        logger.exception(f"LUT generate failed: {ex}")
        return {"error": str(ex)}


@router.post('/lut/preview')
@router.post('/lut/preview-image')
async def lut_preview(
    request: Request,
    file: Optional[UploadFile] = File(None),
    image: Optional[UploadFile] = File(None),
    settings: UploadFile = File(...),
):
    """
    Server-side preview: apply the UI settings directly to an uploaded image.
    Accepts multipart form-data with fields:
      - file or image: the image to preview
      - settings: a JSON blob containing the settings (same schema as generate)
    Returns a PNG image.
    """
    try:
        # Parse settings JSON (sent as a Blob part)
        raw_settings = await settings.read()
        try:
            import json as _json
            payload = _json.loads(raw_settings.decode('utf-8', errors='ignore')) if raw_settings else {}
        except Exception:
            payload = {}

        # Pick the image file
        img_part = file or image
        if not img_part:
            return {"error": "no_image", "message": "Upload an image as 'file' or 'image'"}
        img_bytes = await img_part.read()
        if not img_bytes:
            return {"error": "empty_image"}

        # Open image
        img = Image.open(io.BytesIO(img_bytes)).convert('RGB')

        # Build a temporary LUT volume from settings and apply via GPU/CPU sampler
        vol_np, dmin, dmax = _build_lut_volume_from_settings(payload, int(payload.get('resolution') or 33))
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        vol_th, dm_th, dM_th = to_torch_lut(vol_np, torch.tensor(dmin), torch.tensor(dmax), device)
        out = apply_lut_gpu(img, vol_th, dm_th, dM_th, strength=1.0, device=device)

        # Encode PNG
        buf = io.BytesIO()
        out.save(buf, format='PNG')
        buf.seek(0)
        headers = {"Access-Control-Expose-Headers": "Content-Disposition"}
        return StreamingResponse(buf, media_type='image/png', headers=headers)
    except Exception as ex:
        logger.exception(f"LUT preview failed: {ex}")
        return {"error": str(ex)}