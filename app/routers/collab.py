from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt

from app.core.config import logger, COLLAB_JWT_SECRET, COLLAB_JWT_TTL_DAYS
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

# Storage key helpers for collaborators
_DEF_COLLAB_ROOT = "collab"

def _invite_index_key(token: str) -> str:
    return f"collab/invites/{token}.json"

def _collab_user_key(owner_uid: str, email: str) -> str:
    safe_email = (email or "").lower()
    return f"collab/collaborators/{owner_uid}/{safe_email}.json"

def _email_index_key(email: str) -> str:
    safe_email = (email or "").lower()
    return f"collab/email-index/{safe_email}.json"

def _hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def _verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def _issue_collab_jwt(owner_uid: str, email: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "photomark.collab",
        "kind": "collab",
        "sub": f"collab:{owner_uid}:{email.lower()}",
        "owner_uid": owner_uid,
        "email": email.lower(),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=COLLAB_JWT_TTL_DAYS)).timestamp()),
    }
    return jwt.encode(payload, COLLAB_JWT_SECRET, algorithm="HS256")


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
    # Generate a strong random password for collaborator
    raw_password = secrets.token_urlsafe(16)
    pw_hash = _hash_password(raw_password)

    now = datetime.now(timezone.utc)
    invite = {
        "token": token,
        "email": email,
        "role": role,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=7)).isoformat(),
        # store password hash until first successful login
        "password_hash": pw_hash,
        "accepted": False,
    }
    team.setdefault("invites", []).append(invite)
    _write_team(owner_uid, team)

    # Write global token index so accept can resolve owner
    try:
        write_json_key(_invite_index_key(token), {"owner_uid": owner_uid, "token": token})
    except Exception:
        logger.warning("Failed to write invite index for token")

    # Maintain global email index for workspace selection at login
    try:
        idx_key = _email_index_key(email)
        idx = read_json_key(idx_key) or {"workspaces": []}
        if owner_uid not in idx.get("workspaces", []):
            idx.setdefault("workspaces", []).append(owner_uid)
        write_json_key(idx_key, idx)
    except Exception:
        logger.warning("Failed to update email index for %s", email)

    # Build acceptance link and include the generated password in the email content
    front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
    accept_url = f"{front}#collab-login?accept_token={token}"

    intro_html = (
        f"You have been invited to join a workspace as <b>{role.replace('_','/')}</b>.<br/>"
        f"Use the button to accept the invite.<br/><br/>"
        f"<b>Your collaborator password:</b> <code>{raw_password}</code><br/>"
        f"You can sign in later at the Collaborator Sign-In page using your email and this password."
    )

    html = render_email(
        "email_basic.html",
        title="You've been invited to collaborate",
        intro=intro_html,
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
    # Token-only acceptance (no Firebase auth required)
    index = read_json_key(_invite_index_key(token)) or {}
    owner_uid = index.get("owner_uid")
    if not owner_uid:
        return JSONResponse({"error": "Invalid or expired token"}, status_code=400)

    team = _read_team(owner_uid)
    inv: Optional[dict] = next((i for i in team.get("invites", []) if i.get("token") == token), None)
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
        try:
            team["invites"] = [i for i in team.get("invites", []) if i.get("token") != token]
            _write_team(owner_uid, team)
        except Exception:
            pass
        return JSONResponse({"error": "Invite expired"}, status_code=400)

    # Mark invite accepted but keep it until first successful collaborator login (per spec)
    inv["accepted"] = True
    _write_team(owner_uid, team)

    return {"ok": True, "owner_uid": owner_uid, "role": inv.get("role")}


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
    # Also clean collaborator credential if exists
    try:
        if email:
            cred_key = _collab_user_key(owner_uid, email)
            # Overwrite with minimal tombstone to remove password hash
            if read_json_key(cred_key):
                write_json_key(cred_key, {"owner_uid": owner_uid, "email": email.lower(), "revoked": True})
    except Exception:
        pass
    return {"ok": True}


@router.post("/resend-index")
async def _debug_make_index_for_invite(request: Request, token: str = Body(..., embed=True)):
    # This endpoint creates a global index for token -> owner, needed for acceptance.
    # It's called automatically by the UI right after creating an invite.
    owner_uid = get_uid_from_request(request)
    if not owner_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    write_json_key(_invite_index_key(token), {"owner_uid": owner_uid, "token": token})
    return {"ok": True}


@router.post("/login")
async def collab_login(email: str = Body(..., embed=True), password: str = Body(..., embed=True), owner_uid: Optional[str] = Body(None, embed=True)):
    """Collaborator login with email/password.
    - If owner_uid is not provided, return the list of workspaces for selection.
    - If provided, validate against pending invite (first login) or stored credentials.
    """
    email = (email or "").strip().lower()
    if not email or not password:
        return JSONResponse({"error": "Missing credentials"}, status_code=400)

    # If no owner_uid provided, return list of workspaces this email is associated with
    if not owner_uid:
        idx = read_json_key(_email_index_key(email)) or {}
        workspaces = idx.get("workspaces", [])
        if not workspaces:
            return JSONResponse({"error": "No workspaces for this email"}, status_code=404)
        return {"select_workspace": True, "workspaces": workspaces}

    # Try first-login flow via invite
    team = _read_team(owner_uid)
    inv: Optional[dict] = next((i for i in team.get("invites", []) if (i.get("email") or "").lower() == email), None)
    if inv and _verify_password(password, inv.get("password_hash") or ""):
        role = inv.get("role")
        # Persist collaborator credential for subsequent logins
        cred_key = _collab_user_key(owner_uid, email)
        write_json_key(cred_key, {
            "owner_uid": owner_uid,
            "email": email,
            "role": role,
            "password_hash": inv.get("password_hash"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        # Remove invite after first successful login
        team["invites"] = [i for i in team.get("invites", []) if i.get("token") != inv.get("token")]
        _write_team(owner_uid, team)
        # Add member to team (using synthetic uid)
        uid = f"collab:{owner_uid}:{email}"
        member = {"uid": uid, "email": email, "role": role, "joined_at": datetime.now(timezone.utc).isoformat()}
        members = [m for m in team.get("members", []) if m.get("uid") != uid and (m.get("email") or "").lower() != email]
        members.append(member)
        team["members"] = members
        _write_team(owner_uid, team)
        token = _issue_collab_jwt(owner_uid, email, role)
        return {"ok": True, "token": token, "owner_uid": owner_uid, "role": role}

    # Fallback: existing collaborator credential
    cred = read_json_key(_collab_user_key(owner_uid, email)) or {}
    if cred and _verify_password(password, cred.get("password_hash") or ""):
        role = cred.get("role") or "retoucher"
        token = _issue_collab_jwt(owner_uid, email, role)
        return {"ok": True, "token": token, "owner_uid": owner_uid, "role": role}

    return JSONResponse({"error": "Invalid credentials"}, status_code=401)