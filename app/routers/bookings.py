from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from typing import Optional, Dict, Any, List
import uuid
import time

from app.core.auth import resolve_workspace_uid, has_role_access
from app.core.auth import get_fs_client as _get_fs_client
from app.utils.emailing import render_email, send_email_smtp
from app.core.config import logger
from app.utils.storage import read_json_key, write_json_key

router = APIRouter(prefix="/api/booking", tags=["booking"])  # dashboard + form settings


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
            "background_color": form.get("background_color") or "#0b0b0c",
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
    form.update({
        "background_color": str(bg),
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
    # Default date prefill through query param ?date=YYYY-MM-DD
    try:
        default_date = str(request.query_params.get("date") or "").strip()
    except Exception:
        default_date = ""
    html = _render_public_form_html(form_id, bg, default_date)
    return HTMLResponse(html)


def _render_public_form_html(form_id: str, bg: str, default_date: str = "") -> str:
    css = f"""
    :root{{--pm-accent:#8ab4f8}}
    *{{box-sizing:border-box}}
    body{{margin:0;background:{bg};color:#fafafa;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,Helvetica Neue,Arial}}
    .container{{max-width:720px;margin:0 auto;padding:20px}}
    h1{{font-size:clamp(22px,4vw,30px);font-weight:800;letter-spacing:-.02em;margin:0 0 12px}}
    .card{{border:1px solid rgba(255,255,255,.12);border-radius:16px;background:rgba(255,255,255,.06);padding:16px}}
    label{{display:block;font-size:12px;opacity:.9;margin:10px 0 6px}}
    input,textarea,select{{width:100%;background:rgba(0,0,0,.3);color:#fff;border:1px solid rgba(255,255,255,.18);border-radius:10px;padding:10px}}
    button{{display:inline-flex;align-items:center;gap:8px;background:var(--pm-accent);color:#000;font-weight:700;padding:10px 14px;border-radius:10px;border:0;text-decoration:none;margin-top:14px}}
    .row{{display:grid;gap:10px;grid-template-columns:1fr}}
    @media(min-width:640px){{.row{{grid-template-columns:1fr 1fr}}}}
    .note{{font-size:12px;opacity:.75;margin-top:8px}}
    .ok{{background:#10b981;color:#001}}
    .err{{background:#ef4444;color:#fff}}
    """
    # Note: The form posts to the same origin API endpoint
    return f"""<!doctype html><html><head><meta charset='utf-8'/><meta name='viewport' content='width=device-width,initial-scale=1'/><title>Booking</title><style>{css}</style></head>
    <body>
      <div class='container'>
        <h1>Booking Request</h1>
        <div class='card'>
          <form method='POST' action='/api/booking/submit' enctype='application/x-www-form-urlencoded' onsubmit='return onSubmit(event)'>
            <input type='hidden' name='form_id' value='{form_id}' />
            <label>Full name</label>
            <input name='client_name' required placeholder='Your name' />
            <div class='row'>
              <div>
                <label>Email</label>
                <input name='email' type='email' required placeholder='you@example.com' />
              </div>
              <div>
                <label>Phone</label>
                <input name='phone' required placeholder='+1 777 888 999' />
              </div>
            </div>
            <label>Location</label>
            <input name='location' id='pm_location' placeholder='City, Country (auto)' />
            <input type='hidden' name='latitude' id='pm_lat' />
            <input type='hidden' name='longitude' id='pm_lon' />
            <label>Service details</label>
            <textarea name='service_details' rows='4' placeholder='What service are you looking for?'></textarea>
            <div class='row'>
              <div>
                <label>Preferred date</label>
                <input name='date' type='date' required value='{default_date}' />
              </div>
              <div>
                <label>Payment option</label>
                <select name='payment_option'>
                  <option value='online'>Online</option>
                  <option value='offline'>Offline</option>
                </select>
              </div>
            </div>
            <button type='submit'>Submit</button>
            <div id='msg' class='note'></div>
          </form>
        </div>
      </div>
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
        async function onSubmit(e){
          e.preventDefault();
          const form = e.target;
          const msg = document.getElementById('msg');
          msg.textContent = 'Submitting...';
          msg.className = 'note';
          try{
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
    </body></html>"""


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
        record = {
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
                    if snap.exists:
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
