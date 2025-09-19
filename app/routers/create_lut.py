from fastapi import APIRouter, UploadFile, File, Form, Request
from starlette.responses import StreamingResponse
from typing import Optional, Tuple, List, Dict, Any
import io

import numpy as np
from PIL import Image
import torch
import torch.nn.functional as F

# Optional: PyLUT for generating .cube files from programmatic transforms
try:
    from pylut import LUT3D  # type: ignore
except Exception:
    try:
        from pylut.lut import LUT3D  # type: ignore
    except Exception:
        LUT3D = None  # handled at runtime

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.config import logger
from app.utils.storage import read_json_key, write_json_key

router = APIRouter(prefix="/api/style/lut", tags=["create-lut"])  # exposes /generate and /preview


# -----------------------------
# Billing helpers (one-free usage)
# -----------------------------
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


# -----------------------------
# LUT application helpers (Torch-based preview)
# -----------------------------

def to_torch_lut(
    volume: np.ndarray,
    domain_min: Tuple[float, float, float] | torch.Tensor,
    domain_max: Tuple[float, float, float] | torch.Tensor,
    device: torch.device,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Convert LUT numpy volume [S,S,S,3] to a torch tensor [1,3,S,S,S] (N,C,D,H,W)
    and return (lut_volume, domain_min_tensor, domain_max_tensor).

    Convention used here:
      - The three spatial axes (D,H,W) correspond to (R,G,B) respectively.
      - grid_sample for 5D expects grid[..., (x,y,z)] mapping to (W,H,D) = (B,G,R).
    """
    vol_th = torch.from_numpy(volume).to(device=device, dtype=torch.float32)  # [S,S,S,3]
    vol_th = vol_th.permute(3, 0, 1, 2).contiguous()  # [3,S,S,S]
    vol_th = vol_th.unsqueeze(0)  # [1,3,S,S,S]

    if isinstance(domain_min, torch.Tensor):
        dm = domain_min.to(device=device, dtype=torch.float32)
    else:
        dm = torch.tensor(domain_min, device=device, dtype=torch.float32)
    if isinstance(domain_max, torch.Tensor):
        dM = domain_max.to(device=device, dtype=torch.float32)
    else:
        dM = torch.tensor(domain_max, device=device, dtype=torch.float32)

    return vol_th, dm, dM


def apply_lut_image(
    img: Image.Image,
    lut_volume: torch.Tensor,
    domain_min: torch.Tensor,
    domain_max: torch.Tensor,
    strength: float,
    device: torch.device,
) -> Image.Image:
    """
    Apply a 3D LUT to an image using grid_sample. Works on CPU or GPU.
    """
    if img.mode != 'RGB':
        img = img.convert('RGB')

    np_img = np.asarray(img, dtype=np.float32) / 255.0  # [H,W,3]
    h, w = np_img.shape[:2]

    # [1,3,H,W]
    th_img = torch.from_numpy(np_img).to(device=device, dtype=torch.float32)
    th_img = th_img.permute(2, 0, 1).unsqueeze(0)

    # Normalize RGB into [0,1] within DOMAIN_MIN..DOMAIN_MAX, then map to [-1,1]
    rgb = th_img.permute(0, 2, 3, 1)  # [1,H,W,3]
    dm = domain_min.view(1, 1, 1, 3)
    dM = domain_max.view(1, 1, 1, 3)
    rgb_norm = torch.clamp((rgb - dm) / torch.clamp(dM - dm, min=1e-6), 0.0, 1.0)

    # Build 5D grid for sampling the LUT volume (N,C,D,H,W) with grid (N,D_out,H_out,W_out,3)
    grid5d = torch.empty((1, 1, h, w, 3), device=device, dtype=th_img.dtype)
    grid5d[..., 0] = rgb_norm[..., 2] * 2.0 - 1.0  # x (W) <- B
    grid5d[..., 1] = rgb_norm[..., 1] * 2.0 - 1.0  # y (H) <- G
    grid5d[..., 2] = rgb_norm[..., 0] * 2.0 - 1.0  # z (D) <- R

    sampled = F.grid_sample(
        lut_volume,
        grid5d,
        mode='bilinear',
        padding_mode='border',
        align_corners=True,
    )  # [1,3,1,H,W]
    sampled = sampled.squeeze(2)  # [1,3,H,W]

    k = float(max(0.0, min(1.0, strength)))
    out = th_img.lerp(sampled, k)  # [1,3,H,W]
    out = torch.clamp(out, 0.0, 1.0)

    out_np = (out.squeeze(0).permute(1, 2, 0).detach().cpu().numpy() * 255.0).astype(np.uint8)
    return Image.fromarray(out_np, mode='RGB')


# -----------------------------
# Settings -> LUT helpers (for generation/preview)
# -----------------------------

def _eval_curve(points: List[Dict[str, float]], x: float) -> float:
    if not points:
        return x
    pts = sorted(points, key=lambda p: p['x'])
    if x <= pts[0]['x']:
        return pts[0]['y']
    if x >= pts[-1]['x']:
        return pts[-1]['y']
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if a['x'] <= x <= b['x']:
            t = (x - a['x']) / max(1e-6, (b['x'] - a['x']))
            return a['y'] * (1 - t) + b['y'] * t
    return x


def _apply_settings_to_rgb(r: float, g: float, b: float, s: Dict[str, Any]) -> Tuple[float, float, float]:
    # exposure (EV)
    k_exp = 2.0 ** float(s.get('exposure', 0.0))
    r *= k_exp; g *= k_exp; b *= k_exp

    # contrast around mid-grey 0.5
    c = float(s.get('contrast', 1.0))
    r = 0.5 + (r - 0.5) * c
    g = 0.5 + (g - 0.5) * c
    b = 0.5 + (b - 0.5) * c

    # gamma (use primaries.gamma if provided)
    gamma = float(s.get('gamma', 1.0))
    try:
        prim = s.get('primaries') or {}
        pg = float(prim.get('gamma', gamma)) if isinstance(prim, dict) else gamma
        gamma = pg
    except Exception:
        pass
    gamma = max(0.01, gamma)
    inv_g = 1.0 / gamma
    r = r ** inv_g; g = g ** inv_g; b = b ** inv_g

    # HSV-like hue/sat/vibrance approximation via HSL
    hue = float(s.get('hue', 0.0))
    sat = float(s.get('saturation', 1.0))
    vib = float(s.get('vibrance', 1.0))

    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2.0
    d = mx - mn
    if d == 0:
        h = 0.0; s_hsl = 0.0
    else:
        s_hsl = d / (1 - abs(2 * l - 1) + 1e-6)
        if mx == r:
            h = ((g - b) / (d + 1e-6)) % 6
        elif mx == g:
            h = (b - r) / (d + 1e-6) + 2
        else:
            h = (r - g) / (d + 1e-6) + 4
        h *= 60

    # apply hue shift
    h = (h + hue) % 360

    # apply saturation/vibrance (vibrance boosts more when saturation is low)
    s_boost = sat * (1 + (vib - 1) * (1 - s_hsl))
    s_hsl = max(0.0, min(1.0, s_hsl * s_boost))

    # back to RGB
    c_h = (1 - abs(2 * l - 1)) * s_hsl
    x_h = c_h * (1 - abs(((h / 60) % 2) - 1))
    m = l - c_h / 2

    if 0 <= h < 60:
        rp, gp, bp = c_h, x_h, 0.0
    elif 60 <= h < 120:
        rp, gp, bp = x_h, c_h, 0.0
    elif 120 <= h < 180:
        rp, gp, bp = 0.0, c_h, x_h
    elif 180 <= h < 240:
        rp, gp, bp = 0.0, x_h, c_h
    elif 240 <= h < 300:
        rp, gp, bp = x_h, 0.0, c_h
    else:
        rp, gp, bp = c_h, 0.0, x_h

    r = rp + m; g = gp + m; b = bp + m

    # curves
    curves = s.get('curves', {})
    r = _eval_curve(curves.get('r', [{'x': 0, 'y': 0}, {'x': 1, 'y': 1}]), r)
    g = _eval_curve(curves.get('g', [{'x': 0, 'y': 0}, {'x': 1, 'y': 1}]), g)
    b = _eval_curve(curves.get('b', [{'x': 0, 'y': 0}, {'x': 1, 'y': 1}]), b)
    mcurve = curves.get('master', [{'x': 0, 'y': 0}, {'x': 1, 'y': 1}])
    r = _eval_curve(mcurve, r); g = _eval_curve(mcurve, g); b = _eval_curve(mcurve, b)

    # clamp
    r = float(max(0.0, min(1.0, r)))
    g = float(max(0.0, min(1.0, g)))
    b = float(max(0.0, min(1.0, b)))
    return r, g, b


def _build_lut_volume_from_settings(settings: Dict[str, Any], size: int = 33) -> Tuple[np.ndarray, Tuple[float, float, float], Tuple[float, float, float]]:
    """
    Build a 3D LUT volume [S,S,S,3] in [0,1] by evaluating _apply_settings_to_rgb
    across a uniform grid in [0,1]^3. Returns (volume, domain_min, domain_max).
    """
    try:
        s = int(settings.get('resolution') or size)
        size = s if s in (17, 33, 65) else size
    except Exception:
        size = size

    vol = np.zeros((size, size, size, 3), dtype=np.float32)
    grid = np.linspace(0.0, 1.0, size, dtype=np.float32)

    for ri, r in enumerate(grid):
        for gi, g in enumerate(grid):
            for bi, b in enumerate(grid):
                rr, gg, bb = _apply_settings_to_rgb(float(r), float(g), float(b), settings)
                vol[ri, gi, bi, 0] = rr
                vol[ri, gi, bi, 1] = gg
                vol[ri, gi, bi, 2] = bb

    return vol, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)


# -----------------------------
# API routes
# -----------------------------

@router.post('/generate')
async def lut_generate(request: Request, payload: Dict[str, Any]):
    """
    Generate a .cube LUT from UI settings using PyLUT and return as a downloadable file.
    Expects payload with keys: resolution, exposure, contrast, gamma, hue, saturation,
    vibrance, curves{r,g,b,master}.
    """
    # Auth
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
        size = size if size in (17, 33, 65) else 33

        def map_fn(r: float, g: float, b: float):
            rr, gg, bb = _apply_settings_to_rgb(float(r), float(g), float(b), payload)
            return rr, gg, bb

        lut = LUT3D.from_func(size=size, func=map_fn)
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


@router.post('/preview')
@router.post('/preview-image')
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
        raw_settings = await settings.read()
        try:
            import json as _json
            payload = _json.loads(raw_settings.decode('utf-8', errors='ignore')) if raw_settings else {}
        except Exception:
            payload = {}

        img_part = file or image
        if not img_part:
            return {"error": "no_image", "message": "Upload an image as 'file' or 'image'"}
        img_bytes = await img_part.read()
        if not img_bytes:
            return {"error": "empty_image"}

        img = Image.open(io.BytesIO(img_bytes)).convert('RGB')

        # Build LUT from settings and apply
        vol_np, dmin, dmax = _build_lut_volume_from_settings(payload, int(payload.get('resolution') or 33))
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        vol_th, dm_th, dM_th = to_torch_lut(vol_np, dmin, dmax, device)
        out = apply_lut_image(img, vol_th, dm_th, dM_th, strength=1.0, device=device)

        buf = io.BytesIO()
        out.save(buf, format='PNG')
        buf.seek(0)
        headers = {"Access-Control-Expose-Headers": "Content-Disposition"}
        return StreamingResponse(buf, media_type='image/png', headers=headers)
    except Exception as ex:
        logger.exception(f"LUT preview failed: {ex}")
        return {"error": str(ex)}
