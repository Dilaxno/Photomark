from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import os
import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import logger
from app.core.auth import get_uid_from_request, get_user_email_from_uid
from app.utils.storage import read_json_key, write_json_key
from app.utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api/collab", tags=["collaboration"])

# Storage helpers

def _team_key(owner_uid: str) -> str:
    return f"users/{owner_uid}/collab/team.json"


def _owner_ptr_key(member_uid: str) -> str:
    return f"users/{member_uid}/collab/owner.json"


ALLOWED_ROLES = {"admin", "retoucher", "gallery_manager"}


def _read_team(owner_uid: str) -> dict:
    return read_json_key(_team_key(owner_uid)) or {"members": [], "invites": []}


def _write_team(owner_uid: str, data: dict):
    write_json_key(_team_key(owner_uid), data or {"members": [], "invites": []})


@router.get("/team")
async def get_team(request: Request):
    owner_uid = get_uid_from_request(request)
    if not owner_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    team = _read_team(owner_uid)
    return {"members": team.get("members", []), "invites": team.get("invites", [])}


@router.post("/invite")
async def invite_member(
    request: Request,
    email: str = Body(..., embed=True),
    role: str = Body(..., embed=True),
):
    owner_uid = get_uid_from_request(request)
    if not owner_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    email = (email or "").strip().lower()
    role = (role or "").strip().lower().replace("/", "_")
    if role not in ALLOWED_ROLES:
        return JSONResponse({"error": "Invalid role"}, status_code=400)
    if not email or "@" not in email:
        return JSONResponse({"error": "Invalid email"}, status_code=400)

    team = _read_team(owner_uid)
    # Deduplicate existing member/invite
    if any((m.get("email") or "").lower() == email for m in team.get("members", [])):
        return JSONResponse({"error": "Already a member"}, status_code=400)
    if any((i.get("email") or "").lower() == email for i in team.get("invites", [])):
        return JSONResponse({"error": "Already invited"}, status_code=400)

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    invite = {
        "token": token,
        "email": email,
        "role": role,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=7)).isoformat(),
    }
    team.setdefault("invites", []).append(invite)
    _write_team(owner_uid, team)

    # Write global token index so accept can resolve owner
    try:
        write_json_key(f"collab/invites/{token}.json", {"owner_uid": owner_uid, "token": token})
    except Exception:
        logger.warning("Failed to write invite index for token")

    # Build acceptance link (handled by frontend collaboration page)
    front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    accept_url = f"{front}#collaboration?accept_token={token}"

    html = render_email(
        "email_basic.html",
        title="You've been invited to collaborate",
        intro=f"You have been invited to join a workspace as <b>{role.replace('_','/')}</b>.<br/>Click the button below to accept.",
        button_url=accept_url,
        button_label="Accept Invitation",
        footer_note="If you did not expect this, you can ignore this message.",
    )
    subject = "Workspace collaboration invite"
    ok = send_email_smtp(email, subject, html)
    if not ok:
        logger.warning("Invite email send failed for %s", email)
    return {"ok": True, "token": token}


@router.post("/accept")
async def accept_invite(request: Request, token: str = Body(..., embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Search for the invite by scanning pointer owner(s) from token embedded path? We don't know owner.
    # Store invites only under owner; thus require owner lookup by token across potential owners is not feasible.
    # To keep simple: client calls this endpoint with token; we also pass owner_uid via hidden field when resolving UI.
    # But we can derive owner by storing a short index under a public key: collab/invites/{token}.json -> owner_uid
    index_key = f"collab/invites/{token}.json"
    index = read_json_key(index_key) or {}
    owner_uid = index.get("owner_uid")
    if not owner_uid:
        # Backward support: try owner list from env (none). Fail gracefully.
        return JSONResponse({"error": "Invalid or expired token"}, status_code=400)

    team = _read_team(owner_uid)
    inv = None
    invites = team.get("invites", [])
    for i in invites:
        if i.get("token") == token:
            inv = i
            break
    if not inv:
        return JSONResponse({"error": "Invalid or expired token"}, status_code=400)

    # Validate expiration
    try:
        exp = datetime.fromisoformat(inv.get("expires_at"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except Exception:
        exp = datetime.now(timezone.utc) - timedelta(seconds=1)
    if datetime.now(timezone.utc) > exp:
        # Remove expired invite
        try:
            team["invites"] = [i for i in invites if i.get("token") != token]
            _write_team(owner_uid, team)
        except Exception:
            pass
        return JSONResponse({"error": "Invite expired"}, status_code=400)

    # Ensure accepting user's email matches invite email
    acc_email = (get_user_email_from_uid(uid) or "").lower()
    if acc_email != (inv.get("email") or "").lower():
        return JSONResponse({"error": "This invite is for a different email"}, status_code=403)

    # Add member
    role = inv.get("role")
    member = {"uid": uid, "email": acc_email, "role": role, "joined_at": datetime.now(timezone.utc).isoformat()}
    # Replace existing membership by email if any
    members = [m for m in team.get("members", []) if (m.get("email") or "").lower() != acc_email]
    members.append(member)
    team["members"] = members
    # Remove invite
    team["invites"] = [i for i in invites if i.get("token") != token]
    _write_team(owner_uid, team)

    # Write reverse pointer for collaborator
    write_json_key(_owner_ptr_key(uid), {"owner_uid": owner_uid})

    return {"ok": True, "owner_uid": owner_uid, "role": role}


@router.post("/revoke")
async def revoke_member(
    request: Request,
    email: str = Body(None, embed=True),
    uid: str = Body(None, embed=True),
    token: str = Body(None, embed=True),
):
    owner_uid = get_uid_from_request(request)
    if not owner_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    team = _read_team(owner_uid)
    changed = False

    if token:
        before = len(team.get("invites", []))
        team["invites"] = [i for i in team.get("invites", []) if i.get("token") != token]
        changed = changed or (len(team.get("invites", [])) != before)
    if uid or email:
        email_l = (email or "").lower()
        before = len(team.get("members", []))
        team["members"] = [m for m in team.get("members", []) if (m.get("uid") != uid and (m.get("email") or "").lower() != email_l)]
        changed = changed or (len(team.get("members", [])) != before)

    if changed:
        _write_team(owner_uid, team)
    return {"ok": True}


@router.post("/resend-index")
async def _debug_make_index_for_invite(request: Request, token: str = Body(..., embed=True)):
    # This endpoint creates a global index for token -> owner, needed for acceptance.
    # It's called automatically by the UI right after creating an invite.
    owner_uid = get_uid_from_request(request)
    if not owner_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    write_json_key(f"collab/invites/{token}.json", {"owner_uid": owner_uid, "token": token})
    return {"ok": True}