import os
from typing import Optional, Tuple
from fastapi import Request
from app.core.config import logger
from app.utils.storage import read_json_key

# Collaboration helpers
ALLOWED_ROLES = {"admin", "retoucher", "gallery_manager"}


def _owner_ptr_key(member_uid: str) -> str:
    return f"users/{member_uid}/collab/owner.json"


def get_owner_uid_for(member_uid: str) -> Optional[str]:
    try:
        ptr = read_json_key(_owner_ptr_key(member_uid)) or {}
        owner = ptr.get("owner_uid")
        if isinstance(owner, str) and owner:
            return owner
    except Exception as ex:
        logger.warning(f"get_owner_uid_for failed: {ex}")
    return None


def resolve_workspace_uid(request: Request) -> tuple[Optional[str], Optional[str]]:
    """Return (effective_uid, requester_uid). If the requester is a collaborator,
    switch to the owner's workspace; otherwise use requester's own uid."""
    req_uid = get_uid_from_request(request)
    if not req_uid:
        return None, None
    owner = get_owner_uid_for(req_uid)
    return (owner or req_uid), req_uid


def has_role_access(requester_uid: str, owner_uid: str, area: str) -> bool:
    """area: 'retouch' | 'convert' | 'gallery' | 'all'"""
    # Owner always has full access
    if requester_uid == owner_uid:
        return True
    # Load team of owner and check member role
    team = read_json_key(f"users/{owner_uid}/collab/team.json") or {}
    members = team.get("members", []) or []
    role = None
    # Prefer uid match, fallback email
    req_email = get_user_email_from_uid(requester_uid) or ""
    for m in members:
        if m.get("uid") == requester_uid or (req_email and (m.get("email") or "").lower() == req_email):
            role = (m.get("role") or "").lower()
            break
    if not role:
        return False
    if role == "admin":
        return True
    if area in ("retouch", "convert") and role == "retoucher":
        return True
    if area == "gallery" and role == "gallery_manager":
        return True
    return False

firebase_enabled = False
try:
    import firebase_admin
    from firebase_admin import auth as fb_auth, credentials as fb_credentials

    FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
    FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    FIREBASE_SERVICE_ACCOUNT_JSON_PATH = (os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_PATH", "") or "").strip().strip('"').strip("'")

    if not getattr(firebase_admin, "_apps", []):
        if FIREBASE_SERVICE_ACCOUNT_JSON:
            cred = fb_credentials.Certificate(eval(FIREBASE_SERVICE_ACCOUNT_JSON))
            firebase_admin.initialize_app(cred, {"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None)
        elif FIREBASE_SERVICE_ACCOUNT_JSON_PATH and os.path.isfile(FIREBASE_SERVICE_ACCOUNT_JSON_PATH):
            from os import environ
            if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                environ["GOOGLE_APPLICATION_CREDENTIALS"] = FIREBASE_SERVICE_ACCOUNT_JSON_PATH
            cred = fb_credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_JSON_PATH)
            firebase_admin.initialize_app(cred, {"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None)
        else:
            firebase_admin.initialize_app(options={"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None)
    firebase_enabled = True
    logger.info("Firebase Admin initialized (mod)")
except Exception as ex:
    logger.warning(f"Firebase Admin not initialized: {ex}")
    fb_auth = None  # type: ignore


def _parse_collab_uid(uid: str):
    """Return (owner_uid, email) if uid is a synthetic collaborator uid, else (None, None)."""
    try:
        if uid and uid.startswith("collab:"):
            _, owner_uid, email = uid.split(":", 2)
            return owner_uid, email
    except Exception:
        pass
    return None, None


def get_uid_from_request(request: Request) -> Optional[str]:
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None
    # Try collaborator JWT first (HS256)
    try:
        from app.core.config import COLLAB_JWT_SECRET
        import jwt  # type: ignore
        if COLLAB_JWT_SECRET:
            decoded = jwt.decode(token, COLLAB_JWT_SECRET, algorithms=["HS256"])  # raises on invalid
            if decoded.get("kind") == "collab" and isinstance(decoded.get("sub"), str):
                return decoded.get("sub")
    except Exception:
        # Not a valid collaborator token; fall through to Firebase
        pass
    # Firebase token
    if not firebase_enabled or not fb_auth:
        return None
    try:
        decoded = fb_auth.verify_id_token(token)
        return decoded.get("uid")
    except Exception as ex:
        logger.warning(f"Token verification failed: {ex}")
        return None


def get_user_email_from_uid(uid: str) -> Optional[str]:
    # Collaborator synthetic uid support
    owner_uid, email = _parse_collab_uid(uid)
    if email:
        return email.lower()
    try:
        if not firebase_enabled or not fb_auth:
            return None
        user = fb_auth.get_user(uid)
        return (getattr(user, "email", None) or "").lower()
    except Exception as ex:
        logger.warning(f"get_user_email_from_uid failed: {ex}")
        return None


def get_uid_by_email(email: str) -> Optional[str]:
    try:
        if not email:
            return None
        if not firebase_enabled or not fb_auth:
            return None
        user = fb_auth.get_user_by_email(email)
        return getattr(user, "uid", None)
    except Exception as ex:
        logger.warning(f"get_uid_by_email failed: {ex}")
        return None


def require_admin(request: Request, admin_emails: list[str]) -> Tuple[bool, str]:
    try:
        if not firebase_enabled or not fb_auth:
            return False, "auth disabled"
        uid = get_uid_from_request(request)
        if not uid:
            return False, "unauthorized"
        user = fb_auth.get_user(uid)
        email = (getattr(user, "email", None) or "").lower()
        if email and (email in admin_emails):
            return True, email
        return False, email or ""
    except Exception as ex:
        logger.warning(f"require_admin failed: {ex}")
        return False, "error"