from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from typing import Optional, Dict, Any, List
import uuid
import time
from string import Template

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.auth import get_fs_client as _get_fs_client
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
    # template removed

    form.update({
        "background_color": str(bg),
        "form_card_bg": str(form_card_bg),
        "label_color": str(label_color),
        "button_bg": str(button_bg),
        "button_text": str(button_text),
        "hide_payment_option": bool(hide_payment_option),
        "allow_in_studio": bool(allow_in_studio),
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
    html = _render_public_form_html(
        form_id,
        bg,
        default_date,
        form_card_bg=form_card_bg,
        label_color=label_color,
        button_bg=button_bg,
        button_text=button_text,
        hide_payment_option=hide_payment_option,
        allow_in_studio=allow_in_studio,
    )
    return HTMLResponse(html)


def _render_public_form_html(
    form_id: str,
    bg: str,
    default_date: str = "",
    *,
    form_card_bg: str = "rgba(255,255,255,.04)",
    label_color: str = "#cbd5e1",
    button_bg: str = "#7fe0d6",
    button_text: str = "#001014",
    hide_payment_option: bool = False,
    allow_in_studio: bool = False,
) -> str:
    # Use Template to avoid f-string brace issues with CSS/JS
    css_tpl = Template(
        """
    :root{--pm-accent:#8ab4f8}
    *{box-sizing:border-box}
    body{margin:0;background:${bg};color:#fafafa;font-family:'Outfit', -apple-system, system-ui, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica Neue, Arial}
    .container{max-width:720px;margin:0 auto;padding:20px}
    h1{font-size:clamp(22px,4vw,30px);font-weight:800;letter-spacing:-.02em;margin:0 0 12px}
    .card{border:1px solid rgba(255,255,255,.12);border-radius:16px;background:${form_card_bg};padding:16px}
    label{display:block;font-size:12px;color:${label_color};margin:10px 0 6px}
    input,textarea,select{width:100%;background:rgba(0,0,0,.3);color:#fff;border:1px solid rgba(255,255,255,.18);border-radius:10px;padding:10px}
    button{display:inline-flex;align-items:center;gap:8px;background:${button_bg};color:${button_text};font-weight:700;padding:10px 14px;border-radius:10px;border:0;text-decoration:none;margin-top:14px}
    .row{display:grid;gap:10px;grid-template-columns:1fr}
    @media(min-width:640px){.row{grid-template-columns:1fr 1fr}}
    .note{font-size:12px;opacity:.75;margin-top:8px}
    .ok{background:#10b981;color:#001}
    .err{background:#ef4444;color:#fff}
        """.strip()
    )
    # Base CSS for the split layout
    css = css_tpl.substitute(
        bg=bg,
        form_card_bg=form_card_bg,
        label_color=label_color,
        button_bg=button_bg,
        button_text=button_text,
    ) + "\n" + Template(
        """
        .container{max-width:980px;display:grid;grid-template-columns:1.1fr 1fr;gap:28px;align-items:start}
        @media(max-width:980px){.container{display:block}}
        .hero{background:linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02));border:1px solid rgba(255,255,255,.12);border-radius:24px;padding:24px;box-shadow:0 10px 40px rgba(0,0,0,.35)}
        .hero .pill{display:inline-block;border:1px solid rgba(255,255,255,.22);border-radius:999px;padding:6px 12px;font-size:12px;opacity:.9;margin-bottom:10px}
        .hero h1{font-size:clamp(28px,5vw,44px);font-weight:800;letter-spacing:-.02em;margin:0 0 10px}
        .hero .sub{opacity:.85;max-width:46ch;line-height:1.5}
        .hero .cta{display:inline-flex;margin-top:18px;background:${button_bg};color:${button_text};font-weight:700;padding:10px 16px;border-radius:12px;text-decoration:none}
        .form-card{border:1px solid rgba(255,255,255,.12);border-radius:20px;background:${form_card_bg};padding:22px;box-shadow:0 10px 40px rgba(0,0,0,.35)}
        .form-title{font-size:16px;font-weight:600;margin-bottom:16px}
        .grid-2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
        @media(max-width:640px){.grid-2{grid-template-columns:1fr}}
        .field{margin:10px 0}
        .field label{font-size:12px;color:${label_color};display:block;margin-bottom:6px}
        .field input,.field textarea,.field select{width:100%;background:transparent;color:#fff;border:0;border-bottom:1px solid rgba(255,255,255,.14);border-radius:0;padding:10px 0}
        .field textarea{resize:vertical}
        .actions{margin-top:16px}
        .actions button{background:${button_bg};color:${button_text};font-weight:700;padding:10px 14px;border-radius:10px;border:0}
        """
    ).substitute(
        form_card_bg=form_card_bg,
        label_color=label_color,
        button_bg=button_bg,
        button_text=button_text,
    )

    # Prepare conditional payment option HTML
    payment_html = "" if hide_payment_option else (
        """
              <div>
                <label>Payment option</label>
                <select name='payment_option'>
                  <option value='online'>Online</option>
                  <option value='offline'>Offline</option>
                </select>
              </div>
        """
    )

    # Prepare optional studio toggle
    studio_html = (
        '<label style="display:flex;align-items:center;gap:8px;margin-top:8px">'
        '<input type="checkbox" id="pm_studio" /> In studio'
        '</label>'
    ) if allow_in_studio else ''

    # Single split layout content
    content_html = Template(
        """
    <div class='container'>
      <section class='hero'>
        <div class='pill'>Book</div>
        <h1>Book Your Photography Session!</h1>
        <p class='sub'>Have a special moment to capture? We'd love to hear from you. Reach out anytime and let's create something beautiful together.</p>
        <a href='#form' class='cta'>Book Now</a>
      </section>
      <section class='form-card' id='form'>
        <div class='form-title'>Book Your Session</div>
        <form method='POST' action='/api/booking/submit' enctype='application/x-www-form-urlencoded' onsubmit='return onSubmit(event)'>
          <input type='hidden' name='form_id' value='${form_id}' />
          <input type='hidden' name='client_name' id='pm_cn' />
          <input type='hidden' name='latitude' id='pm_lat' />
          <input type='hidden' name='longitude' id='pm_lon' />
          <div class='grid-2'>
            <div class='field'>
              <label>First Name</label>
              <input id='pm_fn' placeholder='John' />
            </div>
            <div class='field'>
              <label>Last Name</label>
              <input id='pm_ln' placeholder='Doe' />
            </div>
          </div>
          <div class='grid-2'>
            <div class='field'>
              <label>Phone</label>
              <input name='phone' placeholder='+1 777 888 999' required />
            </div>
            <div class='field'>
              <label>Email Address</label>
              <input name='email' type='email' placeholder='you@example.com' required />
            </div>
          </div>
          <div class='field'>
            <label>Message</label>
            <textarea name='service_details' rows='4' placeholder='What service are you looking for?'></textarea>
          </div>
          <div class='grid-2'>
            <div class='field'>
              <label>Preferred date</label>
              <input name='date' type='date' required value='${default_date}' />
            </div>
            ${payment_html}
          </div>
          ${studio_html}
          <div class='actions'>
            <button type='submit'>Submit</button>
            <div id='msg' class='note' style='display:inline-block;margin-left:10px'></div>
          </div>
        </form>
      </section>
    </div>
        """
    ).substitute(form_id=form_id, default_date=default_date, payment_html=payment_html, studio_html=studio_html)

    html_tpl = Template(
        """<!doctype html>
<html>
  <head>
    <meta charset='utf-8'/>
    <meta name='viewport' content='width=device-width,initial-scale=1'/>
    <title>Booking</title>
    <link rel="preconnect" href="https://fonts.googleapis.com"/>
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet"/>
    <style>${css}</style>
  </head>
  <body>
    ${content}
    <script>
      // Geolocation: best-effort, fills hidden lat/lon and a simple location string
      (function(){
        try {
          if (!navigator.geolocation) return;
          navigator.geolocation.getCurrentPosition(function(pos){
            try {
              var lat = (pos && pos.coords && pos.coords.latitude) ? pos.coords.latitude.toFixed(6) : '';
              var lon = (pos && pos.coords && pos.coords.longitude) ? pos.coords.longitude.toFixed(6) : '';
              var latEl = document.getElementById('pm_lat');
              var lonEl = document.getElementById('pm_lon');
              var locEl = document.getElementById('pm_location');
              if (latEl) latEl.value = lat;
              if (lonEl) lonEl.value = lon;
              if (locEl && (!locEl.value || locEl.value.trim()==='')) locEl.value = (lat && lon) ? (lat + ',' + lon) : '';
            } catch(e) {}
          });
        } catch(e) {}
      })();
      (function(){
        try {
          var studio = document.getElementById('pm_studio');
          var locEl = document.getElementById('pm_location');
          var latEl = document.getElementById('pm_lat');
          var lonEl = document.getElementById('pm_lon');
          if (!studio) return;
          var apply = function(){
            if (!studio || !locEl) return;
            if (studio.checked){
              if (locEl) locEl.value = 'In studio';
              if (latEl) latEl.value = '';
              if (lonEl) lonEl.value = '';
              if (locEl) locEl.setAttribute('readonly', 'readonly');
            } else {
              if (locEl && locEl.value === 'In studio') locEl.value = '';
              if (locEl) locEl.removeAttribute('readonly');
            }
          };
          studio.addEventListener('change', apply);
          apply();
        } catch(e) {}
      })();
      async function onSubmit(e){
        e.preventDefault();
        const form = e.target;
        const msg = document.getElementById('msg');
        msg.textContent = 'Submitting...';
        msg.className = 'note';
        try{
          // Compose client_name if split fields are present
          try{
            const fn = document.getElementById('pm_fn');
            const ln = document.getElementById('pm_ln');
            const cn = document.getElementById('pm_cn');
            if (cn) {
              const v = [fn && fn.value || '', ln && ln.value || ''].filter(Boolean).join(' ').trim();
              if (v) cn.value = v;
            }
          } catch(_e){}
          const fd = new FormData(form);
          const res = await fetch(form.action, { method: 'POST', body: fd, credentials: 'include' });
          const data = await res.json().catch(()=>({}));
          if(!res.ok || data.error) throw new Error(data.error || 'Error');
          msg.textContent = 'Thanks! We have received your request.';
          msg.className = 'note ok';
          form.reset();
        }catch(err){
          msg.textContent = err.message || 'Could not submit';
          msg.className = 'note err';
        }
        return false;
      }
    </script>
  </body>
</html>"""
    )

    html = html_tpl.substitute(
        css=css,
        content=content_html,
    )
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

    html = _render_public_form_html(
        form_id="preview",
        bg=bg,
        default_date=date,
        form_card_bg=form_card_bg,
        label_color=label_color,
        button_bg=button_bg,
        button_text=button_text,
        hide_payment_option=hide_payment_option,
        allow_in_studio=allow_in_studio,
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
