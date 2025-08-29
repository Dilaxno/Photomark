import os
import io
import math
from typing import Dict, Tuple, Optional, List

import numpy as np
from PIL import Image

try:
    import torch  # type: ignore
    TORCH_AVAILABLE = True
except Exception:
    TORCH_AVAILABLE = False

from app.core.config import logger

# In-memory cache
_LUTS: Dict[str, np.ndarray] = {}
_TEXTURES_2D: Dict[str, bytes] = {}


def parse_cube(path: str) -> Tuple[np.ndarray, int]:
    """
    Parse a .cube LUT file into a numpy array of shape (N, N, N, 3), values in [0,1].
    Returns (lut, size N).
    """
    size = None
    domain_min = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    domain_max = np.array([1.0, 1.0, 1.0], dtype=np.float32)
    rows: List[List[float]] = []
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith('#'):
                continue
            up = s.upper()
            if up.startswith('TITLE'):
                continue
            if up.startswith('DOMAIN_MIN'):
                parts = s.split()
                try:
                    domain_min = np.array([float(parts[-3]), float(parts[-2]), float(parts[-1])], dtype=np.float32)
                except Exception:
                    pass
                continue
            if up.startswith('DOMAIN_MAX'):
                parts = s.split()
                try:
                    domain_max = np.array([float(parts[-3]), float(parts[-2]), float(parts[-1])], dtype=np.float32)
                except Exception:
                    pass
                continue
            if up.startswith('LUT_3D_SIZE'):
                try:
                    size = int(s.split()[-1])
                except Exception:
                    size = None
                continue
            # Otherwise assume data row
            parts = s.split()
            if len(parts) >= 3:
                try:
                    rows.append([float(parts[0]), float(parts[1]), float(parts[2])])
                except Exception:
                    continue
    if not size:
        # Infer size from rows count
        nrows = len(rows)
        n = round(nrows ** (1.0 / 3.0))
        if n * n * n != nrows:
            raise ValueError(f"Invalid .cube rows count {nrows}")
        size = n
    expected = size * size * size
    if len(rows) < expected:
        raise ValueError(f".cube has insufficient rows: {len(rows)} < {expected}")

    # Normalize rows from domain to [0,1] just in case (usually already 0..1)
    rows_np = np.array(rows[:expected], dtype=np.float32)
    dom_range = (domain_max - domain_min)
    dom_range[dom_range == 0] = 1.0
    rows_np = (rows_np - domain_min[None, :]) / dom_range[None, :]
    rows_np = np.clip(rows_np, 0.0, 1.0)

    # Arrange into (r,g,b) order consistent with common .cube: b fastest, then g, then r.
    # Many .cube files index as for r in 0..N-1: for g in 0..N-1: for b in 0..N-1: write RGB
    lut = rows_np.reshape((size, size, size, 3))
    return lut, size


def to_2d_texture(lut: np.ndarray) -> bytes:
    """
    Convert a 3D LUT (N,N,N,3) into a 2D texture PNG commonly used by shaders.
    Layout: a grid of N tiles horizontally, each tile is N x N for varying blue channel,
    rows for green, columns for red slices stacked.
    Resulting image size: (N * N, N) where width = N*N, height = N.
    """
    N = lut.shape[0]
    # Create (H, W, 3) with H=N, W=N*N
    tex = np.zeros((N, N * N, 3), dtype=np.float32)
    # For each r in 0..N-1, place a tile at columns [r*N:(r+1)*N]
    for r in range(N):
        # tile: (N,N,3) where rows=g, cols=b
        tile = lut[r]  # shape (N,N,3) with [g,b]
        tex[:, r * N:(r + 1) * N, :] = tile
    # Convert to uint8
    tex8 = np.clip(tex * 255.0 + 0.5, 0, 255).astype(np.uint8)
    im = Image.fromarray(tex8, mode='RGB')
    buf = io.BytesIO()
    im.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def load_luts_from_dir(dir_path: str) -> Dict[str, np.ndarray]:
    global _LUTS, _TEXTURES_2D
    loaded: Dict[str, np.ndarray] = {}
    if not os.path.isdir(dir_path):
        logger.info("LUTs directory not found: %s", dir_path)
        return {}
    for name in os.listdir(dir_path):
        if not name.lower().endswith('.cube'):
            continue
        path = os.path.join(dir_path, name)
        key = os.path.splitext(name)[0]
        try:
            lut, n = parse_cube(path)
            loaded[key] = lut
        except Exception as ex:
            logger.warning("Failed to parse LUT %s: %s", name, ex)
    _LUTS = loaded
    _TEXTURES_2D = {k: to_2d_texture(v) for k, v in loaded.items()}
    logger.info("Loaded %d LUTs from %s", len(_LUTS), dir_path)
    return _LUTS


def get_luts() -> Dict[str, np.ndarray]:
    return _LUTS


def get_texture_2d(lut_name: str) -> Optional[bytes]:
    return _TEXTURES_2D.get(lut_name)


def _ensure_image_rgb_uint8(raw: bytes) -> np.ndarray:
    im = Image.open(io.BytesIO(raw)).convert('RGB')
    return np.asarray(im, dtype=np.uint8)


def apply_lut_numpy(raw: bytes, lut: np.ndarray) -> bytes:
    """
    Fast nearest-neighbor 3D LUT application using vectorized indexing.
    """
    img = _ensure_image_rgb_uint8(raw)
    N = lut.shape[0]
    # Normalize to 0..1
    arr = img.astype(np.float32) / 255.0
    # Scale to grid 0..N-1 and round to nearest
    idx = np.clip((arr * (N - 1)).round().astype(np.int32), 0, N - 1)
    r_idx = idx[..., 0]
    g_idx = idx[..., 1]
    b_idx = idx[..., 2]
    out = lut[r_idx, g_idx, b_idx]
    out8 = np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)
    im = Image.fromarray(out8, mode='RGB')
    buf = io.BytesIO()
    im.save(buf, format='JPEG', quality=95, subsampling=0, progressive=True, optimize=True)
    return buf.getvalue()


def apply_lut_torch(raw: bytes, lut: np.ndarray, use_cuda: bool = True) -> bytes:
    if not TORCH_AVAILABLE:
        raise RuntimeError("PyTorch not available")
    device = torch.device('cuda') if (use_cuda and torch.cuda.is_available()) else torch.device('cpu')
    # image tensor [H,W,3] float32 0..1
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    arr = torch.from_numpy(np.asarray(img, dtype=np.uint8)).to(device)
    arr = arr.to(torch.float32) / 255.0
    H, W, _ = arr.shape
    N = lut.shape[0]
    # LUT tensor [N,N,N,3]
    lut_t = torch.from_numpy(lut).to(device)
    # Indices
    idx = torch.clamp((arr * (N - 1)).round().to(dtype=torch.long), 0, N - 1)
    r = idx[..., 0]
    g = idx[..., 1]
    b = idx[..., 2]
    out = lut_t[r, g, b]
    out8 = torch.clamp(out * 255.0 + 0.5, 0, 255).to(dtype=torch.uint8).cpu().numpy()
    im = Image.fromarray(out8, mode='RGB')
    buf = io.BytesIO()
    im.save(buf, format='JPEG', quality=95, subsampling=0, progressive=True, optimize=True)
    return buf.getvalue()


def list_luts() -> List[str]:
    return sorted(_LUTS.keys())


def apply_lut(raw: bytes, lut_name: str, engine: str = 'auto') -> bytes:
    lut = _LUTS.get(lut_name)
    if lut is None:
        raise KeyError(f"LUT not found: {lut_name}")
    eng = (engine or 'auto').lower().strip()
    if eng == 'torch' or (eng == 'auto' and TORCH_AVAILABLE and (torch.cuda.is_available() if TORCH_AVAILABLE else False)):
        try:
            return apply_lut_torch(raw, lut, use_cuda=True)
        except Exception as ex:
            logger.warning("Torch path failed, falling back to numpy: %s", ex)
            return apply_lut_numpy(raw, lut)
    elif eng == 'numpy' or eng == 'auto':
        return apply_lut_numpy(raw, lut)
    elif eng == 'opencv':
        # Not implemented: OpenCV CUDA does not support 3D LUT directly
        raise NotImplementedError("OpenCV CUDA 3D LUT not implemented; use torch or numpy")
    else:
        return apply_lut_numpy(raw, lut)