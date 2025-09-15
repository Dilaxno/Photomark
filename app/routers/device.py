from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import httpx

from app.core.auth import get_uid_from_request, get_user_email_from_uid
from app.core.config import GEOIP_LOOKUP_URL, logger, NEW_DEVICE_ALERT_COOLDOWN_SEC
from app.utils.storage import read_json_key, write_json_key
from app.utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api", tags=["device"])


def _device_meta_key(uid: str) -> str:
    return f"users/{uid}/devices/meta.json"


def _device_key(uid: str, fp: str) -> str:
    return f"users/{uid}/devices/{fp}.json"


def _device_meta_key_user(uid: str) -> str:
    # Alias for clarity in meta usage
    return _device_meta_key(uid)


def _get_client_ip(request: Request) -> str:
    try:
        headers = request.headers
        # Prefer CDN/proxy forwarded headers
        for h in ("cf-connecting-ip", "x-real-ip", "x-client-ip"):
            v = headers.get(h) or headers.get(h.title())
            if v:
                return v.split(",")[0].strip()
        xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        if request.client:
            return request.client.host or ""
    except Exception:
        pass
    return ""


@router.post("/auth/device/register")
async def device_register(request: Request, payload: Optional[Dict[str, Any]] = Body(None)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Extract headers and client data
    ua = request.headers.get("user-agent") or ""
    ip = _get_client_ip(request)
    now = datetime.now(timezone.utc).isoformat()

    # Prefer FingerprintJS payload from frontend, fallback to hash(ua|ip)
    client_fp = ""
    if payload and isinstance(payload, dict):
        client_fp = str(payload.get("fingerprint") or "").strip()
    if not client_fp:
        # Simple fallback
        import hashlib
        client_fp = hashlib.sha256(f"{ua}|{ip}".encode("utf-8")).hexdigest()[:16]

    info: Dict[str, Any] = {}
    if payload and isinstance(payload, dict):
        raw_info = payload.get("info") or {}
        if isinstance(raw_info, dict):
            info = raw_info

    # Load existing device record to determine if this is a new device
    key = _device_key(uid, client_fp)
    rec = read_json_key(key) or {}
    is_new_device = not bool(rec.get("first_seen"))

    # Update device record
    seen_count = int(rec.get("seen_count") or 0) + 1
    rec.update({
        "uid": uid,
        "fp": client_fp,
        "ip": ip,
        "ua": ua or info.get("ua") or "",
        "info": info,
        "seen_count": seen_count,
        "last_seen": now,
    })
    if is_new_device:
        rec["first_seen"] = now
    write_json_key(key, rec)

    # Update meta
    meta_key = _device_meta_key_user(uid)
    meta = read_json_key(meta_key) or {}
    meta["last_register_at"] = now

    # Optionally geolocate IP
    city = region = country = org = ""
    if GEOIP_LOOKUP_URL and ip:
        try:
            url = GEOIP_LOOKUP_URL
            if "{ip}" in url:
                url = url.replace("{ip}", ip)
            elif "?" in url:
                url = f"{url}&ip={ip}"
            else:
                url = f"{url}?ip={ip}"
            async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    city = str(data.get("city") or data.get("City") or "")
                    region = str(data.get("region") or data.get("region_name") or data.get("RegionName") or "")
                    country = str(data.get("country") or data.get("country_name") or data.get("Country") or "")
                    org = str(data.get("org") or data.get("isp") or "")
        except Exception as ex:
            logger.debug(f"GeoIP lookup failed for {ip}: {ex}")

    # Send notification email if a new device was detected (with cooldown)
    alerted = False
    if is_new_device:
        try:
            # Cooldown guard
            last_alert_at = meta.get("last_alert_at")
            should_alert = True
            if last_alert_at:
                try:
                    from datetime import datetime as _dt
                    last_dt = _dt.fromisoformat(str(last_alert_at))
                    delta = _dt.now(timezone.utc) - (last_dt.replace(tzinfo=timezone.utc) if last_dt.tzinfo is None else last_dt)
                    if delta.total_seconds() < max(NEW_DEVICE_ALERT_COOLDOWN_SEC, 0):
                        should_alert = False
                except Exception:
                    pass

            if should_alert:
                user_email = get_user_email_from_uid(uid) or ""
                if user_email:
                    # Build message
                    loc_bits = ", ".join([b for b in [city, region, country] if b])
                    tz = str((info.get("tz") or "")).strip()
                    details = [
                        f"Time: {now}",
                        f"IP: {ip}",
                        f"Location: {loc_bits}" if loc_bits else None,
                        f"Network: {org}" if org else None,
                        f"Device: {ua or (info.get('ua') or '')}",
                        f"Timezone: {tz}" if tz else None,
                        f"Fingerprint: {client_fp}",
                    ]
                    details = [d for d in details if d]
                    intro = "A new device just signed in to your account.<br><br>" + "<br>".join(details) + "<br><br>If this wasn’t you, we recommend changing your password and reviewing your security settings."
                    subject = "New device login to your account"
                    html = render_email(
                        "email_basic.html",
                        title="New device detected",
                        intro=intro,
                    )
                    try:
                        send_email_smtp(user_email, subject, html)
                        alerted = True
                        meta["last_alert_at"] = now
                    except Exception as ex:
                        logger.warning(f"Failed to send new-device email to {user_email}: {ex}")
        except Exception as ex:
            logger.warning(f"new-device alert flow failed for uid={uid}: {ex}")

    write_json_key(meta_key, meta)

    return {"ok": True, "fp": client_fp, "new_device": is_new_device, "alerted": alerted}