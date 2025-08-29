from typing import Dict, List, Optional
import os
import base64
import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from app.core.config import s3, R2_LUTS_BUCKET, R2_LUTS_PREFIX

router = APIRouter(prefix="/api/lut-previews", tags=["lut-previews"]) 

# In-memory cache of last listing (optional convenience for quick lookup)
_cache: Dict[str, List[Dict]] = {"items": []}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _key_to_id(key: str) -> str:
    """Encode an R2 object key into a URL-safe id (no slashes)."""
    return base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii").rstrip("=")


def _id_to_key(file_id: str) -> str:
    pad = "=" * (-len(file_id) % 4)
    return base64.urlsafe_b64decode((file_id + pad).encode("ascii")).decode("utf-8")


def _is_image_key(key: str) -> bool:
    lower = key.lower()
    return any(lower.endswith(ext) for ext in IMAGE_EXTS)


def _basename_no_ext(key: str) -> str:
    name = key.split("/")[-1]
    return name.rsplit(".", 1)[0].strip().lower()


@router.get("/list")
async def list_previews(prefix: Optional[str] = None):
    if not s3 or not R2_LUTS_BUCKET:
        return JSONResponse({"error": "R2 not configured"}, status_code=400)

    bucket_name = R2_LUTS_BUCKET
    obj_prefix = (prefix or R2_LUTS_PREFIX or "").lstrip("/")

    try:
        bucket = s3.Bucket(bucket_name)
        # Iterate objects under prefix, filter image files
        items: List[Dict] = []
        for obj in bucket.objects.filter(Prefix=obj_prefix):
            key = obj.key
            if not _is_image_key(key):
                continue
            name = key.split("/")[-1]
            base = _basename_no_ext(key)
            items.append({
                "id": _key_to_id(key),
                "name": name,
                "base": base,
                "mimeType": mimetypes.guess_type(name)[0] or "image/jpeg",
            })
            if len(items) >= 5000:
                break
        _cache["items"] = items
        return {"files": items}
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"R2 list error: {ex}")


@router.get("/{file_id}")
async def proxy_preview(file_id: str):
    if not s3 or not R2_LUTS_BUCKET:
        raise HTTPException(status_code=400, detail="R2 not configured")
    try:
        key = _id_to_key(file_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid file id")

    try:
        obj = s3.Object(R2_LUTS_BUCKET, key)
        res = obj.get()
        content = res["Body"].read()
        ctype = res.get("ContentType") or mimetypes.guess_type(key)[0] or "application/octet-stream"
        return Response(content=content, media_type=ctype)
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"R2 fetch error: {ex}")