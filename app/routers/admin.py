import os
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.config import logger, s3, R2_BUCKET
from app.core.auth import get_fs_client

router = APIRouter(prefix="/api/admin", tags=["admin"])  # secure endpoints via ADMIN_SECRET


# --- Security helpers ---

def _get_admin_secret() -> str:
    return (os.getenv("ADMIN_SECRET") or "").strip()


def _extract_secret(request: Request, explicit: Optional[str] = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    # Header takes precedence if provided
    try:
      h = request.headers.get("X-Admin-Secret", "").strip()
      if h:
        return h
    except Exception:
      pass
    # Query fallback
    try:
      q = request.query_params.get("secret", "").strip()
      if q:
        return q
    except Exception:
      pass
    return ""


def _require_admin(request: Request, secret: Optional[str] = None) -> Optional[JSONResponse]:
    configured = _get_admin_secret()
    if not configured:
        return JSONResponse({"error": "admin_not_configured"}, status_code=503)
    provided = _extract_secret(request, secret)
    if not provided or provided != configured:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


# --- Models ---

class UpdateUserPayload(BaseModel):
    uid: str
    updates: Dict[str, Any]


class DeleteUserPayload(BaseModel):
    uid: str
    delete_storage: Optional[bool] = False


class BatchDeletePayload(BaseModel):
    uids: List[str]
    delete_storage: Optional[bool] = False


class BatchUpdatePayload(BaseModel):
    uids: List[str]
    updates: Dict[str, Any]


# --- Firestore helpers ---

_USER_FIELDS_INDEX = (
    "email",
    "name",
    "displayName",
    "studioName",
    "businessName",
    "brand_name",
    "handle",
    "role",
)


def _fetch_all_users(max_count: int = 1000) -> List[Tuple[str, Dict[str, Any]]]:
    db = get_fs_client()
    if not db:
        return []
    docs = list(db.collection("users").limit(max_count).stream())
    out: List[Tuple[str, Dict[str, Any]]] = []
    for d in docs:
        try:
            data = d.to_dict() or {}
            out.append((d.id, data))
        except Exception:
            continue
    return out


def _build_search_blob(uid: str, data: Dict[str, Any]) -> str:
    parts: List[str] = [uid]
    for f in _USER_FIELDS_INDEX:
        v = data.get(f)
        if v is not None:
            try:
                parts.append(str(v))
            except Exception:
                continue
    # Include any known contact/meta fields if present
    for k in ("phone", "company", "website", "bio"):
        v = data.get(k)
        if v is not None:
            try:
                parts.append(str(v))
            except Exception:
                pass
    blob = " ".join(parts)
    blob = re.sub(r"\s+", " ", blob).strip().lower()
    return blob


def _similarity(a: str, b: str) -> float:
    # Lightweight semantic-ish similarity: token overlap + partial ratio
    if not a or not b:
        return 0.0
    a = a.lower().strip()
    b = b.lower().strip()
    if not a or not b:
        return 0.0
    # Token overlap score (Jaccard)
    at = set(re.split(r"[^a-z0-9@._+-]+", a)) - {""}
    bt = set(re.split(r"[^a-z0-9@._+-]+", b)) - {""}
    inter = len(at & bt)
    union = len(at | bt) or 1
    jacc = inter / union
    # Partial ratio using simple longest common substring heuristic (bounded)
    best = 0
    la, lb = len(a), len(b)
    for i in range(min(la, 64)):
        for j in range(min(lb, 64)):
            k = 0
            while i + k < la and j + k < lb and a[i + k] == b[j + k] and k < 48:
                k += 1
            if k > best:
                best = k
    partial = best / max(1, min(len(a), len(b)))
    # Weighted combo
    return 0.65 * jacc + 0.35 * partial


# --- Endpoints ---

@router.get("/users")
async def admin_users_list(request: Request, secret: Optional[str] = None, limit: Optional[int] = 1000):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    try:
        lim = int(limit or 1000)
        users = _fetch_all_users(max_count=max(1, min(lim, 5000)))
        out = [
            {
                "uid": uid,
                "data": data,
                "summary": {
                    "email": str(data.get("email") or ""),
                    "name": str(data.get("name") or data.get("displayName") or ""),
                    "studio": str(data.get("studioName") or data.get("businessName") or data.get("brand_name") or ""),
                    "role": str(data.get("role") or ""),
                    "active": bool(data.get("is_active", True)),
                    "created_at": str(data.get("created_at") or data.get("createdAt") or ""),
                    "updated_at": str(data.get("updated_at") or data.get("updatedAt") or ""),
                    "last_login": str(data.get("last_login") or data.get("lastLogin") or ""),
                },
            }
            for uid, data in users
        ]
        return {"users": out}
    except Exception as ex:
        logger.warning(f"/api/admin/users failed: {ex}")
        return JSONResponse({"error": "list_failed"}, status_code=500)


@router.get("/users/search")
async def admin_users_search(request: Request, q: str, secret: Optional[str] = None, limit: Optional[int] = 200):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    query = (q or "").strip()
    if not query:
        return JSONResponse({"error": "q_required"}, status_code=400)
    try:
        users = _fetch_all_users(max_count=5000)
        qb = query.lower().strip()
        scored: List[Tuple[float, Tuple[str, Dict[str, Any]]]] = []
        for uid, data in users:
            blob = _build_search_blob(uid, data)
            score = _similarity(qb, blob)
            # Boost exact partial matches on key fields
            try:
                for f in _USER_FIELDS_INDEX:
                    v = str(data.get(f) or "").lower()
                    if v and qb in v:
                        score += 0.3
            except Exception:
                pass
            if score > 0:
                scored.append((min(score, 1.0), (uid, data)))
        scored.sort(key=lambda x: x[0], reverse=True)
        lim = int(limit or 200)
        out = [
            {
                "uid": uid,
                "score": round(sc, 4),
                "data": data,
                "summary": {
                    "email": str(data.get("email") or ""),
                    "name": str(data.get("name") or data.get("displayName") or ""),
                    "studio": str(data.get("studioName") or data.get("businessName") or data.get("brand_name") or ""),
                    "role": str(data.get("role") or ""),
                    "active": bool(data.get("is_active", True)),
                },
            }
            for sc, (uid, data) in scored[: max(1, min(lim, 1000))]
        ]
        return {"users": out, "query": query}
    except Exception as ex:
        logger.warning(f"/api/admin/users/search failed: {ex}")
        return JSONResponse({"error": "search_failed"}, status_code=500)


@router.post("/users/update")
async def admin_user_update(request: Request, payload: UpdateUserPayload, secret: Optional[str] = None):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    uid = (payload.uid or "").strip()
    if not uid:
        return JSONResponse({"error": "uid_required"}, status_code=400)
    updates = dict(payload.updates or {})
    if not updates:
        return JSONResponse({"error": "no_updates"}, status_code=400)
    try:
        db = get_fs_client()
        if not db:
            return JSONResponse({"error": "no_firestore"}, status_code=503)
        doc_ref = db.collection("users").document(uid)
        doc_ref.set(updates, merge=True)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"/api/admin/users/update failed: {ex}")
        return JSONResponse({"error": "update_failed"}, status_code=500)


@router.post("/users/delete")
async def admin_user_delete(request: Request, payload: DeleteUserPayload, secret: Optional[str] = None):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    uid = (payload.uid or "").strip()
    if not uid:
        return JSONResponse({"error": "uid_required"}, status_code=400)
    try:
        db = get_fs_client()
        if not db:
            return JSONResponse({"error": "no_firestore"}, status_code=503)
        # Delete Firestore doc
        try:
            db.collection("users").document(uid).delete()
        except Exception:
            pass
        # Optionally delete storage under users/{uid}/
        deleted_storage = []
        if payload.delete_storage and s3 and R2_BUCKET:
            try:
                bucket = s3.Bucket(R2_BUCKET)
                prefix = f"users/{uid}/"
                objs = list(bucket.objects.filter(Prefix=prefix))
                batch = [{"Key": o.key} for o in objs if o.key and not o.key.endswith("/")]
                if batch:
                    resp = bucket.delete_objects(Delete={"Objects": batch, "Quiet": True})
                    for d in (resp or {}).get("Deleted", []):
                        k = d.get("Key")
                        if k:
                            deleted_storage.append(k)
            except Exception as ex:
                logger.warning(f"storage cleanup failed for {uid}: {ex}")
        return {"ok": True, "uid": uid, "storage_deleted": len(deleted_storage)}
    except Exception as ex:
        logger.warning(f"/api/admin/users/delete failed: {ex}")
        return JSONResponse({"error": "delete_failed"}, status_code=500)


@router.post("/users/batch_delete")
async def admin_users_batch_delete(request: Request, payload: BatchDeletePayload, secret: Optional[str] = None):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    uids = [str(u or "").strip() for u in (payload.uids or []) if str(u or "").strip()]
    if not uids:
        return JSONResponse({"error": "uids_required"}, status_code=400)
    ok: List[str] = []
    errors: List[str] = []
    for uid in uids:
        try:
            res = await admin_user_delete(request, DeleteUserPayload(uid=uid, delete_storage=payload.delete_storage), secret)
            if isinstance(res, JSONResponse):
                errors.append(f"{uid} : {getattr(res, 'status_code', 500)}")
            else:
                ok.append(uid)
        except Exception as ex:
            errors.append(f"{uid} : {ex}")
    return {"ok": True, "deleted": ok, "errors": errors}


@router.post("/users/batch_update")
async def admin_users_batch_update(request: Request, payload: BatchUpdatePayload, secret: Optional[str] = None):
    sec = _require_admin(request, secret)
    if sec is not None:
        return sec
    uids = [str(u or "").strip() for u in (payload.uids or []) if str(u or "").strip()]
    updates = dict(payload.updates or {})
    if not uids:
        return JSONResponse({"error": "uids_required"}, status_code=400)
    if not updates:
        return JSONResponse({"error": "no_updates"}, status_code=400)
    ok: List[str] = []
    errors: List[str] = []
    try:
        db = get_fs_client()
        if not db:
            return JSONResponse({"error": "no_firestore"}, status_code=503)
        for uid in uids:
            try:
                db.collection("users").document(uid).set(updates, merge=True)
                ok.append(uid)
            except Exception as ex:
                errors.append(f"{uid} : {ex}")
        return {"ok": True, "updated": ok, "errors": errors}
    except Exception as ex:
        logger.warning(f"/api/admin/users/batch_update failed: {ex}")
        return JSONResponse({"error": "batch_update_failed"}, status_code=500)
