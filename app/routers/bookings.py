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
            "title_align": form.get("title_align") or "center",
            "subtitle_align": form.get("subtitle_align") or "center",
            "title_size": int(form.get("title_size") or 28),
            "subtitle_size": int(form.get("subtitle_size") or 14),
            "title_font": form.get("title_font") or "Inter",
            "subtitle_font": form.get("subtitle_font") or "Inter",
            "input_radius": int(form.get("input_radius") or 10),
            "submit_label": form.get("submit_label") or "Request Booking",
            "title_font_data": form.get("title_font_data") or "",
            "subtitle_font_data": form.get("subtitle_font_data") or "",
            "label_font": form.get("label_font") or "Inter",
            "label_size": int(form.get("label_size") or 14),
            "label_font_data": form.get("label_font_data") or "",
            "studio_address": form.get("studio_address") or "",
            "studio_lat": form.get("studio_lat") or "",
            "studio_lng": form.get("studio_lng") or "",
            "maps_api_key": form.get("maps_api_key") or "",
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
    title_align = str(payload.get("title_align") or form.get("title_align") or "center").lower()
    if title_align not in ("left","center","right"): title_align = "center"
    subtitle_align = str(payload.get("subtitle_align") or form.get("subtitle_align") or "center").lower()
    if subtitle_align not in ("left","center","right"): subtitle_align = "center"
    try:
        title_size = int(payload.get("title_size") if payload.get("title_size") is not None else form.get("title_size") or 28)
    except Exception:
        title_size = 28
    try:
        subtitle_size = int(payload.get("subtitle_size") if payload.get("subtitle_size") is not None else form.get("subtitle_size") or 14)
    except Exception:
        subtitle_size = 14
    title_font = str(payload.get("title_font") or form.get("title_font") or "Inter")
    subtitle_font = str(payload.get("subtitle_font") or form.get("subtitle_font") or "Inter")
    label_font = str(payload.get("label_font") or form.get("label_font") or "Inter")
    try:
        label_size = int(payload.get("label_size") if payload.get("label_size") is not None else form.get("label_size") or 14)
    except Exception:
        label_size = 14
    # Submit label + custom fonts
    submit_label = str(payload.get("submit_label") or form.get("submit_label") or "Request Booking")
    title_font_data = str(payload.get("title_font_data") or form.get("title_font_data") or "")
    subtitle_font_data = str(payload.get("subtitle_font_data") or form.get("subtitle_font_data") or "")
    label_font_data = str(payload.get("label_font_data") or form.get("label_font_data") or "")
    # Studio + Maps settings
    studio_address = str(payload.get("studio_address") or form.get("studio_address") or "")
    studio_lat = str(payload.get("studio_lat") or form.get("studio_lat") or "")
    studio_lng = str(payload.get("studio_lng") or form.get("studio_lng") or "")
    maps_api_key = str(payload.get("maps_api_key") or form.get("maps_api_key") or "")
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
        "title_align": title_align,
        "subtitle_align": subtitle_align,
        "title_size": int(max(8, min(96, title_size))),
        "subtitle_size": int(max(8, min(48, subtitle_size))),
        "title_font": str(title_font),
        "subtitle_font": str(subtitle_font),
        "label_font": str(label_font),
        "label_size": int(max(8, min(48, label_size))),
        "input_radius": int(max(0, min(32, input_radius))),
        "submit_label": submit_label,
        "title_font_data": title_font_data,
        "subtitle_font_data": subtitle_font_data,
        "label_font_data": label_font_data,
        "studio_address": studio_address,
        "studio_lat": studio_lat,
        "studio_lng": studio_lng,
        "maps_api_key": maps_api_key,
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

    # Title/subtitle appearance overrides
    try:
        title_align = str(request.query_params.get("title_align") or form.get("title_align") or "center").lower()
        if title_align not in ("left","center","right"): title_align = "center"
    except Exception:
        title_align = "center"
    try:
        subtitle_align = str(request.query_params.get("subtitle_align") or form.get("subtitle_align") or "center").lower()
        if subtitle_align not in ("left","center","right"): subtitle_align = "center"
    except Exception:
        subtitle_align = "center"
    try:
        title_size = int(request.query_params.get("title_size") or form.get("title_size") or 28)
    except Exception:
        title_size = 28
    try:
        subtitle_size = int(request.query_params.get("subtitle_size") or form.get("subtitle_size") or 14)
    except Exception:
        subtitle_size = 14
    try:
        title_font = str(request.query_params.get("title_font") or form.get("title_font") or "Inter")
    except Exception:
        title_font = "Inter"
    try:
        subtitle_font = str(request.query_params.get("subtitle_font") or form.get("subtitle_font") or "Inter")
    except Exception:
        subtitle_font = "Inter"
    try:
        label_font = str(request.query_params.get("label_font") or form.get("label_font") or "Inter")
    except Exception:
        label_font = "Inter"
    try:
        label_size = int(request.query_params.get("label_size") or form.get("label_size") or 14)
    except Exception:
        label_size = 14

    try:
        submit_label = str(request.query_params.get("submit_label") or form.get("submit_label") or "Request Booking")
    except Exception:
        submit_label = "Request Booking"

    # Studio/maps from saved form or query overrides
    maps_api_key = str(request.query_params.get("maps_api_key") or form.get("maps_api_key") or "")
    studio_address = str(request.query_params.get("studio_address") or form.get("studio_address") or "")
    studio_lat = str(request.query_params.get("studio_lat") or form.get("studio_lat") or "")
    studio_lng = str(request.query_params.get("studio_lng") or form.get("studio_lng") or "")

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
        title_align=title_align,
        subtitle_align=subtitle_align,
        title_size=title_size,
        subtitle_size=subtitle_size,
        label_size=label_size,
        title_font=title_font,
        subtitle_font=subtitle_font,
        label_font=label_font,
        submit_label=submit_label,
        title_font_data=str(form.get("title_font_data") or ""),
        subtitle_font_data=str(form.get("subtitle_font_data") or ""),
        label_font_data=str(form.get("label_font_data") or ""),
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
    title_align: str = "center",
    subtitle_align: str = "center",
    title_size: int = 28,
    subtitle_size: int = 14,
    title_font: str = "Inter",
    subtitle_font: str = "Inter",
    label_font: str = "Inter",
    label_size: int = 14,
    submit_label: str = "Request Booking",
    title_font_data: str = "",
    subtitle_font_data: str = "",
    label_font_data: str = "",
    studio_address: str = "",
    studio_lat: str = "",
    studio_lng: str = "",
    maps_api_key: str = "",
) -> str:
    # Build Google Fonts link tags for requested families (simple sanitization)
    def _safe_font(f: str) -> str:
        try:
            return "".join(ch for ch in f if ch.isalnum() or ch in (" ", "+", "-")) or "Inter"
        except Exception:
            return "Inter"
    families = []
    for fam in (str(title_font), str(subtitle_font), str(label_font), 'Inter'):
        sf = _safe_font(fam)
        if sf and sf not in families:
            families.append(sf)
    font_links = "\n        ".join([
        f'<link href="https://fonts.googleapis.com/css2?family={fn.replace(" ", "+")}:wght@400;600&display=swap" rel="stylesheet"/>'
        for fn in families
    ])

    maps_script = f"<script src=\"https://maps.googleapis.com/maps/api/js?key={maps_api_key}&libraries=places\"></script>" if maps_api_key else ""

    # Custom @font-face from uploaded Data URLs
    custom_fonts_css = ""
    if title_font_data:
        custom_fonts_css += f"""
        @font-face {{
            font-family: 'CustomTitleFont';
            src: url({title_font_data});
            font-weight: 400 700;
            font-style: normal;
            font-display: swap;
        }}
        """
    if subtitle_font_data:
        custom_fonts_css += f"""
        @font-face {{
            font-family: 'CustomSubtitleFont';
            src: url({subtitle_font_data});
            font-weight: 400 700;
            font-style: normal;
            font-display: swap;
        }}
        """
    if label_font_data:
        custom_fonts_css += f"""
        @font-face {{
            font-family: 'CustomLabelFont';
            src: url({label_font_data});
            font-weight: 400 700;
            font-style: normal;
            font-display: swap;
        }}
        """

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
    .title-card {{
        background: {card_bg};
        border-radius: 16px;
        padding: 20px 24px;
        box-shadow: 0 6px 18px rgba(0,0,0,0.08);
        margin-bottom: 20px;
    }}
    h1 {{
        font-size: {int(max(8, min(96, title_size)))}px;
        font-weight: 600;
        margin: 0 0 8px 0;
        text-align: {title_align};
        font-family: {"'CustomTitleFont', " if title_font_data else ''}'{_safe_font(title_font)}', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    }}
    .subtitle {{
        text-align: {subtitle_align};
        font-size: {int(max(8, min(48, subtitle_size)))}px;
        font-family: {"'CustomSubtitleFont', " if subtitle_font_data else ''}'{_safe_font(subtitle_font)}', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    }}
    .form-card {{
        background: {card_bg};
        border-radius: 16px;
        padding: 32px;
        box-shadow: 0 6px 18px rgba(0,0,0,0.08);
    }}
    .map-wrap {{
        margin-top: 8px;
    }}
    #map {{
        width: 100%;
        height: 240px;
        border-radius: 12px;
        border: 1px solid #d1d5db;
    }}
    .muted {{
        color: #6b7280;
        font-size: 13px;
    }}
    .field {{
        margin-bottom: 20px;
    }}
    .field label {{
        font-size: {int(max(8, min(48, label_size)))}px;
        font-weight: 500;
        margin-bottom: 6px;
        display: block;
        font-family: {"'CustomLabelFont', " if label_font_data else ''}'{_safe_font(label_font)}', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
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
        margin-top: 0;
        opacity: 0.85;
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
        {font_links}
        {maps_script}
        <style>{custom_fonts_css}{css}</style>
      </head>
      <body>
        <div class="container">
          <div class="title-card">
            <h1>{title_text}</h1>
            <div class='note subtitle'>{subtitle_text}</div>
          </div>
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
              <div class='field'>
                <label>Location</label>
                <div>
                  {"" if not allow_in_studio else "<label style='margin-right:12px'><input type='checkbox' id='inStudio'/> In studio</label>"}
                  <label><input type='checkbox' id='customLoc'/> Custom location</label>
                </div>
                <input id='placeInput' type='text' placeholder='Type a place or address' style='display:none;margin-top:8px;width:100%;padding:10px;border:1px solid #d1d5db;border-radius:8px' />
                <div class='map-wrap'><div id='map'></div></div>
                <div class='muted'>Pick a location or choose In studio.</div>
                <input type='hidden' name='location' id='locField'/>
                <input type='hidden' name='latitude' id='latField'/>
                <input type='hidden' name='longitude' id='lngField'/>
              </div>
              <button type='submit'>{submit_label}</button>
              <div id='msg' class='note'></div>
            </form>
          </div>
          <script>
            (function() {{
              let map = null, marker = null, autocomplete = null;
              const inStudioCb = document.getElementById('inStudio');
              const customCb = document.getElementById('customLoc');
              const placeInput = document.getElementById('placeInput');
              const locField = document.getElementById('locField');
              const latField = document.getElementById('latField');
              const lngField = document.getElementById('lngField');
              const studio = {{
                address: {repr(studio_address)},
                lat: parseFloat({repr(studio_lat)}) || null,
                lng: parseFloat({repr(studio_lng)}) || null,
              }};

              function setMarker(pos, title) {{
                if (!map) return;
                if (!marker) marker = new google.maps.Marker({{ map }});
                marker.setPosition(pos);
                if (title) marker.setTitle(title);
                map.setCenter(pos);
              }}

              function useStudio() {{
                if (studio.lat && studio.lng) {{
                  const pos = {{ lat: studio.lat, lng: studio.lng }};
                  setMarker(pos, studio.address || 'Studio');
                  locField.value = studio.address || 'Studio';
                  latField.value = String(studio.lat);
                  lngField.value = String(studio.lng);
                }} else {{
                  // No studio set — clear fields
                  locField.value = '';
                  latField.value = '';
                  lngField.value = '';
                }}
              }}

              window.onSubmit = async function(e) {{
                // Validate: if custom is checked ensure coords present
                if (customCb && customCb.checked) {{
                  if (!latField.value || !lngField.value) {{
                    e.preventDefault();
                    alert('Please select a custom location on the map.');
                    return false;
                  }}
                }}

                // Submit via fetch to keep the form on the page and show a message
                e.preventDefault();
                const form = e.target;
                const msgEl = document.getElementById('msg');
                if (msgEl) msgEl.textContent = 'Sending...';
                try {{
                  const res = await fetch(form.action || '/api/booking/submit', {{
                    method: 'POST',
                    body: new FormData(form),
                    credentials: 'same-origin'
                  }});
                  let data = null;
                  try {{ data = await res.json(); }} catch(_err) {{ data = null; }}
                  if (res.ok && data && data.ok) {{
                    if (msgEl) msgEl.textContent = 'Thank you! Your request has been sent.';
                  }} else {{
                    if (msgEl) msgEl.textContent = 'Something went wrong. Please try again.';
                  }}
                }} catch (err) {{
                  if (msgEl) msgEl.textContent = 'Network error. Please try again.';
                }}
                return false;
              }}

              function init() {{
                const mapEl = document.getElementById('map');
                if (!mapEl) return;
                const hasMaps = !!(window.google && window.google.maps);
                const start = {{ lat: 37.7749, lng: -122.4194 }}; // fallback
                map = hasMaps ? new google.maps.Map(mapEl, {{ center: start, zoom: 12 }}) : null;
                if (hasMaps) {{ marker = new google.maps.Marker({{ map }}); }}

                if (hasMaps && placeInput) {{
                  autocomplete = new google.maps.places.Autocomplete(placeInput, {{ fields: ['formatted_address','geometry'] }});
                  autocomplete.addListener('place_changed', () => {{
                    const p = autocomplete.getPlace();
                    if (p && p.geometry && p.geometry.location) {{
                      const pos = {{ lat: p.geometry.location.lat(), lng: p.geometry.location.lng() }};
                      setMarker(pos, p.formatted_address || 'Location');
                      locField.value = p.formatted_address || '';
                      latField.value = String(pos.lat);
                      lngField.value = String(pos.lng);
                    }}
                  }});
                }}

                if (inStudioCb) {{
                  inStudioCb.addEventListener('change', () => {{
                    if (inStudioCb.checked) {{
                      if (customCb) customCb.checked = false;
                      placeInput && (placeInput.style.display = 'none');
                      useStudio();
                    }} else {{
                      // Cleared
                      if (!customCb || !customCb.checked) {{
                        locField.value = latField.value = lngField.value = '';
                      }}
                    }}
                  }});
                }}

                if (customCb) {{
                  customCb.addEventListener('change', () => {{
                    if (customCb.checked) {{
                      if (inStudioCb) inStudioCb.checked = false;
                      placeInput && (placeInput.style.display = 'block');
                      placeInput && placeInput.focus();
                      // Clear until user picks
                      locField.value = latField.value = lngField.value = '';
                    }} else {{
                      placeInput && (placeInput.style.display = 'none');
                      if (!inStudioCb || !inStudioCb.checked) {{
                        locField.value = latField.value = lngField.value = '';
                      }}
                    }}
                  }});
                }}

                // Initialize default selection
                if (inStudioCb && inStudioCb.checked) useStudio();
              }}

              if (document.readyState === 'loading') {{
                document.addEventListener('DOMContentLoaded', init);
              }} else {{ init(); }}
            }})();
          </script>
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
    submit_label = _pick("submit_label", default="Request Booking")

    maps_api_key = _pick("maps_api_key", default="")
    studio_address = _pick("studio_address", default="")
    studio_lat = _pick("studio_lat", default="")
    studio_lng = _pick("studio_lng", default="")

    # Appearance (title/subtitle)
    _ta = _pick("title_align", default="center").lower()
    title_align = _ta if _ta in ("left","center","right") else "center"
    _sa = _pick("subtitle_align", default="center").lower()
    subtitle_align = _sa if _sa in ("left","center","right") else "center"
    try:
        title_size = int(_pick("title_size", default="28"))
    except Exception:
        title_size = 28
    try:
        subtitle_size = int(_pick("subtitle_size", default="14"))
    except Exception:
        subtitle_size = 14
    try:
        label_size = int(_pick("label_size", default="14"))
    except Exception:
        label_size = 14
    title_font = _pick("title_font", default="Inter")
    subtitle_font = _pick("subtitle_font", default="Inter")
    label_font = _pick("label_font", default="Inter")
    title_font_data = _pick("title_font_data", default="")
    subtitle_font_data = _pick("subtitle_font_data", default="")

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
        title_align=title_align,
        subtitle_align=subtitle_align,
        title_size=title_size,
        subtitle_size=subtitle_size,
        label_size=label_size,
        title_font=title_font,
        subtitle_font=subtitle_font,
        label_font=label_font,
        submit_label=submit_label,
        title_font_data=title_font_data,
        subtitle_font_data=subtitle_font_data,
        studio_address=studio_address,
        studio_lat=studio_lat,
        studio_lng=studio_lng,
        maps_api_key=maps_api_key,
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
