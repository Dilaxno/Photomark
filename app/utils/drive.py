import os
from typing import Dict, List, Optional, Tuple
import httpx

GOOGLE_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_API_KEY = os.getenv("DRIVE_API_KEY", "").strip()

class DriveError(RuntimeError):
    pass


def _require_key() -> str:
    if not DRIVE_API_KEY:
        raise DriveError("DRIVE_API_KEY not configured in environment")
    return DRIVE_API_KEY


def list_images_in_folder(folder_id: str) -> List[Dict]:
    """
    List public image files in a Google Drive folder by ID using an API key.
    Returns a list of dicts: { id, name, mimeType, size?, modifiedTime }
    """
    key = _require_key()
    url = f"{GOOGLE_DRIVE_API_BASE}/files"
    params = {
        "q": f"'{folder_id}' in parents and trashed = false and mimeType contains 'image/'",
        "fields": "files(id,name,mimeType,modifiedTime),nextPageToken",
        "pageSize": 1000,
        "includeItemsFromAllDrives": "true",
        "supportsAllDrives": "true",
        "key": key,
    }
    items: List[Dict] = []
    with httpx.Client(timeout=30.0) as client:
        while True:
            r = client.get(url, params=params)
            if r.status_code != 200:
                raise DriveError(f"Drive list error: {r.status_code} {r.text}")
            data = r.json()
            items.extend(data.get("files", []))
            token = data.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
    # Normalize names: keep original but also provide 'base' (lower, no ext)
    for it in items:
        name = it.get("name") or ""
        base = name.rsplit(".", 1)[0].strip().lower()
        it["base"] = base
    return items


def fetch_file_content(file_id: str) -> Tuple[bytes, str]:
    """
    Download file bytes via Drive API using API key.
    Returns (content_bytes, content_type)
    """
    key = _require_key()
    url = f"{GOOGLE_DRIVE_API_BASE}/files/{file_id}"
    params = {"alt": "media", "key": key}
    with httpx.Client(timeout=60.0) as client:
        r = client.get(url, params=params)
        if r.status_code != 200:
            raise DriveError(f"Drive fetch error: {r.status_code} {r.text}")
        content_type = r.headers.get("Content-Type", "application/octet-stream")
        return r.content, content_type