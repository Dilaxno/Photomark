from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from typing import Optional, Dict, Any, List
import uuid
import time
import os
from string import Template

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.auth import get_fs_client as _get_fs_client
from app.core.auth import get_user_email_from_uid
from app.utils.emailing import render_email, send_email_smtp
from app.core.config import logger
from app.utils.storage import read_json_key, write_json_key

router = APIRouter(prefix="/api/booking", tags=["booking"])  # dashboard + form settings
# Single booking layout (Split). Templates removed.


# --------- Helpers ---------

def _user_form_key(uid: str) -> str:
    return f"users/{uid}/booking/form.json"


def _form_registry_key(form_id: str) -> str:
    return f"booking_forms/{form_id}.json"


def _user_bookings_index_key(uid: str) -> str:
    return f"users/{uid}/booking/index.json"


def _user_booking_record_key(uid: str, booking_id: str) -> str:
    return f"users/{uid}/booking/records/{booking_id}.json"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]

# Firestore client types (optional)
try:
    from firebase_admin import firestore as fb_fs  # type: ignore
except Exception:
    fb_fs = None  # type: ignore


# --------- Form settings (per-user) ---------

@router.get("/form")
async def get_form(request: Request):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return {"error": "Unauthorized"}
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return {"error": "Forbidden"}

    # Load or create default form
    form = read_json_key(_user_form_key(eff_uid)) or {}
    if not form.get("form_id"):
        form_id = _new_id()
        form = {
            "form_id": form_id,
            "background_color": form.get("background_color") or "#0a0d0f",
            # Optional customizations with sensible defaults
            "form_card_bg": form.get("form_card_bg") or "rgba(255,255,255,.04)",
            "label_color": form.get("label_color") or "#cbd5e1",
            "button_bg": form.get("button_bg") or "#7fe0d6",
            "button_text": form.get("button_text") or "#001014",
            "hide_payment_option": bool(form.get("hide_payment_option") or False),
            "allow_in_studio": bool(form.get("allow_in_studio") or False),
            "title": form.get("title") or "Book a Photoshoot",
            "subtitle": form.get("subtitle") or "Fill this form here",
            "input_radius": int(form.get("input_radius") or 10),
            "updated_at": int(time.time()),
        }
        write_json_key(_user_form_key(eff_uid), form)
        write_json_key(_form_registry_key(form_id), {"user_uid": eff_uid})
    return form


@router.post("/form")
async def update_form(request: Request, payload: Dict[str, Any]):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return {"error": "Unauthorized"}
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return {"error": "Forbidden"}

    form = read_json_key(_user_form_key(eff_uid)) or {}
    if not form.get("form_id"):
        # create if missing
        form_id = _new_id()
        write_json_key(_form_registry_key(form_id), {"user_uid": eff_uid})
        form["form_id"] = form_id

    bg = payload.get("background_color") or form.get("background_color") or "#0b0b0c"
    form_card_bg = payload.get("form_card_bg") or form.get("form_card_bg") or "rgba(255,255,255,.06)"
    label_color = payload.get("label_color") or form.get("label_color") or "#fafafa"
    button_bg = payload.get("button_bg") or form.get("button_bg") or "#8ab4f8"
    button_text = payload.get("button_text") or form.get("button_text") or "#000000"
    hide_payment_option = bool(payload.get("hide_payment_option") if payload.get("hide_payment_option") is not None else form.get("hide_payment_option") or False)
    allow_in_studio = bool(payload.get("allow_in_studio") if payload.get("allow_in_studio") is not None else form.get("allow_in_studio") or False)
    title = payload.get("title") or form.get("title") or "Book a Photoshoot"
    subtitle = payload.get("subtitle") or form.get("subtitle") or "Fill this form here"
    input_radius = int(payload.get("input_radius") if payload.get("input_radius") is not None else form.get("input_radius") or 10)
    # template removed

    form.update({
        "background_color": str(bg),
        "form_card_bg": str(form_card_bg),
        "label_color": str(label_color),
        "button_bg": str(button_bg),
        "button_text": str(button_text),
        "hide_payment_option": bool(hide_payment_option),
        "allow_in_studio": bool(allow_in_studio),
        "title": str(title),
        "subtitle": str(subtitle),
        "input_radius": int(max(0, min(32, input_radius))),
        "updated_at": int(time.time()),
    })
    write_json_key(_user_form_key(eff_uid), form)
    return form


# --------- Public embed page (for iframe) ---------

@router.get("/public/{form_id}")
async def public_booking_form(form_id: str, request: Request):
    # Resolve owner uid
    reg = read_json_key(_form_registry_key(form_id)) or {}
    uid = reg.get("user_uid")
    if not uid:
        return HTMLResponse("<h1>Form not found</h1>", status_code=404)
    form = read_json_key(_user_form_key(uid)) or {}
    bg = form.get("background_color") or "#0b0b0c"
    form_card_bg = form.get("form_card_bg") or "rgba(255,255,255,.06)"
    label_color = form.get("label_color") or "#fafafa"
    button_bg = form.get("button_bg") or "#8ab4f8"
    button_text = form.get("button_text") or "#000000"
    hide_payment_option = bool(form.get("hide_payment_option") or False)
    allow_in_studio = bool(form.get("allow_in_studio") or False)
    # templates removed; always render split layout
    # Default date prefill through query param ?date=YYYY-MM-DD
    try:
        default_date = str(request.query_params.get("date") or "").strip()
    except Exception:
        default_date = ""
    try:
        input_radius = int(request.query_params.get("input_radius") or form.get("input_radius") or 10)
    except Exception:
        input_radius = 10
    # Flags
    def _qp_bool(key: str) -> bool:
        try:
            v = request.query_params.get(key)
            return bool(str(v).lower() in ("1","true","yes","on")) if v is not None else False
        except Exception:
            return False

    full_form = _qp_bool("full_form")
    no_cta = _qp_bool("no_cta")

    # Resolve title/subtitle (query overrides saved form)
    try:
        title_text = str(request.query_params.get("title") or form.get("title") or "Book a Photoshoot")
    except Exception:
        title_text = "Book a Photoshoot"
    try:
        subtitle_text = str(request.query_params.get("subtitle") or form.get("subtitle") or "Fill this form here")
    except Exception:
        subtitle_text = "Fill this form here"

    html = _render_modern_form_html(
        form_id=form_id,
        default_date=default_date,
        title_text=title_text,
        subtitle_text=subtitle_text,
        accent=label_color,
        bg=bg,
        card_bg=form_card_bg,
        accent_button=button_bg,
        accent_button_text=button_text,
        allow_in_studio=allow_in_studio,
        hide_payment_option=hide_payment_option,
        input_radius=input_radius,
    )
    return HTMLResponse(html)


def _render_modern_form_html(
    form_id: str,
    default_date: str = "",
    *,
    title_text: str = "Book a Photoshoot",
    subtitle_text: str = "Fill this form here",
    accent: str = "#111827",       # dark gray text
    bg: str = "#ffffff",           # light/white background
    card_bg: str = "#f9fafb",      # light gray card
    accent_button: str = "#111827",
    accent_button_text: str = "#ffffff",
    allow_in_studio: bool = False,
    hide_payment_option: bool = False,
    input_radius: int = 10,
) -> str:
    css = f"""
    * {{ box-sizing: border-box; }}
    body {{
        margin: 0;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
        background: {bg};
        color: {accent};
        line-height: 1.6;
        padding: 40px 20px;
    }}
    .container {{
        max-width: 600px;
        margin: 0 auto;
    }}
    h1 {{
        font-size: 28px;
        font-weight: 600;
        margin-bottom: 24px;
        text-align: center;
    }}
    .form-card {{
        background: {card_bg};
        border-radius: 16px;
        padding: 32px;
        box-shadow: 0 6px 18px rgba(0,0,0,0.08);
    }}
    .field {{
        margin-bottom: 20px;
    }}
    .field label {{
        font-size: 14px;
        font-weight: 500;
        margin-bottom: 6px;
        display: block;
    }}
    .field input, .field textarea, .field select {{
        width: 100%;
        padding: 12px 14px;
        border: 1px solid #d1d5db;
        border-radius: {int(max(0, min(32, input_radius)))}px;
        background: #fff;
        font-size: 15px;
        transition: border 0.2s;
    }}
    .field input:focus, .field textarea:focus, .field select:focus {{
        outline: none;
        border-color: {accent};
    }}
    button {{
        width: 100%;
        background: {accent_button};
        color: {accent_button_text};
        font-weight: 600;
        border: none;
        padding: 14px;
        border-radius: 10px;
        font-size: 16px;
        cursor: pointer;
        transition: background 0.2s;
    }}
    button:hover {{
        background: #000;
    }}
    .note {{
        text-align: center;
        margin-top: 16px;
        font-size: 14px;
        opacity: 0.8;
    }}
    """

    payment_html = "" if hide_payment_option else """
        <div class='field'>
            <label>Payment Option</label>
            <select name='payment_option'>
                <option value='online'>Online</option>
                <option value='offline'>Offline</option>
            </select>
        </div>
    """

    studio_html = (
        "<div class='field'><label><input type='checkbox' name='studio'/> In studio</label></div>"
        if allow_in_studio else ""
    )

    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset='utf-8'/>
        <meta name='viewport' content='width=device-width,initial-scale=1'/>
        <title>{title_text}</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet"/>
        <style>{css}</style>
      </head>
      <body>
        <div class="container">
          <h1>{title_text}</h1>
          <div class='note'>{subtitle_text}</div>
          <div class="form-card">
            <form method='POST' action='/api/booking/submit' onsubmit='return onSubmit(event)'>
              <input type='hidden' name='form_id' value='{form_id}'/>
              <div class='field'>
                <label>Name</label>
                <input name='client_name' placeholder='Your full name' required/>
              </div>
              <div class='field'>
                <label>Email</label>
                <input name='email' type='email' placeholder='you@example.com' required/>
              </div>
              <div class='field'>
                <label>Phone</label>
                <input name='phone' placeholder='+1 777 888 999' required/>
              </div>
              <div class='field'>
                <label>Preferred Date</label>
                <input type='date' name='date' value='{default_date}' required/>
              </div>
              <div class='field'>
                <label>Message</label>
                <textarea name='service_details' rows='4' placeholder='What kind of session are you interested in?'></textarea>
              </div>
              {payment_html}
              {studio_html}
              <button type='submit'>Request Booking</button>
              <div id='msg' class='note'></div>
            </form>
          </div>
        </div>
      </body>
    </html>
    """
    return html



@router.get("/preview")
async def preview_booking(request: Request):
    # Read appearance from query params to render instant preview without persistence
    qp = request.query_params
    def _pick(*keys: str, default: str = "") -> str:
        for k in keys:
            v = qp.get(k)
            if v is not None:
                return str(v)
        return default

    # templates removed

    bg = _pick("background_color", "bg", default="#0a0d0f")
    form_card_bg = _pick("form_card_bg", "card_bg", default="rgba(255,255,255,.04)")
    label_color = _pick("label_color", "label", default="#cbd5e1")
    button_bg = _pick("button_bg", "btn_bg", default="#7fe0d6")
    button_text = _pick("button_text", "btn_text", default="#001014")
    date = _pick("date", default="")

    def _bool(key: str) -> bool:
        v = qp.get(key)
        if v is None:
            return False
        return str(v).lower() in ("1", "true", "yes", "on")

    hide_payment_option = _bool("hide_payment_option")
    allow_in_studio = _bool("allow_in_studio")
    full_form = _bool("full_form")
    no_cta = _bool("no_cta")

    title_text = _pick("title", default="Book a Photoshoot")
    subtitle_text = _pick("subtitle", default="Fill this form here")
    try:
        input_radius = int(_pick("input_radius", default="10"))
    except Exception:
        input_radius = 10

    html = _render_modern_form_html(
        form_id="preview",
        default_date=date,
        title_text=title_text,
        subtitle_text=subtitle_text,
        accent=label_color,
        bg=bg,
        card_bg=form_card_bg,
        accent_button=button_bg,
        accent_button_text=button_text,
        allow_in_studio=allow_in_studio,
        hide_payment_option=hide_payment_option,
        input_radius=input_radius,
    )
    return HTMLResponse(html)

# templates endpoint removed


# --------- Public submit (no auth) ---------

@router.post("/submit")
async def submit_booking(
    form_id: str = Form(...),
    client_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    service_details: str = Form(""),
    date: str = Form(...),
    payment_option: str = Form("online"),
    location: str = Form(""),
    latitude: Optional[str] = Form(None),
    longitude: Optional[str] = Form(None),
):
    try:
        # resolve form -> user
        reg = read_json_key(_form_registry_key(form_id)) or {}
        uid = reg.get("user_uid")
        if not uid:
            return {"error": "invalid_form"}

        booking_id = _new_id()
        now = int(time.time())
        record: Dict[str, Any] = {
            "id": booking_id,
            "user_uid": uid,
            "form_id": form_id,
            "client_name": client_name.strip(),
            "email": email.strip(),
            "phone": phone.strip(),
            "service_details": service_details or "",
            "date": date,
            "payment_option": payment_option if payment_option in ("online","offline") else "online",
            "status": "new",
            "created_at": now,
            "updated_at": now,
        }
        if location:
            record["location"] = location.strip()
        if latitude:
            record["latitude"] = latitude
        if longitude:
            record["longitude"] = longitude
        write_json_key(_user_booking_record_key(uid, booking_id), record)

        # Firestore persistence (best-effort)
        try:
            db = _get_fs_client()
            if db is not None:
                db.collection('users').document(uid).collection('bookings').document(booking_id).set(record, merge=True)
        except Exception as ex:
            logger.warning(f"booking submit: firestore write failed for {uid}/{booking_id}: {ex}")

        idx = read_json_key(_user_bookings_index_key(uid)) or {"items": []}
        items = idx.get("items") or []
        # store a lightweight copy for listing
        lite_keys = ["id","client_name","email","phone","date","payment_option","status","created_at","updated_at"]
        try:
            if record.get("location"):
                lite_keys.append("location")
        except Exception:
            pass
        lite = {k: record[k] for k in lite_keys if k in record}
        items.insert(0, lite)
        idx["items"] = items[:1000]
        write_json_key(_user_bookings_index_key(uid), idx)
        # Notify photographer/owner by email (best-effort)
        try:
            owner_email = (get_user_email_from_uid(uid) or '').strip()
            if not owner_email:
                try:
                    db = _get_fs_client()
                    if db is not None:
                        snap = db.collection('users').document(uid).get()
                        if getattr(snap, 'exists', False):
                            data = snap.to_dict() or {}
                            owner_email = str(data.get('email') or '').strip()
                except Exception:
                    pass
            if owner_email:
                subject = f"New booking request from {client_name or email}"
                front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
                dash_url = f"{front}/#booking"
                # Simple HTML escaping
                def esc(s: str) -> str:
                    try:
                        return str(s).replace('<', '&lt;').replace('>', '&gt;')
                    except Exception:
                        return str(s)
                intro = (
                    "A new booking request was submitted via your form.<br><br>"
                    f"<strong>Name:</strong> {esc(client_name)}<br>"
                    f"<strong>Email:</strong> <a href='mailto:{email}'>{esc(email)}</a><br>"
                    f"<strong>Phone:</strong> {esc(phone)}<br>"
                    f"<strong>Date:</strong> {esc(date)}<br>"
                    f"<strong>Payment:</strong> {esc(record.get('payment_option'))}<br>"
                )
                if record.get('location'):
                    intro += f"<strong>Location:</strong> {esc(record.get('location'))}<br>"
                if service_details:
                    intro += f"<strong>Message:</strong><br>{esc(service_details)}"
                html = render_email(
                    "email_basic.html",
                    title="New booking request",
                    intro=intro,
                    button_label="Open Booking",
                    button_url=dash_url,
                    footer_note=f"Request ID: {booking_id}",
                )
                text = (
                    "New booking request\n"
                    f"Name: {client_name}\n"
                    f"Email: {email}\n"
                    f"Phone: {phone}\n"
                    f"Date: {date}\n"
                    f"Payment: {record.get('payment_option')}\n"
                    + (f"Location: {record.get('location')}\n" if record.get('location') else "")
                    + (f"Message: {service_details}\n" if service_details else "")
                )
                try:
                    send_email_smtp(owner_email, subject, html, text, reply_to=email)
                except Exception:
                    pass
        except Exception:
            pass
        return {"ok": True, "id": booking_id}
    except Exception as ex:
        logger.exception(f"booking submit failed: {ex}")
        return {"error": "submit_failed"}


# --------- Dashboard APIs (auth) ---------

@router.get("/list")
async def list_bookings(request: Request, status: Optional[str] = None):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return {"error": "Unauthorized"}
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return {"error": "Forbidden"}

    # Prefer Firestore if available
    try:
        db = _get_fs_client()
        if db is not None:
            q = db.collection('users').document(eff_uid).collection('bookings')
            docs = [d.to_dict() or {} for d in q.stream()]  # type: ignore[attr-defined]
            items = []
            for rec in docs:
                lite: Dict[str, Any] = {
                    "id": rec.get("id"),
                    "client_name": rec.get("client_name"),
                    "email": rec.get("email"),
                    "phone": rec.get("phone"),
                    "date": rec.get("date"),
                    "payment_option": rec.get("payment_option"),
                    "status": rec.get("status"),
                    "created_at": rec.get("created_at"),
                    "updated_at": rec.get("updated_at"),
                }
                # include location if present
                if rec.get("location"):
                    lite["location"] = rec.get("location")
                items.append(lite)
            # filter and sort by created_at desc
            if status and status in ("new","pending","confirmed","cancelled"):
                items = [it for it in items if it.get("status") == status]
            try:
                items.sort(key=lambda x: int(x.get("created_at") or 0), reverse=True)
            except Exception:
                pass
            return {"items": items}
    except Exception as ex:
        logger.warning(f"booking list: firestore read failed for {eff_uid}: {ex}")

    # Fallback to JSON index
    idx = read_json_key(_user_bookings_index_key(eff_uid)) or {"items": []}
    items = idx.get("items") or []
    if status and status in ("new","pending","confirmed","cancelled"):
        items = [it for it in items if it.get("status") == status]
    return {"items": items}


@router.get("/{booking_id}")
async def get_booking(request: Request, booking_id: str):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return {"error": "Unauthorized"}
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return {"error": "Forbidden"}

    # Try Firestore first
    try:
        db = _get_fs_client()
        if db is not None:
            snap = db.collection('users').document(eff_uid).collection('bookings').document(booking_id).get()
            if getattr(snap, 'exists', False):
                data = snap.to_dict() or {}
                if data:
                    return data
    except Exception as ex:
        logger.warning(f"booking get: firestore read failed for {eff_uid}/{booking_id}: {ex}")

    rec = read_json_key(_user_booking_record_key(eff_uid, booking_id))
    if not rec:
        return {"error": "not_found"}
    return rec


@router.post("/{booking_id}/status")
async def update_status(request: Request, booking_id: str, payload: Dict[str, Any]):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return {"error": "Unauthorized"}
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return {"error": "Forbidden"}

    rec_key = _user_booking_record_key(eff_uid, booking_id)
    rec = read_json_key(rec_key)
    if not rec:
        return {"error": "not_found"}

    new_status = str(payload.get("status") or "").lower()
    if new_status not in ("new","pending","confirmed","cancelled"):
        return {"error": "bad_status"}

    now = int(time.time())
    rec["status"] = new_status
    rec["updated_at"] = now
    write_json_key(rec_key, rec)

    # Firestore update (best-effort)
    try:
        db = _get_fs_client()
        if db is not None:
            db.collection('users').document(eff_uid).collection('bookings').document(booking_id).set({
                "status": new_status,
                "updated_at": now,
            }, merge=True)
    except Exception as ex:
        logger.warning(f"booking status: firestore update failed for {eff_uid}/{booking_id}: {ex}")

    # update index
    idx = read_json_key(_user_bookings_index_key(eff_uid)) or {"items": []}
    items: List[Dict[str, Any]] = idx.get("items") or []
    for it in items:
        if it.get("id") == booking_id:
            it["status"] = new_status
            it["updated_at"] = now
            break
    idx["items"] = items
    write_json_key(_user_bookings_index_key(eff_uid), idx)

    # Notify client by email for important status changes
    try:
        if new_status in ("confirmed", "cancelled"):
            client_email = (rec.get("email") or "").strip()
            client_name = (rec.get("client_name") or "").strip()
            # Resolve account (owner) name for branding in the message
            account_name = "your photographer"
            owner_email = ""
            try:
                db = _get_fs_client()
                if db is not None:
                    snap = db.collection('users').document(eff_uid).get()
                    if getattr(snap, 'exists', False):
                        data = snap.to_dict() or {}
                        account_name = str(data.get('name') or account_name)
                        owner_email = str(data.get('email') or owner_email)
            except Exception:
                pass

            if client_email:
                if new_status == "confirmed":
                    subject = "Your booking has been confirmed"
                    intro = (
                        f"Hi {client_name or 'there'},<br><br>"
                        f"Good news! Your booking with <b>{account_name}</b> has been <b>confirmed</b>.<br>"
                        f"If you have any questions, you can reach out directly at <a href='mailto:{owner_email or 'support@photomark.app'}'>{owner_email or 'support@photomark.app'}</a>."
                    )
                else:
                    subject = "Your booking has been cancelled"
                    intro = (
                        f"Hi {client_name or 'there'},<br><br>"
                        f"We’re sorry to let you know your booking with <b>{account_name}</b> was <b>cancelled</b>.<br>"
                        f"If this was a mistake or you’d like to reschedule, contact us at <a href='mailto:{owner_email or 'support@photomark.app'}'>{owner_email or 'support@photomark.app'}</a>."
                    )

                html = render_email(
                    "email_basic.html",
                    title=subject,
                    intro=intro,
                    footer_note=f"Status updated at: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(now))} UTC"
                )
                # Best-effort: do not block API if email fails
                try:
                    send_email_smtp(client_email, subject, html, reply_to=(owner_email or None), from_name=account_name)
                except Exception:
                    pass
    except Exception:
        # Never fail the API due to email issues
        pass

    return {"ok": True, "status": new_status}
