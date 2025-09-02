import io
import math
import json
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

import numpy as np
from PIL import Image
try:
    import cv2  # opencv-python-headless
except Exception as _:
    cv2 = None

try:
    import lensfunpy as lf
except Exception:
    lf = None


# -------------------------------
# Data structures & helpers
# -------------------------------

@dataclass
class GearGuess:
    camera_maker: Optional[str] = None
    camera_model: Optional[str] = None
    lens_maker: Optional[str] = None
    lens_model: Optional[str] = None

@dataclass
class SensorSpec:
    width_mm: Optional[float] = None
    height_mm: Optional[float] = None
    crop: Optional[float] = None  # if known

@dataclass
class ShotSpec:
    focal_length_mm: Optional[float] = None
    aperture: Optional[float] = None  # f-number N
    focus_distance_m: float = 2.0  # subject distance S (m)

@dataclass
class ArtifactsSpec:
    vignetting: Optional[float] = None              # 0..1 strength if no LF data
    chromatic_aberration: Optional[float] = None    # px shift factor if no LF data
    distortion_k1: Optional[float] = None           # override radial distortion if no LF data
    bokeh_strength: Optional[float] = None          # multiplies CoC blur

LF_DB = None

def _lazy_lensfun_db():
    global LF_DB
    if LF_DB is None and lf is not None:
        LF_DB = lf.Database()
    return LF_DB

def _to_cv(img: Image.Image) -> np.ndarray:
    """PIL -> OpenCV BGR float32 [0..1]"""
    arr = np.array(img.convert("RGB"), dtype=np.uint8)
    return arr[:, :, ::-1].astype(np.float32) / 255.0

def _to_pil(bgr_float01: np.ndarray) -> Image.Image:
    arr = np.clip(bgr_float01 * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(arr[:, :, ::-1], mode="RGB")

def _meshgrid(w, h):
    ys, xs = np.indices((h, w), dtype=np.float32)
    return xs, ys

def _safe_cv():
    if cv2 is None:
        raise RuntimeError("OpenCV not available. Install opencv-python-headless.")

# -------------------------------
# Lensfun: resolve camera / lens
# -------------------------------

def _resolve_camera_lens(
    camera_model_str: Optional[str],
    lens_model_str: Optional[str],
    sensor: SensorSpec
) -> Tuple[Optional[Any], Optional[Any], SensorSpec]:
    """
    Try to find camera & lens in Lensfun DB.
    If camera not found but sensor size provided, compute crop factor from 36x24 reference.
    """
    db = _lazy_lensfun_db()
    cam = ln = None
    updated_sensor = SensorSpec(sensor.width_mm, sensor.height_mm, sensor.crop)

    if db is None:
        return cam, ln, updated_sensor

    # Try camera lookup (free-form, so we fuzzy-match by contains)
    if camera_model_str:
        cams = db.cameras
        cand = [c for c in cams if camera_model_str.lower() in f"{c.make} {c.model}".lower()]
        if cand:
            cam = cand[0]
            # Lensfun camera has crop factor (relative to 35mm)
            if not updated_sensor.crop:
                updated_sensor.crop = getattr(cam, "crop_factor", None)
            # Sensor size might exist in DB; not always exposed, keep user-provided if present.

    # Try lens lookup (bound to camera if present to respect mounts)
    if lens_model_str:
        if cam is not None:
            lenses = db.find_lenses(cam, None, None)
        else:
            lenses = db.lenses
        cand_l = [l for l in lenses if lens_model_str.lower() in f"{l.make} {l.model}".lower()]
        if cand_l:
            ln = cand_l[0]

    # If no crop factor yet and sensor dims exist, compute crop from 36x24
    if (not updated_sensor.crop) and updated_sensor.width_mm and updated_sensor.height_mm:
        diag = math.hypot(updated_sensor.width_mm, updated_sensor.height_mm)
        diag_ff = math.hypot(36.0, 24.0)
        updated_sensor.crop = diag_ff / diag if diag > 0 else None

    return cam, ln, updated_sensor

# -------------------------------
# Lensfun: geometry distortion map
# -------------------------------
def _apply_lensfun_distortion(bgr: np.ndarray, cam, ln, shot: ShotSpec) -> np.ndarray:
    """
    Use Lensfun's geometry model to produce *distorted* image (simulation).
    Lensfun's Modifier typically provides correction mappings. To simulate,
    we apply the inverse mapping (i.e., we remap as if we are undoing the correction).
    """
    _safe_cv()
    h, w = bgr.shape[:2]
    if lf is None or (cam is None and ln is None):
        return bgr

    # Determine crop: if camera present, prefer its crop
    crop = getattr(cam, "crop_factor", None) if cam else None
    if crop is None:
        # Fallback heuristic: typical full-frame crop = 1.0
        crop = 1.0

    # Focal/aperture defaults
    f_mm = float(shot.focal_length_mm or 50.0)
    N = float(shot.aperture or 2.8)
    distance_m = float(shot.focus_distance_m or 2.0)

    try:
        mod = lf.Modifier(ln, crop, w, h) if ln is not None else lf.Modifier(None, crop, w, h)
        # Lensfun expects distance in meters; focal/aperture in the obvious units
        mod.initialize(f_mm, N, distance_m)
    except Exception:
        # If anything fails, return as-is
        return bgr

    # Build coordinate map (pixel centers)
    xs, ys = _meshgrid(w, h)
    coords = np.dstack([xs, ys]).reshape(-1, 2).astype(np.float32)

    # Lensfun APIs differ across builds; try a few known names.
    distorted = None
    for method_name, inverse in [
        ("apply_subpixel_distortion", True),   # ask for the inverse of correction (simulate)
        ("apply_geometry_distortion", True),   # alt name in some builds
        ("apply_subpixel_distortion", False),  # fallback if API semantics inverted on build
    ]:
        try:
            fn = getattr(mod, method_name)
            # many builds use a kwarg 'inverse', some use positional; try both
            try:
                mapped = fn(coords, inverse=inverse)
            except TypeError:
                mapped = fn(coords, inverse)
            if mapped is not None and mapped.shape == coords.shape:
                distorted = mapped
                break
        except Exception:
            continue

    if distorted is None:
        # Could not obtain a LF mapping
        return bgr

    map_x = distorted[:, 0].reshape(h, w).astype(np.float32)
    map_y = distorted[:, 1].reshape(h, w).astype(np.float32)
    # Remap source pixels into distorted coordinates
    out = cv2.remap(bgr, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    return out

# -------------------------------
# Chromatic Aberration simulation
# -------------------------------

def _simulate_tca(bgr: np.ndarray, strength_px: float = 0.5) -> np.ndarray:
    """
    Simple radial transverse chromatic aberration:
    shift R outward and B inward by a small, radius-proportional amount.
    """
    _safe_cv()
    h, w = bgr.shape[:2]
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0

    ys, xs = np.indices((h, w), dtype=np.float32)
    dx = xs - cx
    dy = ys - cy
    r = np.sqrt(dx * dx + dy * dy)
    r_norm = r / r.max() if r.max() > 0 else r

    # Shift amount grows towards edges
    shift = strength_px * r_norm

    # Compute unit direction vectors
    ux = np.divide(dx, r, out=np.zeros_like(dx), where=r > 1e-6)
    uy = np.divide(dy, r, out=np.zeros_like(dy), where=r > 1e-6)

    map_x_base = xs.astype(np.float32)
    map_y_base = ys.astype(np.float32)

    # Red channel shifted outward
    map_x_r = (map_x_base + ux * shift).astype(np.float32)
    map_y_r = (map_y_base + uy * shift).astype(np.float32)

    # Blue channel shifted inward
    map_x_b = (map_x_base - ux * shift).astype(np.float32)
    map_y_b = (map_y_base - uy * shift).astype(np.float32)

    # Extract channels
    B, G, R = cv2.split(bgr)

    Rd = cv2.remap(R, map_x_r, map_y_r, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    Bd = cv2.remap(B, map_x_b, map_y_b, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

    out = cv2.merge([Bd, G, Rd])
    return out
# -------------------------------
# Vignetting simulation
# -------------------------------

def _simulate_vignetting(bgr: np.ndarray, strength: float = 0.5, softness: float = 2.5) -> np.ndarray:
    """
    Radial falloff. strength: 0..1, softness controls curve (larger = gentler).
    Uses cos^4-ish curve approximation.
    """
    h, w = bgr.shape[:2]
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    ys, xs = np.indices((h, w), dtype=np.float32)
    # radial distance from center
    r = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    r_norm = np.clip(r / r.max(), 0, 1)

    # Smooth falloff curve (cosine-like)
    falloff = (1.0 - strength) + strength * (1.0 - r_norm ** softness)
    falloff = np.clip(falloff, 0.0, 1.0).astype(np.float32)

    if bgr.ndim == 3:
        falloff = falloff[:, :, None]

    return bgr * falloff

# -------------------------------
# Depth of Field / Bokeh (CoC)
# -------------------------------

def _coc_infinity_mm(f_mm: float, N: float, S_m: float) -> float:
    """
    Circle of confusion diameter on sensor (mm) for a background point at infinity,
    focused at S (m). Thin lens approximation.
    c_inf = f^2 / (N * (S - f))   (with S in mm)
    """
    S_mm = S_m * 1000.0
    if S_mm <= f_mm + 1e-6:
        return 0.0
    return (f_mm * f_mm) / (N * (S_mm - f_mm))

def _apply_bokeh_disc(bgr: np.ndarray, radius_px: float) -> np.ndarray:
    """
    Simple circular (pillbox) PSF blur to mimic bokeh.
    """
    _safe_cv()
    r = max(0, int(round(radius_px)))
    if r < 1:
        return bgr

    # Build normalized circular kernel
    k = 2 * r + 1
    y, x = np.ogrid[-r:r+1, -r:r+1]
    mask = (x*x + y*y) <= r*r
    kernel = np.zeros((k, k), dtype=np.float32)
    kernel[mask] = 1.0
    s = kernel.sum()
    if s > 0:
        kernel /= s

    # Filter each channel
    out = np.empty_like(bgr)
    for c in range(bgr.shape[2]):
        out[:, :, c] = cv2.filter2D(bgr[:, :, c], -1, kernel, borderType=cv2.BORDER_REFLECT)
    return out

def _simulate_dof_bokeh(
    bgr: np.ndarray,
    sensor: SensorSpec,
    shot: ShotSpec,
    bokeh_strength: Optional[float] = None
) -> np.ndarray:
    """
    Without a depth map, we approximate background at infinity bokeh only.
    That gives a conservative "shallow DoF look" akin to how far backgrounds blur.
    """
    if sensor.width_mm is None:
        # Assume 36mm width if unknown (full frame) for px/mm
        sensor_width_mm = 36.0
    else:
        sensor_width_mm = sensor.width_mm

    f_mm = float(shot.focal_length_mm or 50.0)
    N = float(shot.aperture or 2.8)
    S_m = float(shot.focus_distance_m or 2.0)

    h, w = bgr.shape[:2]
    px_per_mm = w / sensor_width_mm if sensor_width_mm > 0 else (w / 36.0)

    c_inf_mm = _coc_infinity_mm(f_mm, N, S_m)  # CoC on sensor in mm
    base_radius_px = c_inf_mm * px_per_mm * 0.5  # radius ~ half of diameter

    # Artistic control multiplier (default 1.0)
    mult = float(bokeh_strength) if bokeh_strength is not None else 1.0
    radius_px = base_radius_px * max(0.0, mult)

    # Apply single-radius disc blur as a reasonable background-blur proxy
    return _apply_bokeh_disc(bgr, radius_px)

# -------------------------------
# Public API
# -------------------------------

def list_presets() -> Dict[str, Any]:
    """
    Minimal stub: return whatever lenses/cameras we can list (names only) to help clients.
    You can expand with pagination or caching as needed.
    """
    db = _lazy_lensfun_db()
    cams = []
    lenses = []
    if db is not None:
        for c in db.cameras[:200]:
            cams.append(f"{c.make} {c.model}")
        for l in db.lenses[:400]:
            lenses.append(f"{l.make} {l.model}")
    return {"cameras": cams, "lenses": lenses}

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
    compose_side_by_side: bool = True,
) -> Dict[str, Any]:
    """
    Returns dict: {
        "width": int, "height": int,
        "info": { ... parameters ... },
        "images": {
            "simulated_jpeg": <bytes>,
            "side_by_side_jpeg": <bytes or None>,
        }
    }
    """
    _safe_cv()

    # Load image
    src_pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    src = _to_cv(src_pil)  # BGR float [0..1]
    h, w = src.shape[:2]

    # Resolve Lensfun camera/lens and sensor/crop
    sensor = SensorSpec(sensor_width_mm, sensor_height_mm, None)
    cam, ln, sensor = _resolve_camera_lens(camera_model, lens_model, sensor)

    # Build shot spec
    shot = ShotSpec(
        focal_length_mm=focal_length_mm,
        aperture=aperture,
        focus_distance_m=focus_distance_m
    )

    # --- 1) Geometry: distortion ---
    work = src.copy()
    if lf is not None and (cam is not None or ln is not None):
        try:
            work = _apply_lensfun_distortion(work, cam, ln, shot)
        except Exception:
            # If LF mapping fails, we fall back to k1 radial
            pass

    # Optional fallback: user-provided k1 radial distortion (barrel/pincushion)
    if distortion_k1 is not None:
        # Positive k1 -> barrel; negative -> pincushion
        xs, ys = _meshgrid(w, h)
        cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
        x = (xs - cx) / cx
        y = (ys - cy) / cy
        r2 = x * x + y * y
        k1 = float(distortion_k1)
        u = x * (1 + k1 * r2)
        v = y * (1 + k1 * r2)
        map_x = (u * cx + cx).astype(np.float32)
        map_y = (v * cy + cy).astype(np.float32)
        work = cv2.remap(work, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

    # --- 2) Chromatic aberration (TCA) ---
    if chromatic_aberration is not None:
        try:
            work = _simulate_tca(work, strength_px=float(chromatic_aberration))
        except Exception:
            pass
    else:
        # Small default if no LF CA data (subtle realism)
        work = _simulate_tca(work, strength_px=0.3)

    # --- 3) Vignetting ---
    vig_strength = float(vignetting) if vignetting is not None else 0.35
    try:
        work = _simulate_vignetting(work, strength=vig_strength, softness=2.8)
    except Exception:
        pass

    # --- 4) DoF / Bokeh using CoC (no depth map; background proxy) ---
    try:
        work = _simulate_dof_bokeh(work, sensor, shot, bokeh_strength=bokeh_strength)
    except Exception:
        pass

    # Compose side-by-side preview
    sbs_bytes = None
    if compose_side_by_side:
        try:
            sim_pil = _to_pil(work)
            sbs = Image.new("RGB", (w * 2, h))
            sbs.paste(src_pil, (0, 0))
            sbs.paste(sim_pil, (w, 0))
            buf_sbs = io.BytesIO()
            sbs.save(buf_sbs, format="JPEG", quality=90, subsampling=1)
            sbs_bytes = buf_sbs.getvalue()
        except Exception:
            sbs_bytes = None

    # Encode final simulated image
    sim_pil = _to_pil(work)
    buf = io.BytesIO()
    sim_pil.save(buf, format="JPEG", quality=92, subsampling=1)
    out_bytes = buf.getvalue()

    info = {
        "used_lensfun": bool(lf is not None and (cam is not None or ln is not None)),
        "camera_resolved": f"{getattr(cam, 'make', '')} {getattr(cam, 'model', '')}".strip() if cam else None,
        "lens_resolved": f"{getattr(ln, 'make', '')} {getattr(ln, 'model', '')}".strip() if ln else None,
        "sensor": {
            "width_mm": sensor.width_mm,
            "height_mm": sensor.height_mm,
            "crop": sensor.crop,
        },
        "shot": {
            "focal_length_mm": shot.focal_length_mm,
            "aperture": shot.aperture,
            "focus_distance_m": shot.focus_distance_m,
        },
        "params": {
            "vignetting": vignetting,
            "chromatic_aberration": chromatic_aberration,
            "distortion_k1": distortion_k1,
            "bokeh_strength": bokeh_strength,
        }
    }

    return {
        "width": w,
        "height": h,
        "info": info,
        "images": {
            "simulated_jpeg": out_bytes,
            "side_by_side_jpeg": sbs_bytes
        }
    }