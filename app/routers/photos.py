from fastapi import APIRouter, Request, Body, Response
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from typing import List
import os, json
from datetime import datetime

from app.core.config import s3, R2_BUCKET, R2_PUBLIC_BASE_URL, STATIC_DIR as static_dir, logger
from app.core.auth import get_uid_from_request

router = APIRouter(prefix="/api", tags=["photos"])


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
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
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
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    logger.info(f"Listing photos for user={uid}")
    items: list[dict] = []

    prefix = f"users/{uid}/watermarked/"
    if s3 and R2_BUCKET:
        try:
            cnt = 0
            bucket = s3.Bucket(R2_BUCKET)
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
                items.append({
                    "key": key,
                    "url": url,
                    "name": os.path.basename(key),
                    "size": getattr(obj, "size", 0),
                    "last_modified": getattr(obj, "last_modified", datetime.utcnow()).isoformat(),
                })
                cnt += 1
            logger.info(f"Listed {cnt} objects from R2 for {uid}")
        except Exception as ex:
            logger.exception(f"Failed listing R2 objects: {ex}")
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
                        "size": os.path.getsize(local_path),
                        "last_modified": datetime.utcfromtimestamp(os.path.getmtime(local_path)).isoformat(),
                    })

    dedup = {}
    for it in items:
        dedup[it["key"]] = it
    items = list(dedup.values())
    logger.info(f"Returning {len(items)} total items for {uid}")
    return {"photos": items}


@router.post("/photos/delete")
async def api_photos_delete(request: Request, keys: List[str] = Body(..., embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not keys:
        return JSONResponse({"error": "no keys"}, status_code=400)

    deleted: list[str] = []
    errors: list[str] = []
    if s3 and R2_BUCKET:
        try:
            bucket = s3.Bucket(R2_BUCKET)
            allowed = [k for k in keys if k.startswith(f"users/{uid}/")]
            objs = [{"Key": k} for k in allowed]
            if objs:
                resp = bucket.delete_objects(Delete={"Objects": objs, "Quiet": True})
                for d in resp.get("Deleted", []):
                    deleted.append(d.get("Key"))
        except Exception as ex:
            logger.exception(f"Delete error: {ex}")
            errors.append(str(ex))
    else:
        for k in keys:
            if not k.startswith(f"users/{uid}/"):
                errors.append(f"forbidden: {k}")
                continue
            path = os.path.join(static_dir, k)
            try:
                if os.path.exists(path):
                    os.remove(path)
                    deleted.append(k)
            except Exception as ex:
                errors.append(f"{k}: {ex}")

    return {"deleted": deleted, "errors": errors}


@router.get("/photos/download/{key:path}")
async def api_photos_download(request: Request, key: str):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
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