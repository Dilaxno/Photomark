import io
import math
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np
import cv2
from PIL import Image

try:
    import rawpy  # Optional RAW support
except Exception:  # pragma: no cover
    rawpy = None

from app.core.config import logger
from app.utils.storage import upload_bytes


# ---- Presets ----
# Minimal, extendable preset library. Values are approximate profiles.
CAMERA_PRESETS: Dict[str, Dict[str, float]] = {
    # Full-frame
    "Sony A7III": {"sensor_width_mm": 36.0, "sensor_height_mm": 24.0, "coc_mm": 0.03},
    "Canon 5D Mark IV": {"sensor_width_mm": 36.0, "sensor_height_mm": 24.0, "coc_mm": 0.03},
    # APS-C
    "Sony A6400": {"sensor_width_mm": 23.5, "sensor_height_mm": 15.6, "coc_mm": 0.02},
    "Fujifilm X-T4": {"sensor_width_mm": 23.5, "sensor_height_mm": 15.6, "coc_mm": 0.02},
    # Micro Four Thirds
    "Panasonic GH5": {"sensor_width_mm": 17.3, "sensor_height_mm": 13.0, "coc_mm": 0.015},
}

LENS_PRESETS: Dict[str, Dict[str, float]] = {
    "35mm f/1.4": {"focal_length_mm": 35.0, "max_aperture": 1.4, "vignetting": 0.25, "distortion_k1": -0.05, "chromatic_aberration": 0.15},
    "50mm f/1.8": {"focal_length_mm": 50.0, "max_aperture": 1.8, "vignetting": 0.2, "distortion_k1": -0.02, "chromatic_aberration": 0.1},
    "85mm f/1.4": {"focal_length_mm": 85.0, "max_aperture": 1.4, "vignetting": 0.2, "distortion_k1": 0.0, "chromatic_aberration": 0.08},
    "24-70mm f/2.8 @24": {"focal_length_mm": 24.0, "max_aperture": 2.8, "vignetting": 0.3, "distortion_k1": -0.12, "chromatic_aberration": 0.2},
    "24-70mm f/2.8 @70": {"focal_length_mm": 70.0, "max_aperture": 2.8, "vignetting": 0.22, "distortion_k1": 0.02, "chromatic_aberration": 0.12},
}


@dataclass
class SimulationParams:
    # Camera
    sensor_width_mm: float
    sensor_height_mm: float
    coc_mm: float
    # Lens
    focal_length_mm: float
    aperture: float  # f-number
    focus_distance_m: float  # focus distance in meters
    # Effects strength overrides (0..1 typically)
    vignetting: float = 0.0
    chromatic_aberration: float = 0.0
    distortion_k1: float = 0.0  # barrel(-)/pincushion(+)
    bokeh_strength: Optional[float] = None  # optional override for blur intensity


# ---- Core math helpers ----

def compute_fov(sensor_width_mm: float, sensor_height_mm: float, focal_length_mm: float) -> Dict[str, float]:
    def fov(dim_mm: float) -> float:
        return 2.0 * math.degrees(math.atan((dim_mm / 2.0) / focal_length_mm))

    diagonal_mm = math.hypot(sensor_width_mm, sensor_height_mm)
    return {
        "horizontal_deg": fov(sensor_width_mm),
        "vertical_deg": fov(sensor_height_mm),
        "diagonal_deg": 2.0 * math.degrees(math.atan((diagonal_mm / 2.0) / focal_length_mm)),
    }


def compute_dof(f_mm: float, N: float, coc_mm: float, focus_distance_m: float) -> Dict[str, float]:
    # Convert to mm
    s_mm = focus_distance_m * 1000.0
    f = f_mm
    H = (f * f) / (N * coc_mm) + f  # hyperfocal distance (mm)
    near = (H * s_mm) / (H + (s_mm - f))
    far = float("inf") if H <= s_mm else (H * s_mm) / (H - (s_mm - f))
    total = float("inf") if math.isinf(far) else (far - near)
    return {
        "hyperfocal_m": H / 1000.0,
        "near_m": near / 1000.0,
        "far_m": (far / 1000.0) if not math.isinf(far) else float("inf"),
        "total_m": (total / 1000.0) if not math.isinf(total) else float("inf"),
    }


# ---- Image IO ----

def load_image_to_rgb(image_bytes: bytes, filename: str) -> np.ndarray:
    name_lower = (filename or "").lower()
    if any(name_lower.endswith(ext) for ext in [".cr2", ".nef", ".arw", ".dng", ".raf", ".rw2", ".orf"]) and rawpy is not None:
        try:
            with rawpy.imread(io.BytesIO(image_bytes)) as raw:
                rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8)
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)  # we return BGR for OpenCV ops
        except Exception as ex:
            logger.warning(f"RAW decode failed, falling back to PIL: {ex}")
    # Fallback to PIL for standard formats
    pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    arr = np.array(pil)[:, :, ::-1]  # RGB -> BGR
    return arr


def save_bgr_image(image_bgr: np.ndarray, key_prefix: str = "simulations") -> Tuple[str, bytes]:
    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(image_rgb)
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=92)
    data = buf.getvalue()
    key = f"{key_prefix}/{uuid.uuid4().hex}.jpg"
    url = upload_bytes(key, data, content_type="image/jpeg")
    return url, data


# ---- Effect kernels ----

def apply_vignetting(img: np.ndarray, strength: float = 0.2) -> np.ndarray:
    h, w = img.shape[:2]
    y, x = np.ogrid[:h, :w]
    cy, cx = h / 2.0, w / 2.0
    ry = (y - cy) / cy
    rx = (x - cx) / cx
    r2 = rx**2 + ry**2
    mask = 1.0 - np.clip(strength, 0.0, 1.0) * r2
    mask = np.clip(mask, 0.0, 1.0)
    out = img.astype(np.float32)
    out[:, :, 0] *= mask
    out[:, :, 1] *= mask
    out[:, :, 2] *= mask
    return np.clip(out, 0, 255).astype(np.uint8)


def apply_chromatic_aberration(img: np.ndarray, strength: float = 0.1) -> np.ndarray:
    if strength <= 0.0:
        return img
    h, w = img.shape[:2]
    cy, cx = h / 2.0, w / 2.0
    yy, xx = np.indices((h, w), dtype=np.float32)
    x_norm = (xx - cx) / cx
    y_norm = (yy - cy) / cy
    r = np.sqrt(x_norm**2 + y_norm**2)
    # Shift per channel: red outward, blue inward
    shift = strength * 2.0  # scale factor
    dx = x_norm * shift
    dy = y_norm * shift

    map_x = (xx + dx).astype(np.float32)
    map_y = (yy + dy).astype(np.float32)
    map_x_inv = (xx - dx).astype(np.float32)
    map_y_inv = (yy - dy).astype(np.float32)

    b, g, rch = cv2.split(img)
    r_warp = cv2.remap(rch, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    b_warp = cv2.remap(b, map_x_inv, map_y_inv, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    return cv2.merge((b_warp, g, r_warp))


def apply_radial_distortion(img: np.ndarray, k1: float = -0.05) -> np.ndarray:
    if abs(k1) < 1e-6:
        return img
    h, w = img.shape[:2]
    yy, xx = np.indices((h, w), dtype=np.float32)
    cx, cy = w / 2.0, h / 2.0
    x = (xx - cx) / cx
    y = (yy - cy) / cy
    r2 = x * x + y * y
    factor = 1 + k1 * r2
    x_dist = x * factor
    y_dist = y * factor
    map_x = (x_dist * cx + cx).astype(np.float32)
    map_y = (y_dist * cy + cy).astype(np.float32)
    return cv2.remap(img, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def disk_kernel(radius: int) -> np.ndarray:
    r = max(1, int(radius))
    k = np.zeros((2 * r + 1, 2 * r + 1), dtype=np.float32)
    cy = cx = r
    for y in range(2 * r + 1):
        for x in range(2 * r + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                k[y, x] = 1.0
    s = k.sum()
    if s > 0:
        k /= s
    return k


def apply_bokeh_blur(img: np.ndarray, params: SimulationParams) -> np.ndarray:
    # Approximate blur radius in pixels. Not physically exact; tuned for visual plausibility.
    # radius_px ~ scale * (f/N) * (px_per_mm)
    h, w = img.shape[:2]
    sensor_diag_mm = math.hypot(params.sensor_width_mm, params.sensor_height_mm)
    image_diag_px = math.hypot(w, h)
    px_per_mm = image_diag_px / sensor_diag_mm if sensor_diag_mm > 0 else 5.0

    base = (params.focal_length_mm / max(params.aperture, 0.1))
    scale = 0.03  # tuning constant
    radius = (params.bokeh_strength if params.bokeh_strength is not None else 1.0) * base * px_per_mm * scale
    radius = max(1, int(min(radius, min(h, w) * 0.02)))  # cap radius

    k = disk_kernel(radius)
    blurred = cv2.filter2D(img, -1, k, borderType=cv2.BORDER_REFLECT)
    return blurred


def simulate_lens_render(img_bgr: np.ndarray, params: SimulationParams) -> np.ndarray:
    out = img_bgr.copy()
    out = apply_radial_distortion(out, params.distortion_k1)
    out = apply_bokeh_blur(out, params)
    out = apply_vignetting(out, params.vignetting)
    out = apply_chromatic_aberration(out, params.chromatic_aberration)
    return out


def build_params(
    camera_model: Optional[str],
    lens_model: Optional[str],
    focal_length_mm: Optional[float],
    aperture: Optional[float],
    sensor_width_mm: Optional[float],
    sensor_height_mm: Optional[float],
    focus_distance_m: float,
    vignetting: Optional[float],
    chromatic_aberration: Optional[float],
    distortion_k1: Optional[float],
    bokeh_strength: Optional[float],
) -> Tuple[SimulationParams, Dict[str, float]]:
    # Camera
    cam = CAMERA_PRESETS.get(camera_model or "", {})
    sw = sensor_width_mm or cam.get("sensor_width_mm", 36.0)
    sh = sensor_height_mm or cam.get("sensor_height_mm", 24.0)
    coc = cam.get("coc_mm", 0.03)

    # Lens
    lens = LENS_PRESETS.get(lens_model or "", {})
    fmm = focal_length_mm or lens.get("focal_length_mm", 35.0)
    N = aperture or lens.get("max_aperture", 1.8)

    params = SimulationParams(
        sensor_width_mm=float(sw),
        sensor_height_mm=float(sh),
        coc_mm=float(coc),
        focal_length_mm=float(fmm),
        aperture=float(N),
        focus_distance_m=float(max(0.1, focus_distance_m)),
        vignetting=float(vignetting if vignetting is not None else lens.get("vignetting", 0.15)),
        chromatic_aberration=float(chromatic_aberration if chromatic_aberration is not None else lens.get("chromatic_aberration", 0.1)),
        distortion_k1=float(distortion_k1 if distortion_k1 is not None else lens.get("distortion_k1", 0.0)),
        bokeh_strength=bokeh_strength,
    )

    fov = compute_fov(params.sensor_width_mm, params.sensor_height_mm, params.focal_length_mm)
    dof = compute_dof(params.focal_length_mm, params.aperture, params.coc_mm, params.focus_distance_m)

    metrics = {**fov, **dof}
    return params, metrics


def process_simulation(
    image_bytes: bytes,
    filename: str,
    camera_model: Optional[str] = None,
    lens_model: Optional[str] = None,
    focal_length_mm: Optional[float] = None,
    aperture: Optional[float] = None,
    sensor_width_mm: Optional[float] = None,
    sensor_height_mm: Optional[float] = None,
    focus_distance_m: float = 2.0,
    vignetting: Optional[float] = None,
    chromatic_aberration: Optional[float] = None,
    distortion_k1: Optional[float] = None,
    bokeh_strength: Optional[float] = None,
    compose_side_by_side: bool = False,
) -> Dict[str, object]:
    # Build parameters and metrics
    params, metrics = build_params(
        camera_model,
        lens_model,
        focal_length_mm,
        aperture,
        sensor_width_mm,
        sensor_height_mm,
        focus_distance_m,
        vignetting,
        chromatic_aberration,
        distortion_k1,
        bokeh_strength,
    )

    # Load image
    img_bgr = load_image_to_rgb(image_bytes, filename)

    # Simulate
    sim_bgr = simulate_lens_render(img_bgr, params)

    # Save outputs
    original_url, _ = save_bgr_image(img_bgr, key_prefix="simulations/original")
    simulated_url, _ = save_bgr_image(sim_bgr, key_prefix="simulations/simulated")

    side_by_side_url = None
    if compose_side_by_side:
        h1, w1 = img_bgr.shape[:2]
        h2, w2 = sim_bgr.shape[:2]
        h = min(h1, h2)
        # Resize maintaining aspect ratio to same height
        def resize_to_h(im, target_h):
            ih, iw = im.shape[:2]
            new_w = int(iw * (target_h / ih))
            return cv2.resize(im, (new_w, target_h), interpolation=cv2.INTER_AREA)

        left = resize_to_h(img_bgr, h)
        right = resize_to_h(sim_bgr, h)
        composite = np.hstack([left, right])
        side_by_side_url, _ = save_bgr_image(composite, key_prefix="simulations/compare")

    return {
        "metrics": metrics,
        "camera_model": camera_model,
        "lens_model": lens_model,
        "params": {
            "sensor_width_mm": params.sensor_width_mm,
            "sensor_height_mm": params.sensor_height_mm,
            "coc_mm": params.coc_mm,
            "focal_length_mm": params.focal_length_mm,
            "aperture": params.aperture,
            "focus_distance_m": params.focus_distance_m,
            "vignetting": params.vignetting,
            "chromatic_aberration": params.chromatic_aberration,
            "distortion_k1": params.distortion_k1,
        },
        "original_url": original_url,
        "simulated_url": simulated_url,
        "compare_url": side_by_side_url,
        "notes": (
            "RAW decoding requires rawpy; if unavailable, image is processed via standard loader. "
            "DOF/bokeh are approximations without a depth map."
        ),
    }


def list_presets() -> Dict[str, object]:
    return {
        "cameras": CAMERA_PRESETS,
        "lenses": LENS_PRESETS,
        "notes": "Presets are approximate; feel free to extend on the server.",
    }