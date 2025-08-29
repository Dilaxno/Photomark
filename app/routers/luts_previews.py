from typing import Dict, List, Optional
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from app.utils.drive import list_images_in_folder, fetch_file_content, DriveError

router = APIRouter(prefix="/api/lut-previews", tags=["lut-previews"]) 

# Configure via env
DRIVE_FOLDER_ID = os.getenv("DRIVE_LUT_PREVIEWS_FOLDER_ID", "").strip()

# In-memory cache of last listing
_cache: Dict[str, List[Dict]] = {"items": []}


@router.get("/list")
async def list_previews(folder_id: Optional[str] = None):
    fid = (folder_id or DRIVE_FOLDER_ID).strip()
    if not fid:
        return JSONResponse({"error": "folder_id not configured"}, status_code=400)
    try:
        items = list_images_in_folder(fid)
        _cache["items"] = items
        # Expose minimal info for frontend
        return {"files": [
            {"id": it.get("id"), "name": it.get("name"), "base": it.get("base"), "mimeType": it.get("mimeType")}
            for it in items
        ]}
    except DriveError as ex:
        raise HTTPException(status_code=502, detail=str(ex))


@router.get("/{file_id}")
async def proxy_preview(file_id: str):
    # Stream/serve an image by file ID
    try:
        content, ctype = fetch_file_content(file_id)
        return Response(content=content, media_type=ctype)
    except DriveError as ex:
        raise HTTPException(status_code=502, detail=str(ex))