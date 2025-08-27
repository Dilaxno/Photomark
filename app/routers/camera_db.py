from fastapi import APIRouter, Query
import httpx
from typing import Optional

from app.core.config import (
    RAPIDAPI_CAMERA_DB_BASE,
    RAPIDAPI_CAMERA_DB_HOST,
    RAPIDAPI_CAMERA_DB_KEY,
    logger,
)

router = APIRouter(prefix="/api/camera-db", tags=["camera-db"])


def _headers():
    return {
        "x-rapidapi-host": RAPIDAPI_CAMERA_DB_HOST,
        "x-rapidapi-key": RAPIDAPI_CAMERA_DB_KEY,
    }


@router.get("/lenses")
async def list_lenses(
    brand: Optional[str] = Query(None),
    autofocus: Optional[bool] = Query(None),
    aperture_ring: Optional[bool] = Query(None),
    mount: Optional[str] = Query(None),
    page: Optional[int] = Query(None),
):
    if not RAPIDAPI_CAMERA_DB_KEY:
        return {"error": "Camera DB key missing"}
    params = {}
    if brand is not None:
        params["brand"] = brand
    if autofocus is not None:
        params["autofocus"] = str(bool(autofocus)).lower()
    if aperture_ring is not None:
        params["aperture_ring"] = str(bool(aperture_ring)).lower()
    if mount:
        params["mount"] = mount
    if page is not None:
        params["page"] = page

    url = f"{RAPIDAPI_CAMERA_DB_BASE}/lenses"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=_headers(), params=params)
        r.raise_for_status()
        return r.json()


@router.get("/cameras")
async def list_cameras(
    brand: Optional[str] = Query(None),
    mount: Optional[str] = Query(None),
    page: Optional[int] = Query(None),
):
    if not RAPIDAPI_CAMERA_DB_KEY:
        return {"error": "Camera DB key missing"}
    params = {}
    if brand:
        params["brand"] = brand
    if mount:
        params["mount"] = mount
    if page is not None:
        params["page"] = page

    url = f"{RAPIDAPI_CAMERA_DB_BASE}/cameras"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=_headers(), params=params)
        r.raise_for_status()
        return r.json()