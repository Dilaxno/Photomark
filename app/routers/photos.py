from fastapi import APIRouter, Request, Body, Response
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from typing import List, Optional
import os, json, re
from datetime import datetime
from io import BytesIO

from app.core.config import s3, R2_BUCKET, R2_PUBLIC_BASE_URL, STATIC_DIR as static_dir, logger
from app.core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access
from app.utils.storage import read_json_key, write_json_key, read_bytes_key, upload_bytes
from app.utils.invisible_mark import detect_signature, PAYLOAD_LEN
from io import BytesIO
from PIL import Image

router = APIRouter(prefix="/api", tags=["photos"])

# Cache key for invisible watermark detection results per user
# Layout: users/{uid}/_cache/invisible/{sha1_of_key}.json with { ok: bool, ts: iso }
import hashlib

def _cache_key_for_invisible(uid: str, photo_key: str) -> str:
    h = hashlib.sha1(photo_key.encode('utf-8')).hexdigest()
    return f"users/{uid}/_cache/invisible/{h}.json"


def _has_invisible_mark(uid: str, key: str) -> bool:
    """Best-effort detection with caching; returns True if invisible mark is detected."""
    try:
        ckey = _cache_key_for_invisible(uid, key)
        rec = read_json_key(ckey)
        if isinstance(rec, dict) and "ok" in rec:
            return bool(rec.get("ok"))
        data = read_bytes_key(key)
        if not data:
            write_json_key(ckey, {"ok": False, "ts": datetime.utcnow().isoformat()})
            return False
        try:
            img = Image.open(BytesIO(data))
        except Exception:
            write_json_key(ckey, {"ok": False, "ts": datetime.utcnow().isoformat()})
            return False
        try:
            payload = detect_signature(img, payload_len_bytes=PAYLOAD_LEN)
            ok = bool(payload)
        except Exception:
            ok = False
        write_json_key(ckey, {"ok": ok, "ts": datetime.utcnow().isoformat()})
        return ok
    except Exception:
        return False


def _build_manifest(uid: str) -> dict:
    items: list[dict] = []
    prefix = f"users/{uid}/watermarked/"
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        for obj in bucket.objects.filter(Prefix=prefix):
            key = obj.key
            if key.endswith("/_history.txt") or key.endswith("/"):
                continue
            last = getattr(obj, "last_modified", datetime.utcnow())
            if R2_PUBLIC_BASE_URL:
                url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"
            else:
                url = s3.meta.client.generate_presigned_url(
                    "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
                )
            items.append({
                "key": key,
                "url": url,
                "name": os.path.basename(key),
                "last": last.isoformat() if hasattr(last, "isoformat") else str(last),
            })
    else:
        dir_path = os.path.join(static_dir, prefix)
        if os.path.isdir(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    if f == "_history.txt":
                        continue
                    local_path = os.path.join(root, f)
                    rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                    items.append({
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": f,
                        "last": datetime.utcfromtimestamp(os.path.getmtime(local_path)).isoformat(),
                    })
    items.sort(key=lambda x: x.get("last", ""), reverse=True)
    top10 = [{"url": it["url"], "name": it["name"]} for it in items[:10]]
    return {"photos": top10}


@router.post("/embed/refresh")
async def api_embed_refresh(request: Request):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # Gallery/manifests are considered 'gallery' area
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid
    manifest = _build_manifest(uid)
    key = f"users/{uid}/embed/latest.json"
    payload = json.dumps(manifest, ensure_ascii=False)
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        bucket.put_object(Key=key, Body=payload.encode("utf-8"), ContentType="application/json", ACL="public-read")
        public_url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}" if R2_PUBLIC_BASE_URL else None
    else:
        path = os.path.join(static_dir, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        public_url = f"/static/{key}"
    return {"manifest": public_url or key}


@router.post("/embed/myuploads/refresh")
async def api_embed_myuploads_refresh(request: Request):
    """Regenerate My Uploads manifest for script-based embed.
    Produces users/{uid}/embed/myuploads.json listing latest external images.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    # Build simple external manifest (urls + names)
    items: list[dict] = []
    prefix = f"users/{uid}/external/"
    if s3 and R2_BUCKET:
        try:
            client = s3.meta.client
            params = {"Bucket": R2_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
            resp = client.list_objects_v2(**params)
            for entry in resp.get("Contents", []) or []:
                key = entry.get("Key", "")
                if not key or key.endswith("/"):
                    continue
                name = os.path.basename(key)
                url = (
                    f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}" if R2_PUBLIC_BASE_URL else
                    client.generate_presigned_url("get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60*60)
                )
                items.append({"url": url, "name": name})
        except Exception:
            items = []
    else:
        dir_path = os.path.join(static_dir, prefix)
        if os.path.isdir(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    local_path = os.path.join(root, f)
                    rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                    items.append({"url": f"/static/{rel}", "name": f})

    manifest = {"photos": items[:200]}
    key = f"users/{uid}/embed/myuploads.json"
    payload = json.dumps(manifest, ensure_ascii=False)
    if s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
        bucket.put_object(Key=key, Body=payload.encode("utf-8"), ContentType="application/json", ACL="public-read")
        public_url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}" if R2_PUBLIC_BASE_URL else None
    else:
        path = os.path.join(static_dir, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        public_url = f"/static/{key}"
    return {"manifest": public_url or key}


@router.get("/embed.myuploads.js")
async def embed_myuploads_js():
    js = f"""
(function(){{
  function render(container, data){{
    container.innerHTML='';
    var grid=document.createElement('div');
    grid.style.display='grid';
    grid.style.gridTemplateColumns='repeat(5,1fr)';
    grid.style.gap='8px';
    (data.photos||[]).slice(0,50).forEach(function(p){{
      var card=document.createElement('div');
      card.style.border='1px solid #333'; card.style.borderRadius='8px'; card.style.overflow='hidden'; card.style.background='rgba(0,0,0,0.2)';
      var img=document.createElement('img'); img.src=p.url; img.alt=p.name; img.style.width='100%'; img.style.height='140px'; img.style.objectFit='cover';
      var cap=document.createElement('div'); cap.textContent=p.name; cap.style.fontSize='12px'; cap.style.color='#aaa'; cap.style.padding='6px'; cap.style.whiteSpace='nowrap'; cap.style.textOverflow='ellipsis'; cap.style.overflow='hidden';
      card.appendChild(img); card.appendChild(cap); grid.appendChild(card);
    }});
    container.appendChild(grid);
  }}
  function load(el){{
    var uid=el.getAttribute('data-uid'); var manifest=el.getAttribute('data-manifest');
    if(!manifest) manifest=('""" + (R2_PUBLIC_BASE_URL.rstrip('/') if R2_PUBLIC_BASE_URL else '') + """' + '/users/'+uid+'/embed/myuploads.json');
    fetch(manifest,{{cache:'no-store'}}).then(function(r){{return r.json()}}).then(function(data){{render(el,data)}}).catch(function(){{el.innerHTML='Failed to load embed'}});
  }}
  if(document.currentScript){
    var sel=document.querySelectorAll('.photomark-embed, #photomark-embed');
    sel.forEach(function(el){{ load(el); }});
  }
}})();
"""
    return Response(content=js, media_type="application/javascript")


@router.get("/photos")
async def api_photos(
    request: Request,
    limit: int = 200,
    cursor: Optional[str] = None,
    include_original: bool = False,
    include_invisible: bool = False,
    compute_invisible: bool = False,
):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    items: list[dict] = []
    next_token: Optional[str] = None

    prefix = f"users/{uid}/watermarked/"
    if s3 and R2_BUCKET:
        try:
            client = s3.meta.client
            params = {
                "Bucket": R2_BUCKET,
                "Prefix": prefix,
                "MaxKeys": max(1, min(int(limit or 200), 1000)),
            }
            if cursor:
                params["ContinuationToken"] = cursor
            resp = client.list_objects_v2(**params)
            for entry in resp.get("Contents", []) or []:
                key = entry.get("Key", "")
                if not key or key.endswith("/") or key.endswith("/_history.txt"):
                    continue
                name = os.path.basename(key)
                url = (
                    f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}" if R2_PUBLIC_BASE_URL else
                    client.generate_presigned_url(
                        "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
                    )
                )
                item = {
                    "key": key,
                    "url": url,
                    "name": name,
                    "size": int(entry.get("Size", 0) or 0),
                    "last_modified": (entry.get("LastModified") or datetime.utcnow()).isoformat(),
                }
                # Optional: attach original mapping without costly scans
                if include_original:
                    try:
                        m = re.match(r"^(.+)-(\d+)-([a-z]+)-o([^.]+)\.jpg$", name, re.IGNORECASE)
                        if m:
                            base_part, stamp, _suffix, oext = m.group(1), m.group(2), m.group(3), m.group(4)
                            date_part = "/".join(os.path.dirname(key).split("/")[-3:])
                            original_key = f"users/{uid}/originals/{date_part}/{base_part}-{stamp}-orig.{oext}"
                            item["original_key"] = original_key
                            if R2_PUBLIC_BASE_URL:
                                item["original_url"] = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{original_key}"
                            else:
                                item["original_url"] = client.generate_presigned_url(
                                    "get_object", Params={"Bucket": R2_BUCKET, "Key": original_key}, ExpiresIn=60 * 60
                                )
                    except Exception:
                        pass
                # Optional: invisible mark flag using cache only unless compute_invisible is True
                if include_invisible:
                    try:
                        if compute_invisible:
                            item["has_invisible"] = _has_invisible_mark(uid, key)
                        else:
                            ckey = _cache_key_for_invisible(uid, key)
                            rec = read_json_key(ckey)
                            item["has_invisible"] = bool(rec.get("ok")) if isinstance(rec, dict) else False
                    except Exception:
                        item["has_invisible"] = False
                # Optional friend note sidecar read (lightweight)
                try:
                    if "-fromfriend-" in name:
                        meta_key = f"{os.path.splitext(key)[0]}.json"
                        meta = read_json_key(meta_key)
                        if isinstance(meta, dict) and (meta.get("note") or meta.get("from")):
                            item["friend_note"] = str(meta.get("note") or "")
                            if meta.get("from"):
                                item["friend_from"] = str(meta.get("from"))
                            if meta.get("at"):
                                item["friend_at"] = str(meta.get("at"))
                except Exception:
                    pass
                items.append(item)
            if resp.get("IsTruncated"):
                next_token = resp.get("NextContinuationToken")
        except Exception as ex:
            logger.exception(f"Failed listing R2 objects: {ex} | Params: limit={limit}, cursor={cursor}")
            return JSONResponse({"error": "List failed"}, status_code=500)
    else:
        # Local filesystem mode (development). Provide simple offset pagination via cursor=int index.
        try:
            dir_path = os.path.join(static_dir, prefix)
            if os.path.isdir(dir_path):
                all_files: list[tuple[str, float, int]] = []  # (rel_key, mtime, size)
                for root, _, files in os.walk(dir_path):
                    for f in files:
                        if f == "_history.txt":
                            continue
                        local_path = os.path.join(root, f)
                        rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                        try:
                            mtime = os.path.getmtime(local_path)
                            size = os.path.getsize(local_path)
                        except Exception:
                            mtime = 0
                            size = 0
                        all_files.append((rel, mtime, size))
                # Sort latest first and paginate
                all_files.sort(key=lambda t: t[1], reverse=True)
                start = int(cursor or 0) if str(cursor or "").isdigit() else 0
                slice_files = all_files[start:start + max(1, min(int(limit or 200), 5000))]
                for rel, mtime, size in slice_files:
                    name = os.path.basename(rel)
                    item = {
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": name,
                        "size": int(size),
                        "last_modified": datetime.utcfromtimestamp(mtime).isoformat() if mtime else datetime.utcnow().isoformat(),
                    }
                    if include_original:
                        try:
                            m = re.match(r"^(.+)-(\d+)-([a-z]+)-o([^.]+)\.jpg$", name, re.IGNORECASE)
                            if m:
                                base_part, stamp, _suffix, oext = m.group(1), m.group(2), m.group(3), m.group(4)
                                date_part = "/".join(os.path.dirname(rel).split("/")[-3:])
                                original_key = f"users/{uid}/originals/{date_part}/{base_part}-{stamp}-orig.{oext}"
                                item["original_key"] = original_key
                                item["original_url"] = f"/static/{original_key}"
                        except Exception:
                            pass
                    if include_invisible:
                        try:
                            if compute_invisible:
                                item["has_invisible"] = _has_invisible_mark(uid, rel)
                            else:
                                ckey = _cache_key_for_invisible(uid, rel)
                                rec = read_json_key(ckey)
                                item["has_invisible"] = bool(rec.get("ok")) if isinstance(rec, dict) else False
                        except Exception:
                            item["has_invisible"] = False
                    if "-fromfriend-" in name:
                        try:
                            meta_key = f"{os.path.splitext(rel)[0]}.json"
                            meta = read_json_key(meta_key)
                            if isinstance(meta, dict) and (meta.get("note") or meta.get("from")):
                                item["friend_note"] = str(meta.get("note") or "")
                                if meta.get("from"):
                                    item["friend_from"] = str(meta.get("from"))
                                if meta.get("at"):
                                    item["friend_at"] = str(meta.get("at"))
                        except Exception:
                            pass
                    items.append(item)
                if (start + len(slice_files)) < len(all_files):
                    next_token = str(start + len(slice_files))
        except Exception as ex:
            logger.exception(f"Local listing failed: {ex}")
            return JSONResponse({"error": "List failed"}, status_code=500)

    resp = {"photos": items}
    if next_token:
        resp["next"] = next_token
    return resp


@router.get("/photos/partners")
async def api_photos_partners(
    request: Request,
    limit: int = 200,
    cursor: Optional[str] = None,
    include_original: bool = False,
    include_invisible: bool = False,
    compute_invisible: bool = False,
):
    """List only collaborator-sent gallery JPEGs stored under users/{uid}/partners/.
    Mirrors /api/photos with friend meta support but different prefix.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    items: list[dict] = []
    next_token: Optional[str] = None

    prefix = f"users/{uid}/partners/"
    if s3 and R2_BUCKET:
        try:
            client = s3.meta.client
            params = {
                "Bucket": R2_BUCKET,
                "Prefix": prefix,
                "MaxKeys": max(1, min(int(limit or 200), 1000)),
            }
            if cursor:
                params["ContinuationToken"] = cursor
            resp = client.list_objects_v2(**params)
            for entry in resp.get("Contents", []) or []:
                key = entry.get("Key", "")
                if not key or key.endswith("/") or key.endswith("/_history.txt"):
                    continue
                name = os.path.basename(key)
                url = (
                    f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}" if R2_PUBLIC_BASE_URL else
                    client.generate_presigned_url(
                        "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
                    )
                )
                item = {
                    "key": key,
                    "url": url,
                    "name": name,
                    "size": int(entry.get("Size", 0) or 0),
                    "last_modified": (entry.get("LastModified") or datetime.utcnow()).isoformat(),
                }
                if include_original:
                    try:
                        m = re.match(r"^(.+)-(\d+)-([a-z]+)-o([^.]+)\.jpg$", name, re.IGNORECASE)
                        if m:
                            base_part, stamp, _suffix, oext = m.group(1), m.group(2), m.group(3), m.group(4)
                            date_part = "/".join(os.path.dirname(key).split("/")[-3:])
                            original_key = f"users/{uid}/originals/{date_part}/{base_part}-{stamp}-orig.{oext}"
                            item["original_key"] = original_key
                            if R2_PUBLIC_BASE_URL:
                                item["original_url"] = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{original_key}"
                            else:
                                item["original_url"] = client.generate_presigned_url(
                                    "get_object", Params={"Bucket": R2_BUCKET, "Key": original_key}, ExpiresIn=60 * 60
                                )
                    except Exception:
                        pass
                if include_invisible:
                    try:
                        if compute_invisible:
                            item["has_invisible"] = _has_invisible_mark(uid, key)
                        else:
                            ckey = _cache_key_for_invisible(uid, key)
                            rec = read_json_key(ckey)
                            item["has_invisible"] = bool(rec.get("ok")) if isinstance(rec, dict) else False
                    except Exception:
                        item["has_invisible"] = False
                try:
                    if "-fromfriend-" in name or "-fromfriend" in name:
                        meta_key = f"{os.path.splitext(key)[0]}.json"
                        meta = read_json_key(meta_key)
                        if isinstance(meta, dict) and (meta.get("note") or meta.get("from")):
                            item["friend_note"] = str(meta.get("note") or "")
                            if meta.get("from"):
                                item["friend_from"] = str(meta.get("from"))
                            if meta.get("at"):
                                item["friend_at"] = str(meta.get("at"))
                except Exception:
                    pass
                items.append(item)
            if resp.get("IsTruncated"):
                next_token = resp.get("NextContinuationToken")
        except Exception as ex:
            logger.exception(f"Failed listing partners objects: {ex}")
            return JSONResponse({"error": "List failed"}, status_code=500)
    else:
        try:
            dir_path = os.path.join(static_dir, prefix)
            if os.path.isdir(dir_path):
                all_files: list[tuple[str, float, int]] = []
                for root, _, files in os.walk(dir_path):
                    for f in files:
                        if f == "_history.txt":
                            continue
                        local_path = os.path.join(root, f)
                        rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                        try:
                            mtime = os.path.getmtime(local_path)
                            size = os.path.getsize(local_path)
                        except Exception:
                            mtime = 0
                            size = 0
                        all_files.append((rel, mtime, size))
                all_files.sort(key=lambda t: t[1], reverse=True)
                start = int(cursor or 0) if str(cursor or "").isdigit() else 0
                slice_files = all_files[start:start + max(1, min(int(limit or 200), 5000))]
                for rel, mtime, size in slice_files:
                    name = os.path.basename(rel)
                    item = {
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": name,
                        "size": int(size),
                        "last_modified": datetime.utcfromtimestamp(mtime).isoformat() if mtime else datetime.utcnow().isoformat(),
                    }
                    if include_original:
                        try:
                            m = re.match(r"^(.+)-(\d+)-([a-z]+)-o([^.]+)\.jpg$", name, re.IGNORECASE)
                            if m:
                                base_part, stamp, _suffix, oext = m.group(1), m.group(2), m.group(3), m.group(4)
                                date_part = "/".join(os.path.dirname(rel).split("/")[-3:])
                                original_key = f"users/{uid}/originals/{date_part}/{base_part}-{stamp}-orig.{oext}"
                                item["original_key"] = original_key
                                item["original_url"] = f"/static/{original_key}"
                        except Exception:
                            pass
                    if include_invisible:
                        try:
                            if compute_invisible:
                                item["has_invisible"] = _has_invisible_mark(uid, rel)
                            else:
                                ckey = _cache_key_for_invisible(uid, rel)
                                rec = read_json_key(ckey)
                                item["has_invisible"] = bool(rec.get("ok")) if isinstance(rec, dict) else False
                        except Exception:
                            item["has_invisible"] = False
                    if "-fromfriend-" in name or "-fromfriend" in name:
                        try:
                            meta_key = f"{os.path.splitext(rel)[0]}.json"
                            meta = read_json_key(meta_key)
                            if isinstance(meta, dict) and (meta.get("note") or meta.get("from")):
                                item["friend_note"] = str(meta.get("note") or "")
                                if meta.get("from"):
                                    item["friend_from"] = str(meta.get("from"))
                                if meta.get("at"):
                                    item["friend_at"] = str(meta.get("at"))
                        except Exception:
                            pass
                    items.append(item)
                if (start + len(slice_files)) < len(all_files):
                    next_token = str(start + len(slice_files))
        except Exception as ex:
            logger.exception(f"Local listing failed: {ex}")
            return JSONResponse({"error": "List failed"}, status_code=500)

    resp = {"photos": items}
    if next_token:
        resp["next"] = next_token
    return resp


@router.post("/photos/rename")
async def api_photos_rename(request: Request, payload: dict = Body(...)):
    """Rename a photo within the user's gallery by changing the filename only.
    - Requires gallery access.
    - Only operates within users/{uid}/watermarked/ or users/{uid}/external/ prefixes.
    - Renames optional sidecar JSON if present.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    uid = eff_uid
    old_key = str((payload or {}).get("old_key") or "").strip().lstrip('/')
    new_name = str((payload or {}).get("new_name") or "").strip()

    if not old_key or not new_name:
        return JSONResponse({"error": "old_key and new_name are required"}, status_code=400)

    allowed_roots = (f"users/{uid}/watermarked/", f"users/{uid}/external/", f"users/{uid}/partners/")
    if not old_key.startswith(allowed_roots):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    # Disallow directory traversal and invalid characters
    if ("/" in new_name) or ("\\" in new_name) or any(c in new_name for c in [":", "*", "?", '"', "<", ">", "|"]):
        return JSONResponse({"error": "new_name contains invalid characters"}, status_code=400)
    if new_name.lower().endswith('.json'):
        return JSONResponse({"error": "new_name cannot end with .json"}, status_code=400)

    dir_part = os.path.dirname(old_key).replace("\\", "/")
    new_key = f"{dir_part}/{new_name}" if dir_part else new_name

    if new_key == old_key:
        # Nothing to do
        url = None
        if s3 and R2_BUCKET:
            try:
                url = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{new_key}" if R2_PUBLIC_BASE_URL else None
            except Exception:
                url = None
        else:
            url = f"/static/{new_key}"
        return {"ok": True, "key": new_key, "name": new_name, "url": url}

    # Ensure destination doesn't already exist
    if s3 and R2_BUCKET:
        client = s3.meta.client
        try:
            client.head_object(Bucket=R2_BUCKET, Key=old_key)
        except Exception:
            return JSONResponse({"error": "Source not found"}, status_code=404)
        try:
            client.head_object(Bucket=R2_BUCKET, Key=new_key)
            return JSONResponse({"error": "Destination already exists"}, status_code=409)
        except Exception:
            pass  # Not found -> OK
        try:
            client.copy_object(
                Bucket=R2_BUCKET,
                Key=new_key,
                CopySource={"Bucket": R2_BUCKET, "Key": old_key},
                MetadataDirective='COPY',
                ACL='public-read',
            )
            client.delete_object(Bucket=R2_BUCKET, Key=old_key)
            # Sidecar JSON (friend note) rename if exists
            old_meta = f"{os.path.splitext(old_key)[0]}.json"
            new_meta = f"{os.path.splitext(new_key)[0]}.json"
            try:
                client.head_object(Bucket=R2_BUCKET, Key=old_meta)
                client.copy_object(
                    Bucket=R2_BUCKET,
                    Key=new_meta,
                    CopySource={"Bucket": R2_BUCKET, "Key": old_meta},
                    MetadataDirective='COPY',
                    ACL='public-read',
                )
                client.delete_object(Bucket=R2_BUCKET, Key=old_meta)
            except Exception:
                pass
            url = (
                f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{new_key}" if R2_PUBLIC_BASE_URL else
                client.generate_presigned_url('get_object', Params={"Bucket": R2_BUCKET, "Key": new_key}, ExpiresIn=60*60)
            )
            return {"ok": True, "key": new_key, "name": new_name, "url": url}
        except Exception as ex:
            logger.exception(f"Rename failed (R2) {old_key} -> {new_key}: {ex}")
            # Best-effort cleanup if destination partially exists
            try:
                client.delete_object(Bucket=R2_BUCKET, Key=new_key)
            except Exception:
                pass
            return JSONResponse({"error": "Rename failed"}, status_code=500)
    else:
        # Local filesystem mode
        src_path = os.path.join(static_dir, old_key)
        dst_path = os.path.join(static_dir, new_key)
        if not os.path.isfile(src_path):
            return JSONResponse({"error": "Source not found"}, status_code=404)
        if os.path.exists(dst_path):
            return JSONResponse({"error": "Destination already exists"}, status_code=409)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        try:
            os.rename(src_path, dst_path)
            # Sidecar JSON rename if exists
            old_meta = f"{os.path.splitext(src_path)[0]}.json"
            new_meta = f"{os.path.splitext(dst_path)[0]}.json"
            if os.path.isfile(old_meta):
                try:
                    os.rename(old_meta, new_meta)
                except Exception:
                    pass
            url = f"/static/{new_key}"
            return {"ok": True, "key": new_key, "name": new_name, "url": url}
        except Exception as ex:
            logger.exception(f"Rename failed (local) {old_key} -> {new_key}: {ex}")
            return JSONResponse({"error": "Rename failed"}, status_code=500)


@router.get("/photos/external")
async def api_photos_external(
    request: Request,
    limit: int = 200,
    cursor: Optional[str] = None,
    include_original: bool = False,
    include_invisible: bool = False,
    compute_invisible: bool = False,
):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    items: list[dict] = []
    next_token: Optional[str] = None

    prefix = f"users/{uid}/external/"
    if s3 and R2_BUCKET:
        try:
            client = s3.meta.client
            params = {
                "Bucket": R2_BUCKET,
                "Prefix": prefix,
                "MaxKeys": max(1, min(int(limit or 200), 1000)),
            }
            if cursor:
                params["ContinuationToken"] = cursor
            resp = client.list_objects_v2(**params)
            for entry in resp.get("Contents", []) or []:
                key = entry.get("Key", "")
                if not key or key.endswith("/") or key.endswith("/_history.txt"):
                    continue
                name = os.path.basename(key)
                if "-fromfriend" in name:
                    # Exclude partner-sent items from external listing
                    continue
                url = (
                    f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}" if R2_PUBLIC_BASE_URL else
                    client.generate_presigned_url(
                        "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
                    )
                )
                item = {
                    "key": key,
                    "url": url,
                    "name": name,
                    "size": int(entry.get("Size", 0) or 0),
                    "last_modified": (entry.get("LastModified") or datetime.utcnow()).isoformat(),
                }
                if include_original:
                    try:
                        m = re.match(r"^(.+)-(\d+)-([a-z]+)-o([^.]+)\.jpg$", name, re.IGNORECASE)
                        if m:
                            base_part, stamp, _suffix, oext = m.group(1), m.group(2), m.group(3), m.group(4)
                            date_part = "/".join(os.path.dirname(key).split("/")[-3:])
                            original_key = f"users/{uid}/originals/{date_part}/{base_part}-{stamp}-orig.{oext}"
                            item["original_key"] = original_key
                            if R2_PUBLIC_BASE_URL:
                                item["original_url"] = f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{original_key}"
                            else:
                                item["original_url"] = client.generate_presigned_url(
                                    "get_object", Params={"Bucket": R2_BUCKET, "Key": original_key}, ExpiresIn=60 * 60
                                )
                    except Exception:
                        pass
                if include_invisible:
                    try:
                        if compute_invisible:
                            item["has_invisible"] = _has_invisible_mark(uid, key)
                        else:
                            ckey = _cache_key_for_invisible(uid, key)
                            rec = read_json_key(ckey)
                            item["has_invisible"] = bool(rec.get("ok")) if isinstance(rec, dict) else False
                    except Exception:
                        item["has_invisible"] = False
                items.append(item)
            if resp.get("IsTruncated"):
                next_token = resp.get("NextContinuationToken")
        except Exception as ex:
            logger.exception(f"Failed listing external objects: {ex}")
            return JSONResponse({"error": "List failed"}, status_code=500)
    else:
        try:
            dir_path = os.path.join(static_dir, prefix)
            if os.path.isdir(dir_path):
                all_files: list[tuple[str, float, int]] = []
                for root, _, files in os.walk(dir_path):
                    for f in files:
                        if f == "_history.txt":
                            continue
                        local_path = os.path.join(root, f)
                        rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                        try:
                            mtime = os.path.getmtime(local_path)
                            size = os.path.getsize(local_path)
                        except Exception:
                            mtime = 0
                            size = 0
                        all_files.append((rel, mtime, size))
                all_files.sort(key=lambda t: t[1], reverse=True)
                start = int(cursor or 0) if str(cursor or "").isdigit() else 0
                slice_files = all_files[start:start + max(1, min(int(limit or 200), 5000))]
                for rel, mtime, size in slice_files:
                    name = os.path.basename(rel)
                    item = {
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": name,
                        "size": int(size),
                        "last_modified": datetime.utcfromtimestamp(mtime).isoformat() if mtime else datetime.utcnow().isoformat(),
                    }
                    if include_original:
                        try:
                            m = re.match(r"^(.+)-(\d+)-([a-z]+)-o([^.]+)\.jpg$", name, re.IGNORECASE)
                            if m:
                                base_part, stamp, _suffix, oext = m.group(1), m.group(2), m.group(3), m.group(4)
                                date_part = "/".join(os.path.dirname(rel).split("/")[-3:])
                                original_key = f"users/{uid}/originals/{date_part}/{base_part}-{stamp}-orig.{oext}"
                                item["original_key"] = original_key
                                item["original_url"] = f"/static/{original_key}"
                        except Exception:
                            pass
                    if include_invisible:
                        try:
                            if compute_invisible:
                                item["has_invisible"] = _has_invisible_mark(uid, rel)
                            else:
                                ckey = _cache_key_for_invisible(uid, rel)
                                rec = read_json_key(ckey)
                                item["has_invisible"] = bool(rec.get("ok")) if isinstance(rec, dict) else False
                        except Exception:
                            item["has_invisible"] = False
                    if "-fromfriend-" in name:
                        try:
                            meta_key = f"{os.path.splitext(rel)[0]}.json"
                            meta = read_json_key(meta_key)
                            if isinstance(meta, dict) and (meta.get("note") or meta.get("from")):
                                item["friend_note"] = str(meta.get("note") or "")
                                if meta.get("from"):
                                    item["friend_from"] = str(meta.get("from"))
                                if meta.get("at"):
                                    item["friend_at"] = str(meta.get("at"))
                        except Exception:
                            pass
                    items.append(item)
                if (start + len(slice_files)) < len(all_files):
                    next_token = str(start + len(slice_files))
        except Exception as ex:
            logger.exception(f"Local listing failed: {ex}")
            return JSONResponse({"error": "List failed"}, status_code=500)

    resp = {"photos": items}
    if next_token:
        resp["next"] = next_token
    return resp

@router.get("/photos/originals")
async def api_photos_originals(request: Request):
    """List only uploaded originals for the current user."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    items: list[dict] = []
    prefix = f"users/{uid}/originals/"
    if s3 and R2_BUCKET:
        try:
            bucket = s3.Bucket(R2_BUCKET)
            for obj in bucket.objects.filter(Prefix=prefix):
                key = obj.key
                if key.endswith("/"):
                    continue
                url = (
                    f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}" if R2_PUBLIC_BASE_URL else
                    s3.meta.client.generate_presigned_url(
                        "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
                    )
                )
                name = os.path.basename(key)
                item = {
                    "key": key,
                    "url": url,
                    "name": name,
                    "size": getattr(obj, "size", 0),
                    "last_modified": getattr(obj, "last_modified", datetime.utcnow()).isoformat(),
                }
                try:
                    # Attach friend note if this original corresponds to a fromfriend watermarked item
                    base = os.path.splitext(name)[0]
                    m = re.match(r"^(.+)-(\d+)-orig\.[^.]+$", name, re.IGNORECASE)
                    if m:
                        base_part, stamp = m.group(1), m.group(2)
                        # Check for watermarked counterpart to find meta json
                        date_part = "/".join(os.path.dirname(key).split("/")[-3:])
                        wm_prefix = f"users/{uid}/watermarked/{date_part}/{base_part}-{stamp}-"
                        # We don't know exact suffix; iterate over bucket listing for this prefix
                        found_wm = False
                        for obj2 in bucket.objects.filter(Prefix=wm_prefix):
                            if obj2.key.endswith('/'):
                                continue
                            found_wm = True
                            if '-fromfriend' in obj2.key:
                                meta_key = f"{os.path.splitext(obj2.key)[0]}.json"
                                meta = read_json_key(meta_key)
                                if isinstance(meta, dict) and (meta.get("note") or meta.get("from")):
                                    item["friend_note"] = str(meta.get("note") or "")
                                    if meta.get("from"):
                                        item["friend_from"] = str(meta.get("from"))
                                    if meta.get("at"):
                                        item["friend_at"] = str(meta.get("at"))
                        if found_wm:
                            item["has_watermarked"] = True
                except Exception:
                    pass
                items.append(item)
        except Exception as ex:
            logger.exception(f"Failed listing originals: {ex}")
    else:
        dir_path = os.path.join(static_dir, prefix)
        if os.path.isdir(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    local_path = os.path.join(root, f)
                    rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                    item = {
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": f,
                        "size": os.path.getsize(local_path),
                        "last_modified": datetime.utcfromtimestamp(os.path.getmtime(local_path)).isoformat(),
                    }
                    try:
                        # Attach friend note by scanning for watermarked counterpart meta json
                        m = re.match(r"^(.+)-(\d+)-orig\.[^.]+$", f, re.IGNORECASE)
                        if m:
                            base_part, stamp = m.group(1), m.group(2)
                            date_part = "/".join(os.path.dirname(rel).split("/")[-3:])
                            wm_dir = os.path.join(static_dir, f"users/{uid}/watermarked/{date_part}")
                            if os.path.isdir(wm_dir):
                                found_wm = False
                                for wf in os.listdir(wm_dir):
                                    if wf.startswith(f"{base_part}-{stamp}-"):
                                        found_wm = True
                                        if '-fromfriend' in wf:
                                            meta_rel = f"users/{uid}/watermarked/{date_part}/{os.path.splitext(wf)[0]}.json"
                                            meta = read_json_key(meta_rel)
                                            if isinstance(meta, dict) and (meta.get("note") or meta.get("from")):
                                                item["friend_note"] = str(meta.get("note") or "")
                                                if meta.get("from"):
                                                    item["friend_from"] = str(meta.get("from"))
                                                if meta.get("at"):
                                                    item["friend_at"] = str(meta.get("at"))
                                if found_wm:
                                    item["has_watermarked"] = True
                    except Exception:
                        pass
                    items.append(item)
    # Sort latest first for convenience
    items.sort(key=lambda x: x.get("last_modified", ""), reverse=True)
    return {"photos": items}


@router.post("/photos/delete")
async def api_photos_delete(request: Request, keys: List[str] = Body(..., embed=True)):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    # deletion is a gallery action (managing owner's gallery)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid
    if not keys:
        return JSONResponse({"error": "no keys"}, status_code=400)

    deleted: list[str] = []
    errors: list[str] = []

    # 1) Delete underlying objects from R2/local (plus friend sidecar .json if present)
    # Expand deletion set to include related peers (original <-> watermarked) and sidecars
    def _expand_with_peers(base_keys: List[str]) -> List[str]:
        full: set[str] = set()
        for k in base_keys:
            if not k.startswith(f"users/{uid}/"):
                continue
            full.add(k)
            # Sidecar JSON next to the image
            full.add(os.path.splitext(k)[0] + ".json")
            try:
                parts = k.split('/')
                # Expect users/{uid}/{area}/{YYYY}/{MM}/{DD}/filename
                if len(parts) >= 7:
                    area = parts[3]  # 'originals' or 'watermarked'
                    date_part = '/'.join(parts[4:7])
                    fname = parts[-1]
                    # Extract base and stamp if possible
                    m_orig = re.match(r"^(.+)-(\d+)-orig\.[^.]+$", fname, re.IGNORECASE)
                    m_wm = re.match(r"^(.+)-(\d+)-([a-z]+)-o[^.]+\.jpg$", fname, re.IGNORECASE)
                    if area == 'originals' and m_orig:
                        base, stamp = m_orig.group(1), m_orig.group(2)
                        # list and add all derived variants (watermarked and external) for this base-stamp
                        for area2 in ("watermarked", "external", "partners"):
                            prefix2 = f"users/{uid}/{area2}/{date_part}/{base}-{stamp}-"
                            if s3 and R2_BUCKET:
                                try:
                                    bucket = s3.Bucket(R2_BUCKET)
                                    for obj in bucket.objects.filter(Prefix=prefix2):
                                        if obj.key.endswith('/'):
                                            continue
                                        full.add(obj.key)
                                        full.add(os.path.splitext(obj.key)[0] + ".json")
                                except Exception:
                                    pass
                            else:
                                local_dir = os.path.join(static_dir, f"users/{uid}/{area2}/{date_part}")
                                if os.path.isdir(local_dir):
                                    for f in os.listdir(local_dir):
                                        if f.startswith(f"{base}-{stamp}-"):
                                            rel = f"users/{uid}/{area2}/{date_part}/{f}"
                                            full.add(rel)
                                            full.add(os.path.splitext(rel)[0] + ".json")
                    elif area in ('watermarked', 'external', 'partners') and m_wm:
                        base, stamp = m_wm.group(1), m_wm.group(2)
                        # Try to remove matching original (unknown ext)
                        for ext in ("jpg","jpeg","png","webp","heic","tif","tiff","bin"):
                            okey = f"users/{uid}/originals/{date_part}/{base}-{stamp}-orig.{ext if ext!='bin' else 'bin'}"
                            full.add(okey)
                            full.add(os.path.splitext(okey)[0] + ".json")
            except Exception:
                pass
        return sorted(set(full))

    allowed = [k for k in keys if k.startswith(f"users/{uid}/")]
    to_delete_all = _expand_with_peers(allowed)

    if s3 and R2_BUCKET:
        try:
            bucket = s3.Bucket(R2_BUCKET)
            # Bulk delete first
            objs = [{"Key": k} for k in to_delete_all]
            deleted_set: set[str] = set()
            if objs:
                try:
                    resp = bucket.delete_objects(Delete={"Objects": objs, "Quiet": False})
                    for d in resp.get("Deleted", []) or []:
                        k = d.get("Key")
                        if k:
                            deleted_set.add(k)
                    for e in resp.get("Errors", []) or []:
                        msg = e.get("Message") or str(e)
                        key = e.get("Key")
                        if key:
                            errors.append(f"{key}: {msg}")
                        else:
                            errors.append(str(e))
                except Exception as ex:
                    # Fall back to per-key below
                    logger.warning(f"Bulk delete failed, will retry per-key: {ex}")
            # Per-key fallback for any not reported deleted
            for k in to_delete_all:
                if k in deleted_set:
                    continue
                try:
                    obj = s3.Object(R2_BUCKET, k)
                    obj.delete()
                    deleted_set.add(k)
                except Exception as ex:
                    # Some providers return 204 even if missing; treat as best-effort
                    errors.append(f"{k}: {ex}")
            deleted.extend(sorted(deleted_set))
        except Exception as ex:
            logger.exception(f"Delete error: {ex}")
            errors.append(str(ex))
    else:
        # Local filesystem deletion
        to_delete_local = to_delete_all
        for k in to_delete_local:
            if not k.startswith(f"users/{uid}/"):
                errors.append(f"forbidden: {k}")
                continue
            path = os.path.join(static_dir, k)
            try:
                if os.path.exists(path):
                    os.remove(path)
                    deleted.append(k)
                else:
                    # Non-existent is fine
                    pass
            except Exception as ex:
                errors.append(f"{k}: {ex}")

    # 2) Purge deleted keys from all user vault manifests so links don't reappear
    try:
        to_purge = set(deleted)
        if to_purge:
            prefix = f"users/{uid}/vaults/"
            if s3 and R2_BUCKET:
                bucket = s3.Bucket(R2_BUCKET)
                for obj in bucket.objects.filter(Prefix=prefix):
                    vkey = obj.key
                    # Only process vault jsons, skip internal meta/approval dirs
                    if not vkey.endswith('.json'):
                        continue
                    if vkey.startswith(prefix + "_meta/") or vkey.startswith(prefix + "_approvals/"):
                        continue
                    data = read_json_key(vkey) or {}
                    keys_list = list(data.get('keys', []))
                    if not keys_list:
                        continue
                    remain = [k for k in keys_list if k not in to_purge]
                    if remain != keys_list:
                        write_json_key(vkey, {"keys": sorted(set(remain))})
            else:
                dir_path = os.path.join(static_dir, prefix)
                if os.path.isdir(dir_path):
                    for f in os.listdir(dir_path):
                        if not f.endswith('.json'):
                            continue
                        if f.startswith('_meta'):
                            continue
                        vpath = os.path.join(dir_path, f)
                        rel_key = os.path.relpath(vpath, static_dir).replace('\\', '/')
                        data = read_json_key(rel_key) or {}
                        keys_list = list(data.get('keys', []))
                        if not keys_list:
                            continue
                        remain = [k for k in keys_list if k not in to_purge]
                        if remain != keys_list:
                            write_json_key(rel_key, {"keys": sorted(set(remain))})
    except Exception as ex:
        logger.warning(f"Failed to purge vault references: {ex}")

    return {"deleted": deleted, "errors": errors}


@router.post("/photos/remove_watermark")
async def api_photos_remove_watermark(request: Request, payload: dict = Body(...)):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    key = str(payload.get("key") or "").strip().lstrip('/')
    if not key or not key.startswith(f"users/{uid}/"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if "/watermarked/" not in key:
        return JSONResponse({"error": "Only watermarked items can be restored"}, status_code=400)

    # Derive original key using the naming convention
    name = os.path.basename(key)
    try:
        m = re.match(r"^(.+)-(\d+)-([a-z]+)-o([^.]+)\.jpg$", name, re.IGNORECASE)
        if not m:
            return JSONResponse({"error": "Cannot derive original for this item"}, status_code=400)
        base_part, stamp, _suffix, oext = m.group(1), m.group(2), m.group(3), m.group(4)
        date_part = "/".join(os.path.dirname(key).split("/")[-3:])
        original_key = f"users/{uid}/originals/{date_part}/{base_part}-{stamp}-orig.{oext}"
    except Exception:
        return JSONResponse({"error": "Cannot derive original for this item"}, status_code=400)

    # Read original bytes
    try:
        data = read_bytes_key(original_key)
        if not data:
            return JSONResponse({"error": "Original not found"}, status_code=404)
        # If original is JPEG, reuse bytes; otherwise convert to JPEG to match gallery format
        if oext.lower() in ("jpg", "jpeg"):
            out_bytes = data
        else:
            from PIL import Image
            try:
                img = Image.open(BytesIO(data)).convert("RGB")
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=95, subsampling=0, optimize=True)
                out_bytes = buf.getvalue()
            except Exception:
                return JSONResponse({"error": "Failed to convert original"}, status_code=500)
        # Write back to the same watermarked key to effectively remove watermark
        _ = upload_bytes(key, out_bytes, content_type="image/jpeg")
        return {"ok": True, "key": key}
    except Exception as ex:
        logger.exception(f"restore failed for {key}: {ex}")
        return JSONResponse({"error": "Restore failed"}, status_code=500)


@router.get("/photos/download/{key:path}")
async def api_photos_download(request: Request, key: str):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid
    key = (key or '').strip().lstrip('/')
    if not key.startswith(f"users/{uid}/"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    name = os.path.basename(key) or "file"
    if s3 and R2_BUCKET:
        try:
            obj = s3.Object(R2_BUCKET, key)
            res = obj.get()
            body = res.get("Body")
            ct = res.get("ContentType") or "application/octet-stream"
            headers = {"Content-Disposition": f'attachment; filename="{name}"'}
            def iter_chunks():
                while True:
                    chunk = body.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    yield chunk
            return StreamingResponse(iter_chunks(), media_type=ct, headers=headers)
        except Exception as ex:
            logger.exception(f"Download error for {key}: {ex}")
            return JSONResponse({"error": "Not found"}, status_code=404)
    else:
        path = os.path.join(static_dir, key)
        if not os.path.isfile(path):
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(path, filename=name, media_type="application/octet-stream")



@router.get("/embed.js")
async def embed_js():
    js = f"""
(function(){{
  function render(container, data){{
    container.innerHTML='';
    var grid=document.createElement('div');
    grid.style.display='grid';
    grid.style.gridTemplateColumns='repeat(5,1fr)';
    grid.style.gap='8px';
    (data.photos||[]).slice(0,10).forEach(function(p){{
      var card=document.createElement('div');
      card.style.border='1px solid #333'; card.style.borderRadius='8px'; card.style.overflow='hidden'; card.style.background='rgba(0,0,0,0.2)';
      var img=document.createElement('img'); img.src=p.url; img.alt=p.name; img.style.width='100%'; img.style.height='120px'; img.style.objectFit='cover';
      var cap=document.createElement('div'); cap.textContent=p.name; cap.style.fontSize='12px'; cap.style.color='#aaa'; cap.style.padding='6px'; cap.style.whiteSpace='nowrap'; cap.style.textOverflow='ellipsis'; cap.style.overflow='hidden';
      card.appendChild(img); card.appendChild(cap); grid.appendChild(card);
    }});
    container.appendChild(grid);
    var view=document.createElement('a'); view.textContent='View all gallery'; view.target='_blank'; view.style.display='inline-block'; view.style.marginTop='8px'; view.style.fontSize='13px'; view.style.color='#7aa2f7'; view.style.textDecoration='none';
    view.href='{R2_PUBLIC_BASE_URL or ''}'.startsWith('http') ? '{R2_PUBLIC_BASE_URL or ''}' : window.location.origin;
    container.appendChild(view);
  }}
  function load(el){{
    var uid=el.getAttribute('data-uid'); var manifest=el.getAttribute('data-manifest');
    if(!manifest) manifest=('""" + (R2_PUBLIC_BASE_URL.rstrip('/') if R2_PUBLIC_BASE_URL else '') + """' + '/users/'+uid+'/embed/latest.json');
    fetch(manifest,{cache:'no-store'}).then(function(r){return r.json()}).then(function(data){render(el,data)}).catch(function(){el.innerHTML='Failed to load embed'});
  }}
  if(document.currentScript){
    var sel=document.querySelectorAll('.photomark-embed, #photomark-embed');
    sel.forEach(function(el){ load(el); });
  }
}})();
"""
    return Response(content=js, media_type="application/javascript")