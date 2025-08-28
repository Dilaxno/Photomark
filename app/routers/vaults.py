from typing import List, Optional, Tuple
import os
import json
import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.config import s3, R2_BUCKET, R2_PUBLIC_BASE_URL, logger
from app.core.auth import get_uid_from_request, get_user_email_from_uid
from app.utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api", tags=["vaults"])


class ApprovalPayload(BaseModel):
    token: str
    key: str
    action: str  # 'approve' or 'deny'
    comment: str | None = None

# Local static dir used when s3 is not configured
STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))


def _share_key(token: str) -> str:
    return f"shares/{token}.json"


def _approval_key(uid: str, vault: str) -> str:
    safe = "".join(c for c in vault if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
    return f"users/{uid}/vaults/_approvals/{safe}.json"


def _make_item_from_key(uid: str, key: str) -> dict:
    if not key.startswith(f"users/{uid}/"):
        raise ValueError("forbidden key")
    name = os.path.basename(key)
    if s3 and R2_BUCKET:
        if R2_PUBLIC_BASE_URL:
            url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"
        else:
            url = s3.meta.client.generate_presigned_url(
                "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
            )
    else:
        url = f"/static/{key}"
    return {"key": key, "url": url, "name": name}


def _vault_key(uid: str, vault: str) -> Tuple[str, str]:
    safe = "".join(c for c in vault if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
    if not safe:
        raise ValueError("invalid vault name")
    return f"users/{uid}/vaults/{safe}.json", safe


def _vault_meta_key(uid: str, vault: str) -> str:
    _, safe = _vault_key(uid, vault)
    return f"users/{uid}/vaults/_meta/{safe}.json"


def _write_json_key(key: str, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        bucket.put_object(Key=key, Body=data.encode('utf-8'), ContentType='application/json', ACL='private')
    else:
        path = os.path.join(STATIC_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(data)


from botocore.exceptions import ClientError

def _read_json_key(key: str) -> Optional[dict]:
    try:
        if s3 and R2_BUCKET:
            obj = s3.Object(R2_BUCKET, key)
            try:
                body = obj.get()["Body"].read().decode("utf-8")
            except ClientError as ce:
                if ce.response.get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                    return None
                raise
            return json.loads(body)
        else:
            path = os.path.join(STATIC_DIR, key)
            if not os.path.isfile(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as ex:
        logger.warning(f"_read_json_key failed for {key}: {ex}")
        return None


def _read_vault(uid: str, vault: str) -> list[str]:
    key, _ = _vault_key(uid, vault)
    try:
        if s3 and R2_BUCKET:
            obj = s3.Object(R2_BUCKET, key)
            body = obj.get()["Body"].read().decode("utf-8")
            data = json.loads(body)
        else:
            path = os.path.join(STATIC_DIR, key)
            if not os.path.isfile(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return list(data.get("keys", []))
    except Exception as ex:
        logger.warning(f"_read_vault failed for {key}: {ex}")
        return []


def _write_vault(uid: str, vault: str, keys: list[str]):
    key, _ = _vault_key(uid, vault)
    payload = json.dumps({"keys": sorted(set(keys))})
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        bucket.put_object(Key=key, Body=payload.encode("utf-8"), ContentType="application/json", ACL="private")
    else:
        path = os.path.join(STATIC_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)


def _delete_vault(uid: str, vault: str) -> bool:
    try:
        key, safe = _vault_key(uid, vault)
        meta_key = _vault_meta_key(uid, vault)
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            to_delete = [{"Key": key}, {"Key": meta_key}]
            bucket.delete_objects(Delete={"Objects": to_delete})
        else:
            path = os.path.join(STATIC_DIR, key)
            meta_path = os.path.join(STATIC_DIR, meta_key)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
            try:
                if os.path.isfile(meta_path):
                    os.remove(meta_path)
            except Exception:
                pass
        return True
    except Exception as ex:
        logger.warning(f"_delete_vault failed for {vault}: {ex}")
        return False


_unlocked_vaults: dict[str, set[str]] = {}

def _read_vault_meta(uid: str, vault: str) -> dict:
    key = _vault_meta_key(uid, vault)
    meta = _read_json_key(key)
    return meta or {}


def _write_vault_meta(uid: str, vault: str, meta: dict):
    key = _vault_meta_key(uid, vault)
    _write_json_key(key, meta or {})


def _vault_salt(uid: str, vault: str) -> str:
    return f"{uid}::{vault}::v1"


import hashlib

def _hash_password(pw: str, salt: str) -> str:
    try:
        return hashlib.sha256((pw or '' + salt).encode('utf-8')).hexdigest()
    except Exception:
        return ''


def _is_vault_unlocked(uid: str, vault: str) -> bool:
    meta = _read_vault_meta(uid, vault)
    if not meta.get('protected'):
        return True
    s = _unlocked_vaults.get(uid) or set()
    return (vault in s)


def _unlock_vault(uid: str, vault: str, password: str) -> bool:
    meta = _read_vault_meta(uid, vault)
    if not meta.get('protected'):
        return True
    salt = _vault_salt(uid, vault)
    if meta.get('hash') == _hash_password(password or '', salt):
        s = _unlocked_vaults.get(uid)
        if not s:
            s = set()
            _unlocked_vaults[uid] = s
        s.add(vault)
        return True
    return False


def _lock_vault(uid: str, vault: str):
    s = _unlocked_vaults.get(uid)
    if s and vault in s:
        s.remove(vault)


@router.get("/vaults")
async def vaults_list(request: Request):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # List vaults by scanning directory/objects
    prefix = f"users/{uid}/vaults/"
    results: list[dict] = []
    try:
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            names: list[str] = []
            for obj in bucket.objects.filter(Prefix=prefix):
                key = obj.key
                if not key.endswith(".json"):
                    continue
                # Skip files inside the _meta directory
                if key.startswith(prefix + "_meta/"):
                    continue
                base = os.path.basename(key)[:-5]
                names.append(base)
            for n in sorted(set(names)):
                count = len(_read_vault(uid, n))
                results.append({"name": n, "count": count})
        else:
            dir_path = os.path.join(STATIC_DIR, prefix)
            if os.path.isdir(dir_path):
                for f in os.listdir(dir_path):
                    if f.endswith(".json") and f != "_meta.json":
                        name = f[:-5]
                        count = len(_read_vault(uid, name))
                        results.append({"name": name, "count": count})
    except Exception as ex:
        logger.warning(f"_list_vaults failed: {ex}")
    # Mark protection state
    for v in results:
        name = v.get("name")
        if not isinstance(name, str):
            continue
        meta = _read_vault_meta(uid, name)
        v["protected"] = bool(meta.get("protected"))
        v["unlocked"] = _is_vault_unlocked(uid, name)
    return {"vaults": results}


@router.post("/vaults/delete")
async def vaults_delete(request: Request, vaults: List[str] = Body(..., embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not vaults or not isinstance(vaults, list):
        return JSONResponse({"error": "No vaults provided"}, status_code=400)
    deleted: list[str] = []
    errors: list[str] = []
    for v in vaults:
        name = str(v or '').strip()
        if not name:
            continue
        ok = _delete_vault(uid, name)
        if ok:
            deleted.append(name)
        else:
            errors.append(name)
    return {"deleted": deleted, "errors": errors}


@router.post("/vaults/create")
async def vaults_create(request: Request, name: str = Body(..., embed=True), protect: Optional[bool] = Body(False, embed=True), password: Optional[str] = Body(None, embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        keys = _read_vault(uid, name)
        _write_vault(uid, name, keys)
        if protect and (password or '').strip():
            salt = _vault_salt(uid, name)
            _write_vault_meta(uid, name, {"protected": True, "hash": _hash_password(password or '', salt)})
        return {"name": _vault_key(uid, name)[1], "count": len(keys)}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/add")
async def vaults_add(request: Request, vault: str = Body(..., embed=True), keys: List[str] = Body(..., embed=True), password: Optional[str] = Body(None, embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    meta = _read_vault_meta(uid, vault)
    if meta.get('protected') and not _is_vault_unlocked(uid, vault):
        if not _unlock_vault(uid, vault, password or ''):
            return JSONResponse({"error": "Vault locked"}, status_code=403)
    try:
        exist = _read_vault(uid, vault)
        filtered = [k for k in keys if k.startswith(f"users/{uid}/")]
        merged = sorted(set(exist) | set(filtered))
        _write_vault(uid, vault, merged)
        return {"vault": _vault_key(uid, vault)[1], "count": len(merged)}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/remove")
async def vaults_remove(request: Request, vault: str = Body(..., embed=True), keys: List[str] = Body(..., embed=True), password: Optional[str] = Body(None, embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    meta = _read_vault_meta(uid, vault)
    if meta.get('protected') and not _is_vault_unlocked(uid, vault):
        if not _unlock_vault(uid, vault, password or ''):
            return JSONResponse({"error": "Vault locked"}, status_code=403)
    try:
        exist = _read_vault(uid, vault)
        to_remove = set(keys)
        remain = [k for k in exist if k not in to_remove]
        _write_vault(uid, vault, remain)
        return {"vault": _vault_key(uid, vault)[1], "count": len(remain)}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/unlock")
async def vaults_unlock(request: Request, vault: str = Body(..., embed=True), password: str = Body(..., embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if _unlock_vault(uid, vault, password or ''):
        return {"ok": True}
    return JSONResponse({"error": "Invalid password"}, status_code=403)


@router.post("/vaults/lock")
async def vaults_lock(request: Request, vault: str = Body(..., embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _lock_vault(uid, vault)
    return {"ok": True}


@router.get("/vaults/photos")
async def vaults_photos(request: Request, vault: str, password: Optional[str] = None):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    meta = _read_vault_meta(uid, vault)
    # If protected and not already unlocked, allow one-shot password check
    if meta.get('protected') and not _is_vault_unlocked(uid, vault):
        if not _unlock_vault(uid, vault, password or ''):
            return JSONResponse({"error": "Vault locked"}, status_code=403)
        # Do not persist unlock beyond this request; immediately lock again
        _lock_vault(uid, vault)
    try:
        keys = _read_vault(uid, vault)
        items: list[dict] = []
        if s3 and R2_BUCKET:
            # Build lookup of originals to attach to items
            orig_prefix = f"users/{uid}/originals/"
            original_lookup: dict[str, dict] = {}
            try:
                bucket = s3.Bucket(R2_BUCKET)
                for o in bucket.objects.filter(Prefix=orig_prefix):
                    ok = o.key
                    if ok.endswith("/"):
                        continue
                    o_url = (
                        f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{ok}" if R2_PUBLIC_BASE_URL else
                        s3.meta.client.generate_presigned_url(
                            "get_object", Params={"Bucket": R2_BUCKET, "Key": ok}, ExpiresIn=60 * 60
                        )
                    )
                    original_lookup[ok] = {"url": o_url}
            except Exception:
                original_lookup = {}

            for key in keys:
                try:
                    item = _make_item_from_key(uid, key)
                    name = os.path.basename(key)
                    original_key = None
                    if "-o" in name:
                        try:
                            base_part = name.rsplit("-o", 1)[0]
                            for suf in ("-logo", "-txt"):
                                if base_part.endswith(suf):
                                    base_part = base_part[: -len(suf)]
                                    break
                            dir_part = os.path.dirname(key)  # users/uid/watermarked/YYYY/MM/DD
                            date_part = "/".join(dir_part.split("/")[-3:])
                            for ext in ("jpg","jpeg","png","webp","heic","tif","tiff","bin"):
                                cand = f"users/{uid}/originals/{date_part}/{base_part}-orig.{ext}" if ext != 'bin' else f"users/{uid}/originals/{date_part}/{base_part}-orig.bin"
                                if cand in original_lookup:
                                    original_key = cand
                                    break
                        except Exception:
                            original_key = None
                    if original_key and original_key in original_lookup:
                        item["original_key"] = original_key
                        item["original_url"] = original_lookup[original_key]["url"]
                    items.append(item)
                except Exception:
                    items.append(_make_item_from_key(uid, key))
        else:
            # Local storage: build a set of original keys available
            original_lookup: set[str] = set()
            orig_dir = os.path.join(STATIC_DIR, f"users/{uid}/originals/")
            if os.path.isdir(orig_dir):
                for root, _, files in os.walk(orig_dir):
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), STATIC_DIR).replace("\\", "/")
                        original_lookup.add(rel)
            for key in keys:
                item = _make_item_from_key(uid, key)
                try:
                    dir_part = os.path.dirname(key)  # users/uid/watermarked/YYYY/MM/DD
                    date_part = "/".join(dir_part.split("/")[-3:])
                    name = os.path.basename(key)
                    base_part = name.rsplit("-o", 1)[0] if "-o" in name else os.path.splitext(name)[0]
                    for suf in ("-logo", "-txt"):
                        if base_part.endswith(suf):
                            base_part = base_part[: -len(suf)]
                            break
                    for ext in ("jpg","jpeg","png","webp","heic","tif","tiff","bin"):
                        cand = f"users/{uid}/originals/{date_part}/{base_part}-orig.{ext}" if ext != 'bin' else f"users/{uid}/originals/{date_part}/{base_part}-orig.bin"
                        if cand in original_lookup:
                            item["original_key"] = cand
                            item["original_url"] = f"/static/{cand}"
                            break
                except Exception:
                    pass
                items.append(item)
        return {"photos": items}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/vaults/share")
async def vaults_share(request: Request, payload: dict = Body(...)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    vault = str((payload or {}).get('vault') or '').strip()
    email = str((payload or {}).get('email') or '').strip()
    if not vault or not email:
        return JSONResponse({"error": "vault and email required"}, status_code=400)
    # Validate vault exists and get normalized name
    try:
        keys = _read_vault(uid, vault)
        safe_vault = _vault_key(uid, vault)[1]
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    try:
        _ = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    expires_at_str = (payload or {}).get('expires_at')
    expires_in_days = (payload or {}).get('expires_in_days')
    now = datetime.utcnow()
    if expires_at_str:
        try:
            exp = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
            if not exp.tzinfo:
                exp = exp.replace(tzinfo=None)
            expires_at_iso = exp.isoformat()
        except Exception:
            return JSONResponse({"error": "invalid expires_at"}, status_code=400)
    else:
        days = int(expires_in_days or 7)
        exp = now + timedelta(days=days)
        expires_at_iso = exp.isoformat()

    token = secrets.token_urlsafe(24)
    rec = {
        "token": token,
        "uid": uid,
        "vault": _vault_key(uid, vault)[1],
        "email": email.lower(),
        "expires_at": expires_at_iso,
        "used": False,
        "created_at": now.isoformat(),
        "max_uses": 1,
    }
    _write_json_key(_share_key(token), rec)

    front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    link = f"{front}/#share?token={token}"

    subject = "You have access to a photo vault"
    html = render_email(
        "email_basic.html",
        title="You've been granted access",
        intro=f"You have been granted one-time access to a photo vault.<br>This link expires on: <strong>{expires_at_iso}</strong>",
        button_label="Open vault",
        button_url=link,
        footer_note="If you did not expect this email, you can ignore it.",
    )
    text = f"Open this link to view the shared vault (expires {expires_at_iso}): {link}"

    sent = send_email_smtp(email, subject, html, text)
    if not sent:
        logger.error("Failed to send share email")
        return JSONResponse({"error": "Failed to send email"}, status_code=500)

    return {"ok": True, "link": link, "expires_at": expires_at_iso}


@router.get("/vaults/shared/photos")
async def vaults_shared_photos(token: str):
    if not token or len(token) < 10:
        return JSONResponse({"error": "invalid token"}, status_code=400)

    rec = _read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)

    try:
        exp = datetime.fromisoformat(str(rec.get('expires_at', '')))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    # Allow multiple uses until expiration; ignore any previous 'used' state

    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    email = (rec.get('email') or '').lower()
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)

    try:
        keys = _read_vault(uid, vault)
        items = [_make_item_from_key(uid, k) for k in keys]
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    # Load approvals map to let client show statuses
    approvals = _read_json_key(_approval_key(uid, vault)) or {}

    return {"photos": items, "vault": vault, "email": email, "approvals": approvals}


def _update_approvals(uid: str, vault: str, photo_key: str, client_email: str, action: str, comment: str | None = None) -> dict:
    """Update approvals file for a vault and return the full approvals map."""
    # Normalize
    action_norm = "approved" if action.lower().startswith("approv") else ("denied" if action.lower().startswith("deny") else None)
    if not action_norm:
        raise ValueError("invalid action")
    client_email = (client_email or "").lower()
    data = _read_json_key(_approval_key(uid, vault)) or {}
    by_photo = data.get("by_photo") or {}
    photo = by_photo.get(photo_key) or {}
    by_email = photo.get("by_email") or {}
    by_email[client_email] = {
        "status": action_norm,
        "comment": (comment or ""),
        "at": datetime.utcnow().isoformat(),
    }
    photo["by_email"] = by_email
    by_photo[photo_key] = photo
    data["by_photo"] = by_photo
    _write_json_key(_approval_key(uid, vault), data)
    return data


@router.post("/vaults/shared/approve")
async def vaults_shared_approve(payload: ApprovalPayload):
    token = (payload.token or "").strip()
    photo_key = (payload.key or "").strip()
    action = (payload.action or "").strip().lower()
    comment = (payload.comment or "").strip()
    if not token or not photo_key or not action:
        return JSONResponse({"error": "token, key and action required"}, status_code=400)

    rec = _read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "invalid token"}, status_code=400)

    # Expiry check
    try:
        exp = datetime.fromisoformat(str(rec.get('expires_at', '')))
    except Exception:
        exp = None
    now = datetime.utcnow()
    if exp and now > exp:
        return JSONResponse({"error": "expired"}, status_code=410)

    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    client_email = (rec.get('email') or '').lower()
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)

    # Validate photo belongs to this uid and vault
    try:
        keys = _read_vault(uid, vault)
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    if photo_key not in keys:
        return JSONResponse({"error": "photo not in vault"}, status_code=400)

    try:
        data = _update_approvals(uid, vault, photo_key, client_email, action, comment)
    except ValueError as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    except Exception as ex:
        logger.warning(f"update approvals failed: {ex}")
        return JSONResponse({"error": "failed to save"}, status_code=500)

    # Notify owner via email (best-effort)
    try:
        owner_email = (get_user_email_from_uid(uid) or "").strip()
        if owner_email:
            name = os.path.basename(photo_key)
            subject = f"{client_email} {('approved' if action.startswith('approv') else 'denied')} a photo in '{vault}'"
            intro = f"Client <strong>{client_email}</strong> <strong>{'approved' if action.startswith('approv') else 'denied'}</strong> the photo <strong>{name}</strong> in vault <strong>{vault}</strong>."
            if comment:
                intro += f"<br>Comment: {comment}"
            html = render_email(
                "email_basic.html",
                title="Client feedback received",
                intro=intro,
                button_label="Open Gallery",
                button_url=(os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/") + "/#gallery",
            )
            text = f"{client_email} {('approved' if action.startswith('approv') else 'denied')} the photo {name} in vault '{vault}'."
            send_email_smtp(owner_email, subject, html, text)
    except Exception:
        pass

    # Return current status for this photo
    by_email = (data.get("by_photo", {}).get(photo_key, {}).get("by_email", {}))
    return {"ok": True, "photo": photo_key, "by_email": by_email}


@router.get("/vaults/approvals")
async def vaults_approvals(request: Request, vault: str):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        safe_vault = _vault_key(uid, vault)[1]
        data = _read_json_key(_approval_key(uid, safe_vault)) or {}
        return {"vault": safe_vault, "approvals": data}
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)