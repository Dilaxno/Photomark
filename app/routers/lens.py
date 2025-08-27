from fastapi import APIRouter, UploadFile, File, Form, Request
from typing import Optional

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.config import logger
from app.utils.lens_simulation import process_simulation, list_presets

router = APIRouter(prefix="/api/lens", tags=["lens-sim"])


@router.get("/presets")
async def get_presets():
    return list_presets()


@router.post("/simulate")
async def simulate(
    request: Request,
    file: UploadFile = File(...),
    camera_model: Optional[str] = Form(None),
    lens_model: Optional[str] = Form(None),
    focal_length_mm: Optional[float] = Form(None),
    aperture: Optional[float] = Form(None),
    sensor_width_mm: Optional[float] = Form(None),
    sensor_height_mm: Optional[float] = Form(None),
    focus_distance_m: float = Form(2.0),
    vignetting: Optional[float] = Form(None),
    chromatic_aberration: Optional[float] = Form(None),
    distortion_k1: Optional[float] = Form(None),
    bokeh_strength: Optional[float] = Form(None),
    compose_side_by_side: bool = Form(True),
):
    # Authorization same as images endpoints: require gallery role
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return {"error": "Unauthorized"}
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return {"error": "Forbidden"}

    try:
        raw = await file.read()
        result = process_simulation(
            image_bytes=raw,
            filename=file.filename or "upload",
            camera_model=camera_model,
            lens_model=lens_model,
            focal_length_mm=focal_length_mm,
            aperture=aperture,
            sensor_width_mm=sensor_width_mm,
            sensor_height_mm=sensor_height_mm,
            focus_distance_m=focus_distance_m,
            vignetting=vignetting,
            chromatic_aberration=chromatic_aberration,
            distortion_k1=distortion_k1,
            bokeh_strength=bokeh_strength,
            compose_side_by_side=compose_side_by_side,
        )
        return {"ok": True, **result}
    except Exception as ex:
        logger.exception(f"Simulation failed: {ex}")
        return {"error": str(ex)}