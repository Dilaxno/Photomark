from fastapi import APIRouter, Query, HTTPException
import httpx
from typing import Optional

from app.core.config import (
    RAPIDAPI_CAMERA_DB_KEY,
    logger,
)

# Correct RapidAPI base and host values
RAPIDAPI_CAMERA_DB_BASE = "https://camera-database.p.rapidapi.com"
RAPIDAPI_CAMERA_DB_HOST = "camera-database.p.rapidapi.com"

router = APIRouter(prefix="/api/camera-db", tags=["camera-db"])


def _headers():
    return {
        "x-rapidapi-host": RAPIDAPI_CAMERA_DB_HOST,
        "x-rapidapi-key": RAPIDAPI_CAMERA_DB_KEY,
    }


@router.get("/lenses")
async def list_lenses(
    brand: str = Query(..., description="Camera brand (required)"),
    autofocus: Optional[bool] = Query(None),
    aperture_ring: Optional[bool] = Query(None),
    mount: Optional[str] = Query(None),
    page: Optional[int] = Query(1),
):
    if not RAPIDAPI_CAMERA_DB_KEY:
        return {"error": "Camera DB key missing"}

    params = {"brand": brand, "page": page}

    if autofocus is not None:
        params["autofocus"] = str(bool(autofocus)).lower()
    if aperture_ring is not None:
        params["aperture_ring"] = str(bool(aperture_ring)).lower()
    if mount:
        params["mount"] = mount

    url = f"{RAPIDAPI_CAMERA_DB_BASE}/lenses"
    logger.info("Upstream request -> %s params=%s headers=%s", url, params, _headers())

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            r = await client.get(url, headers=_headers(), params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as ex:
            status = ex.response.status_code
            try:
                detail = ex.response.json()
            except Exception:
                detail = ex.response.text
            logger.error("Camera DB upstream error: %s %s", status, detail)
            raise HTTPException(status_code=502, detail={
                "upstream_status": status,
                "message": "Camera DB upstream error",
                "detail": detail
            })
        except Exception as ex:
            logger.error("Camera DB request failed: %s", ex)
            raise HTTPException(status_code=502, detail={"message": "Camera DB request failed", "error": str(ex)})


@router.get("/cameras")
async def list_cameras(
    brand: str = Query(..., description="Camera brand (required)"),
    mount: Optional[str] = Query(None),
    page: Optional[int] = Query(1),
):
    if not RAPIDAPI_CAMERA_DB_KEY:
        return {"error": "Camera DB key missing"}

    params = {"brand": brand, "page": page}
    if mount:
        params["mount"] = mount

    url = f"{RAPIDAPI_CAMERA_DB_BASE}/cameras"
    logger.info("Upstream request -> %s params=%s headers=%s", url, params, _headers())

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            r = await client.get(url, headers=_headers(), params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as ex:
            status = ex.response.status_code
            try:
                detail = ex.response.json()
            except Exception:
                detail = ex.response.text
            logger.error("Camera DB upstream error: %s %s", status, detail)
            raise HTTPException(status_code=502, detail={
                "upstream_status": status,
                "message": "Camera DB upstream error",
                "detail": detail
            })
        except Exception as ex:
            logger.error("Camera DB request failed: %s", ex)
            raise HTTPException(status_code=502, detail={"message": "Camera DB request failed", "error": str(ex)})


