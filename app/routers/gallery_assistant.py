import os
import json
from typing import Any, Dict, List
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import httpx

from app.core.config import logger, GROQ_API_KEY, s3, R2_BUCKET, R2_PUBLIC_BASE_URL, STATIC_DIR as static_dir
from app.core.auth import resolve_workspace_uid, has_role_access

router = APIRouter(prefix="/api/gallery/assistant", tags=["gallery_assistant"])

SYSTEM_PROMPT = (
    "You are Mark, a gallery assistant for managing a user's photo library. "
    "Understand natural language requests and output a strict JSON object with fields: "
    "{\n  \"reply\": string,\n  \"commands\": [ { \"op\": string, \"args\": object } ]\n}. "
    "Supported commands: \n"
    "- delete_by_name: { contains: string }  // Case-insensitive substring match on file name (without path).\n"
    "Only emit supported commands. Keep reply short and clear."
)

async def _groq_json(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    if not GROQ_API_KEY:
        return {"reply": "Assistant is not configured.", "commands": []}
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "gpt-oss-120b",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        content = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "{}")
        try:
            obj = json.loads(content)
        except Exception:
            obj = {"reply": str(content)[:400], "commands": []}
        if not isinstance(obj, dict):
            obj = {"reply": str(content)[:400], "commands": []}
        obj.setdefault("reply", "")
        obj.setdefault("commands", [])
        if not isinstance(obj.get("commands"), list):
            obj["commands"] = []
        return obj

async def _list_photos(uid: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    prefix = f"users/{uid}/watermarked/"
    if s3 and R2_BUCKET:
        try:
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
                })
        except Exception as ex:
            logger.exception(f"list photos failed: {ex}")
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
                        "size": os.path.getsize(local_path) if os.path.exists(local_path) else 0,
                    })
    return items

async def _delete_keys(uid: str, keys: List[str]) -> Dict[str, List[str]]:
    deleted: List[str] = []
    errors: List[str] = []
    if s3 and R2_BUCKET:
        try:
            bucket = s3.Bucket(R2_BUCKET)
            allowed = [k for k in keys if k.startswith(f"users/{uid}/")]
            objs = [{"Key": k} for k in allowed]
            if objs:
                resp = bucket.delete_objects(Delete={"Objects": objs, "Quiet": False})
                for d in resp.get("Deleted", []):
                    k = d.get("Key")
                    if k:
                        deleted.append(k)
                for e in resp.get("Errors", []):
                    msg = e.get("Message") or str(e)
                    key = e.get("Key")
                    errors.append(f"{key or ''}: {msg}")
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

@router.post("/chat")
async def chat(request: Request, body: Dict[str, Any]):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    raw_msgs = body.get("messages")
    if not isinstance(raw_msgs, list) or not raw_msgs:
        return JSONResponse({"error": "messages required"}, status_code=400)

    # Ask Groq to turn conversation into structured commands + short reply.
    plan = await _groq_json([m for m in raw_msgs if isinstance(m, dict) and 'role' in m and 'content' in m])
    logger.info(f"assistant plan: {plan}")

    executed: Dict[str, Any] = {"deleted": [], "errors": []}

    # Execute supported commands
    try:
        commands = plan.get("commands") or []
        if isinstance(commands, list):
            # Preload photos only if needed
            photos_cache: List[Dict[str, Any]] | None = None
            for cmd in commands:
                op = (cmd or {}).get("op")
                args = (cmd or {}).get("args") or {}
                if op == "delete_by_name":
                    contains = str(args.get("contains") or "").strip()
                    if not contains:
                        continue
                    if photos_cache is None:
                        photos_cache = await _list_photos(eff_uid)
                    needle = contains.lower()
                    keys = [it["key"] for it in photos_cache if needle in (it.get("name") or "").lower()]
                    if keys:
                        res = await _delete_keys(eff_uid, keys)
                        executed["deleted"] = list(set(list(executed.get("deleted", [])) + res.get("deleted", [])))
                        executed["errors"] = list(set(list(executed.get("errors", [])) + res.get("errors", [])))
    except Exception as ex:
        logger.exception(f"assistant execute error: {ex}")

    reply = plan.get("reply") or "Done."
    return {"reply": reply, "executed": executed}