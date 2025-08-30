from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from typing import Any, Dict, List, Optional
import os
import time

from app.core.auth import get_uid_from_request
from app.core.config import logger

# Firestore admin via firebase_admin
try:
    import firebase_admin
    from firebase_admin import firestore
except Exception:
    firebase_admin = None  # type: ignore
    firestore = None  # type: ignore

router = APIRouter(prefix="/api/replies", tags=["replies"])  # inbound + list


# Inbound webhook from your email provider
# Expected JSON (example):
# {
#   "from": {"email": "sender@example.com", "name": "Alice"},
#   "to": [{"email": "Marouane@photomark.cloud"}],
#   "subject": "Re: A small tool I built for photographers",
#   "text": "Reply body...",
#   "html": "<p>Reply body</p>",
#   "message_id": "<id@provider>",
#   "in_reply_to": "<original@id>"
# }
@router.post("/inbound")
async def inbound_email(request: Request, payload: Dict[str, Any] = Body(...)):
    provider_token = os.getenv("REPLY_WEBHOOK_TOKEN", "").strip()
    auth = request.headers.get("x-inbound-token", "").strip()
    if provider_token and auth != provider_token:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not firebase_admin or not firestore:
        logger.error("Firestore not initialized; cannot store replies")
        return JSONResponse({"error": "storage not configured"}, status_code=500)

    try:
        db = firestore.client()
        col = db.collection("outreach_replies")
        data = {
            "from_email": ((payload.get("from") or {}).get("email") or "").strip(),
            "from_name": ((payload.get("from") or {}).get("name") or "").strip(),
            "subject": (payload.get("subject") or "").strip(),
            "text": (payload.get("text") or "").strip(),
            "html": (payload.get("html") or "").strip(),
            "message_id": (payload.get("message_id") or "").strip(),
            "in_reply_to": (payload.get("in_reply_to") or "").strip(),
            "createdAt": firestore.SERVER_TIMESTAMP,
            "ts": int(time.time()),
        }
        # Basic validation
        if not data["from_email"] or not data["text"]:
            return JSONResponse({"error": "missing from_email or text"}, status_code=400)

        col.add(data)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"[replies.inbound] error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("")
async def list_replies(
    request: Request,
    limit: int = 50,
    before_ts: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    q: Optional[str] = None,
):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if not firebase_admin or not firestore:
        return JSONResponse({"error": "storage not configured"}, status_code=500)

    try:
        db = firestore.client()
        qry = db.collection("outreach_replies")
        # Range filters on ts
        if start_ts is not None:
            qry = qry.where("ts", ">=", int(start_ts))
        if end_ts is not None:
            qry = qry.where("ts", "<=", int(end_ts))
        if before_ts is not None:
            qry = qry.where("ts", "<", int(before_ts))
        qry = qry.order_by("ts", direction=firestore.Query.DESCENDING).limit(max(1, min(int(limit), 500)))

        docs = qry.stream()
        items: List[Dict[str, Any]] = []
        for d in docs:
            obj = d.to_dict() or {}
            obj["id"] = d.id
            items.append({
                "id": obj.get("id"),
                "from": {"email": obj.get("from_email", ""), "name": obj.get("from_name", "")},
                "subject": obj.get("subject", ""),
                "text": obj.get("text", ""),
                "html": obj.get("html", ""),
                "ts": int(obj.get("ts") or 0),
                "createdAt": str(obj.get("createdAt")),
            })

        # In-memory substring filter for subject/sender/body (case-insensitive)
        if q:
            ql = q.strip().lower()
            items = [it for it in items if (
                (it.get("subject") or "").lower().find(ql) >= 0 or
                (it.get("from", {}).get("email") or "").lower().find(ql) >= 0 or
                (it.get("from", {}).get("name") or "").lower().find(ql) >= 0 or
                (it.get("text") or "").lower().find(ql) >= 0
            )]

        next_cursor = items[-1]["ts"] if len(items) >= max(1, min(int(limit), 500)) else None
        return {"ok": True, "items": items, "nextCursor": next_cursor}
    except Exception as ex:
        logger.exception(f"[replies.list] error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)