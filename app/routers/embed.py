from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
import json

# Reuse manifest builder from photos router so we can inline data and avoid client-side CORS
from app.routers.photos import _build_manifest

router = APIRouter(prefix="/embed", tags=["embed"]) 


def _html_page(content: str) -> HTMLResponse:
    return HTMLResponse(content=content, media_type="text/html; charset=utf-8")


@router.get("/gallery")
def embed_gallery(uid: str = Query(..., min_length=3, max_length=64), limit: int = Query(10, ge=1, le=50)):
    # Build manifest server-side and inline it to avoid CORS in iframes or file:// origins
    data = _build_manifest(uid)
    photos = (data.get("photos") or [])[:limit]
    payload = json.dumps({"photos": photos}, ensure_ascii=False)

    html = f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Photomark Gallery</title>
  <style>
    :root {{ color-scheme: dark; }}
    html, body {{ margin:0; height:100%; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:#0b0b0b; color:#ddd; }}
    .wrap {{ padding:12px; }}
    .grid {{ display:grid; grid-template-columns: repeat( auto-fill, minmax(160px, 1fr) ); gap:10px; }}
    .card {{ border:1px solid #2b2b2b; border-radius:10px; overflow:hidden; background:rgba(255,255,255,0.03); }}
    .card img {{ width:100%; height:160px; object-fit:cover; display:block; background:#111; }}
    .cap {{ font-size:12px; color:#a0a0a0; padding:6px 8px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
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