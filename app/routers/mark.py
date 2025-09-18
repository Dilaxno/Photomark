import os
from typing import Any, Dict, List
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
import httpx

from app.core.config import logger, GROQ_API_KEY
from app.core.auth import get_uid_from_request, get_fs_client

router = APIRouter(prefix="/api/mark", tags=["mark_assistant"])  # Global Mark chat assistant

# Optional Firestore import for server timestamps
try:
    from firebase_admin import firestore as fb_fs  # type: ignore
except Exception:
    fb_fs = None  # type: ignore

PHOTOGRAPHY_SYSTEM_PROMPT = (
    "You are Mark — a world‑class photography mentor and business coach.\n"
    "You provide precise, technically accurate and pragmatic help for:\n"
    "• Cameras (DSLR, mirrorless, cinema, medium format), sensors, dynamic range, rolling/global shutter\n"
    "• Lenses & optics (f/ T‑stops, MTF, distortion, focus breathing), focal lengths per genre\n"
    "• Exposure math (Sunny 16, EV, ND math), metering, histogram, zebras, waveforms\n"
    "• Autofocus systems, tracking modes, calibration, back‑button focus\n"
    "• Lighting (speedlights, strobes, HSS/HS, continuous, CRI/TLCI, modifiers, inverse‑square law)\n"
    "• Color management (ICC profiles, calibration, white balance, ACES, LUTs), skin‑tone strategy\n"
    "• RAW pipelines and post (Lightroom, Capture One, Photoshop), denoise, sharpening, retouching ethics\n"
    "• Video (log curves, codecs/bit‑depth/ chroma subsampling, gimbals, timecode, audio basics)\n"
    "• Workflow/tethering, culling, file‑naming, backup & archival (3‑2‑1, checksums, off‑site), delivery\n"
    "• Printing (soft‑proofing, rendering intents, paper choice), albums, color targets\n"
    "• Business (pricing, packages, licensing/usage, contracts, model/property releases, taxes, insurance)\n"
    "• Marketing (portfolio curation, positioning, SEO for photographers, lead funnels, email, ads)\n"
    "• Studio ops & on‑set safety, client experience, pre‑production planning and checklists\n"
    "Answer with concise, step‑by‑step guidance. Include concrete numbers (settings, focal lengths, powers)\n"
    "and gear examples across budgets when relevant. When assumptions are unclear, ask one incisive\n"
    "clarifying question before prescribing a solution. Tone: professional, friendly, no fluff."
)


async def _groq_chat(messages: List[Dict[str, str]]) -> str:
    if not GROQ_API_KEY:
        return "Mark is not configured. Please set GROQ_API_KEY."
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    model = os.getenv("GROQ_MODEL_MARK", "llama-3.1-8b-instant")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": PHOTOGRAPHY_SYSTEM_PROMPT}] + messages,
        "temperature": 0.3,
        "max_tokens": 1200,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            content = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
            return content or ""
    except Exception as ex:
        logger.warning(f"Groq request failed: {ex}")
        return "I couldn't reach the assistant service. Please try again in a moment."


@router.post("/chat")
async def mark_chat(request: Request, body: Dict[str, Any] = Body(...)):
    raw_msgs = body.get("messages")
    if not isinstance(raw_msgs, list) or not raw_msgs:
        return JSONResponse({"error": "messages required"}, status_code=400)

    # messages format: [{ role: 'user'|'assistant'|'system', content: string }]
    safe_msgs: List[Dict[str, str]] = []
    for m in raw_msgs:
        try:
            role = str(m.get("role") or "").strip()
            content = str(m.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                safe_msgs.append({"role": role, "content": content})
        except Exception:
            continue
    if not safe_msgs:
        return JSONResponse({"error": "no valid messages"}, status_code=400)

    reply = await _groq_chat(safe_msgs)
    return {"reply": reply}


# ---------------- Chat persistence (Firestore) ----------------

def _messages_collection(db, uid: str, chat_id: str):
    return (
        db.collection('users')
          .document(uid)
          .collection('chats')
          .document(chat_id)
          .collection('messages')
    )


@router.post("/chats/{chat_id}/messages")
async def mark_add_message(request: Request, chat_id: str, body: Dict[str, Any] = Body(...)):
    """Add a new message to a user's chat.
    Path: users/{uid}/chats/{chatId}/messages/{messageId}
    Body: { sender: 'user' | 'Mark Photography Expert', text: string }
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    sender = str((body or {}).get('sender') or '').strip()
    text = str((body or {}).get('text') or '').strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    # Normalize sender to allowed set
    if sender.lower() in ("user", "me"):
        sender_norm = "user"
    else:
        sender_norm = "Mark Photography Expert"

    db = get_fs_client()
    if not db or not fb_fs:
        return JSONResponse({"error": "firestore unavailable"}, status_code=503)
    try:
        doc = {
            "sender": sender_norm,
            "text": text,
            "timestamp": fb_fs.SERVER_TIMESTAMP,  # type: ignore
        }
        ref = _messages_collection(db, uid, chat_id).document()
        ref.set(doc, merge=False)
        return {"ok": True, "id": ref.id}
    except Exception as ex:
        logger.warning(f"mark_add_message failed: {ex}")
        return JSONResponse({"error": "write_failed"}, status_code=500)


@router.get("/chats/{chat_id}/messages")
async def mark_fetch_messages(request: Request, chat_id: str):
    """Fetch all messages for a chatId ordered by timestamp ascending."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    db = get_fs_client()
    if not db or not fb_fs:
        return JSONResponse({"error": "firestore unavailable"}, status_code=503)
    try:
        q = _messages_collection(db, uid, chat_id).order_by('timestamp', direction=fb_fs.Query.ASCENDING)  # type: ignore
        out: List[Dict[str, Any]] = []
        for snap in q.stream():  # type: ignore[attr-defined]
            data = snap.to_dict() or {}
            ts = data.get('timestamp')
            # Normalize timestamp to ISO if available
            try:
                if ts is not None and hasattr(ts, 'isoformat'):
                    data['timestamp'] = ts.isoformat()
            except Exception:
                pass
            out.append({
                "id": getattr(snap, 'id', None),
                "sender": data.get('sender') or '',
                "text": data.get('text') or '',
                "timestamp": data.get('timestamp') or None,
            })
        return {"messages": out}
    except Exception as ex:
        logger.warning(f"mark_fetch_messages failed: {ex}")
        return JSONResponse({"error": "read_failed"}, status_code=500)
