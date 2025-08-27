from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
import json

# Reuse manifest builder from photos router so we can inline data and avoid client-side CORS
from app.routers.photos import _build_manifest

router = APIRouter(prefix="/embed", tags=["embed"]) 


def _html_page(content: str) -> HTMLResponse:
    return HTMLResponse(content=content, media_type="text/html; charset=utf-8")


@router.get("/gallery")
def embed_gallery(
    uid: str = Query(..., min_length=3, max_length=64),
    limit: str = Query("10"),
    theme: str = Query("dark"),
    bg: str | None = Query(None, min_length=1, max_length=32),
):
    # Build manifest server-side and inline it to avoid CORS in iframes or file:// origins
    data = _build_manifest(uid)
    # Handle limit: allow numbers or 'all'
    photos_all = data.get("photos") or []
    if isinstance(limit, str) and limit.lower() == "all":
        photos = photos_all
    else:
        try:
            n = int(limit)
        except Exception:
            n = 10
        # clamp for safety
        n = max(1, min(n, 200))
        photos = photos_all[:n]
    payload = json.dumps({"photos": photos}, ensure_ascii=False)

    # Theme variables
    t = (theme or "dark").lower()
    cs = "light" if t == "light" else "dark"
    if t == "light":
        bg_default = "#ffffff"; fg = "#111111"; border = "#dddddd"; card_bg = "rgba(0,0,0,0.03)"; cap = "#666666"
    else:
        bg_default = "#0b0b0b"; fg = "#dddddd"; border = "#2b2b2b"; card_bg = "rgba(255,255,255,0.03)"; cap = "#a0a0a0"

    # Allow custom background via ?bg= (hex only for safety: #RGB, #RGBA, #RRGGBB, #RRGGBBAA)
    bg_value = bg_default
    if isinstance(bg, str):
        s = bg.strip()
        if s.startswith('#'):
            h = s[1:]
            if len(h) in (3, 4, 6, 8) and all(c in '0123456789abcdefABCDEF' for c in h):
                bg_value = s

    html = f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Photomark Gallery</title>
  <style>
    :root {{ color-scheme: {cs}; }}
    html, body {{ margin:0; height:100%; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:{bg_value}; color:{fg}; }}
    .wrap {{ padding:12px; }}
    .grid {{ display:grid; grid-template-columns: repeat( auto-fill, minmax(160px, 1fr) ); gap:10px; }}
    .card {{ border:1px solid {border}; border-radius:10px; overflow:hidden; background:{card_bg}; }}
    .card img {{ width:100%; height:160px; object-fit:cover; display:block; background:#111; }}
    .cap {{ font-size:12px; color:{cap}; padding:6px 8px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div id=\"pm-grid\" class=\"grid\"></div>
  </div>
  <script>
    (function(){{
      var DATA = {payload};
      var grid=document.getElementById('pm-grid');
      if(!grid) return;
      var photos=(DATA && DATA.photos) || [];
      photos.forEach(function(p){{
        var card=document.createElement('div'); card.className='card';
        var img=document.createElement('img'); img.loading='lazy'; img.decoding='async'; img.src=p.url; img.alt=p.name||'';
        var cap=document.createElement('div'); cap.className='cap'; cap.textContent=p.name||'';
        card.appendChild(img); card.appendChild(cap); grid.appendChild(card);
      }});
    }})();
  </script>
</body>
</html>"""
    return _html_page(html)