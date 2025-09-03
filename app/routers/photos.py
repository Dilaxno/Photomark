from fastapi import APIRouter, Request, Body, Response
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from typing import List
import os, json, re
from datetime import datetime

from app.core.config import s3, R2_BUCKET, R2_PUBLIC_BASE_URL, STATIC_DIR as static_dir, logger
from app.core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access
from app.utils.storage import read_json_key, write_json_key, read_bytes_key
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


@router.get("/photos")
async def api_photos(request: Request):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    uid = eff_uid

    logger.info(f"Listing photos for user={uid}")
    items: list[dict] = []

    prefix = f"users/{uid}/watermarked/"
    if s3 and R2_BUCKET:
        try:
            cnt = 0
            bucket = s3.Bucket(R2_BUCKET)
            # Build a quick lookup for originals to attach to items
            orig_prefix = f"users/{uid}/originals/"
            original_lookup: dict[str, dict] = {}
            try:
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

            for obj in bucket.objects.filter(Prefix=prefix):
                key = obj.key
                if key.endswith("/_history.txt") or key.endswith("/"):
                    continue
                url = (
                    f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}" if R2_PUBLIC_BASE_URL else
                    s3.meta.client.generate_presigned_url(
                        "get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=60 * 60
                    )
                )
                name = os.path.basename(key)

                # Try to derive corresponding original key from naming convention "...-o<ext>.jpg"
                original_key = None
                if "-o" in name:
                    try:
                        base_part = name.rsplit("-o", 1)[0]
                        # Watermarked name includes a suffix (-logo or -txt). Remove it to match original.
                        for suf in ("-logo", "-txt"):
                            if base_part.endswith(suf):
                                base_part = base_part[: -len(suf)]
                                break
                        # original lived under /originals/ with suffix -orig.<ext>
                        # Find directory date component from key
                        dir_part = os.path.dirname(key)  # users/uid/watermarked/YYYY/MM/DD
                        date_part = "/".join(dir_part.split("/")[-3:])
                        # We don't know ext; scan known list
                        for ext in ("jpg","jpeg","png","webp","heic","tif","tiff","bin"):
                            cand = f"users/{uid}/originals/{date_part}/{base_part}-orig.{ext}" if ext != 'bin' else f"users/{uid}/originals/{date_part}/{base_part}-orig.bin"
                            if cand in original_lookup:
                                original_key = cand
                                break
                    except Exception:
                        original_key = None

                item = {
                    "key": key,
                    "url": url,
                    "name": name,
                    "size": getattr(obj, "size", 0),
                    "last_modified": getattr(obj, "last_modified", datetime.utcnow()).isoformat(),
                }
                # Attach invisible watermark detection flag (cached)
                try:
                    item["has_invisible"] = _has_invisible_mark(uid, key)
                except Exception:
                    item["has_invisible"] = False
                if original_key and original_key in original_lookup:
                    item["original_key"] = original_key
                    item["original_url"] = original_lookup[original_key]["url"]
                # Attach optional friend note if exists: same path as image but .json
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
                cnt += 1
            logger.info(f"Listed {cnt} objects from R2 for {uid}")
        except Exception as ex:
            logger.exception(f"Failed listing R2 objects: {ex}")
    else:
        dir_path = os.path.join(static_dir, prefix)
        if os.path.isdir(dir_path):
            # Build local originals lookup
            orig_dir = os.path.join(static_dir, f"users/{uid}/originals/")
            original_lookup: set[str] = set()
            if os.path.isdir(orig_dir):
                for root, _, files in os.walk(orig_dir):
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), static_dir).replace("\\", "/")
                        original_lookup.add(rel)
            for root, _, files in os.walk(dir_path):
                for f in files:
                    if f == "_history.txt":
                        continue
                    local_path = os.path.join(root, f)
                    rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                    item = {
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": f,
                        "size": os.path.getsize(local_path),
                        "last_modified": datetime.utcfromtimestamp(os.path.getmtime(local_path)).isoformat(),
                    }
                    # Attach invisible watermark detection flag (cached)
                    try:
                        item["has_invisible"] = _has_invisible_mark(uid, rel)
                    except Exception:
                        item["has_invisible"] = False
                    # Try to compute original based on directory and filename convention
                    try:
                        dir_part = os.path.dirname(rel)  # users/uid/watermarked/YYYY/MM/DD
                        date_part = "/".join(dir_part.split("/")[-3:])
                        base_part = f.rsplit("-o", 1)[0] if "-o" in f else os.path.splitext(f)[0]
                        # Watermarked name includes a suffix (-logo or -txt). Remove it to match original.
                        for suf in ("-logo", "-txt"):
                            if base_part.endswith(suf):
                                base_part = base_part[: -len(suf)]
                                break
                        # Try common original extensions
                        for ext in ("jpg","jpeg","png","webp","heic","tif","tiff","bin"):
                            cand = f"users/{uid}/originals/{date_part}/{base_part}-orig.{ext}" if ext != 'bin' else f"users/{uid}/originals/{date_part}/{base_part}-orig.bin"
                            if cand in original_lookup:
                                item["original_key"] = cand
                                item["original_url"] = f"/static/{cand}"
                                break
                    except Exception:
                        pass
                    # Attach optional friend note if exists for local storage
                    try:
                        if "-fromfriend-" in f:
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

    dedup = {}
    for it in items:
        dedup[it["key"]] = it
    items = list(dedup.values())
    logger.info(f"Returning {len(items)} total items for {uid}")
    return {"photos": items}


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
                items.append({
                    "key": key,
                    "url": url,
                    "name": name,
                    "size": getattr(obj, "size", 0),
                    "last_modified": getattr(obj, "last_modified", datetime.utcnow()).isoformat(),
                })
        except Exception as ex:
            logger.exception(f"Failed listing originals: {ex}")
    else:
        dir_path = os.path.join(static_dir, prefix)
        if os.path.isdir(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    local_path = os.path.join(root, f)
                    rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                    items.append({
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": f,
                        "size": os.path.getsize(local_path),
                        "last_modified": datetime.utcfromtimestamp(os.path.getmtime(local_path)).isoformat(),
                    })
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
                        wm_prefix = f"users/{uid}/watermarked/{date_part}/{base}-{stamp}-"
                        # list and add all watermarked variants for this base-stamp
                        if s3 and R2_BUCKET:
                            try:
                                bucket = s3.Bucket(R2_BUCKET)
                                for obj in bucket.objects.filter(Prefix=wm_prefix):
                                    if obj.key.endswith('/'):
                                        continue
                                    full.add(obj.key)
                                    full.add(os.path.splitext(obj.key)[0] + ".json")
                            except Exception:
                                pass
                        else:
                            local_dir = os.path.join(static_dir, f"users/{uid}/watermarked/{date_part}")
                            if os.path.isdir(local_dir):
                                for f in os.listdir(local_dir):
                                    if f.startswith(f"{base}-{stamp}-"):
                                        rel = f"users/{uid}/watermarked/{date_part}/{f}"
                                        full.add(rel)
                                        full.add(os.path.splitext(rel)[0] + ".json")
                    elif area == 'watermarked' and m_wm:
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