from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from datetime import datetime

from app.core.auth import get_uid_from_request
from app.core.config import GEOIP_LOOKUP_URL, logger, NEW_DEVICE_ALERT_COOLDOWN_SEC
from app.utils.storage import read_json_key, write_json_key

router = APIRouter(prefix="/api", tags=["device"])


def _device_meta_key(uid: str) -> str:
    return f"users/{uid}/devices/meta.json"


def _device_key(uid: str, fp: str) -> str:
    return f"users/{uid}/devices/{fp}.json"


@router.post("/auth/device/register")
async def device_register(request: Request):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Best-effort device fingerprint via headers
    ua = request.headers.get("user-agent") or ""
    ip = request.client.host if request.client else ""
    now = datetime.utcnow().isoformat()

    # Simple fingerprint (ua + ip); frontend can be extended to send a more stable one
    import hashlib
    fp = hashlib.sha256(f"{ua}|{ip}".encode("utf-8")).hexdigest()[:16]

    rec = read_json_key(_device_key(uid, fp)) or {}
    rec.update({
        "uid": uid,
        "fp": fp,
        "ip": ip,
        "ua": ua,
        "last_seen": now,
    })
    write_json_key(_device_key(uid, fp), rec)

    meta = read_json_key(_device_meta_key(uid)) or {}
    meta["last_register_at"] = now
    write_json_key(_device_meta_key(uid), meta)

    return {"ok": True, "fp": fp}