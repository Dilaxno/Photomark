from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/embed", tags=["embed"])


def _html_page(content: str) -> HTMLResponse:
    return HTMLResponse(content=content, media_type="text/html; charset=utf-8")


@router.get("/gallery")
def embed_gallery(uid: str = Query(..., min_length=3, max_length=64), limit: int = Query(10, ge=1, le=50)):
    # Minimal, CSS-only grid. Uses existing /api/embed.js and manifest at /static/users/{uid}/embed/latest.json by default.
    # Accepts optional data-manifest via query in case user hosts manifest on CDN; but keeping page simple.
    html = f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Photomark Gallery</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:#0b0b0b; color:#ddd; }}
    .wrap {{ padding:12px; }}
    .grid {{ display:grid; grid-template-columns: repeat( auto-fill, minmax(160px, 1fr) ); gap:10px; }}
    .card {{ border:1px solid #2b2b2b; border-radius:10px; overflow:hidden; background:rgba(255,255,255,0.03); }}
    .card img {{ width:100%; height:160px; object-fit:cover; display:block; }}
    .cap {{ font-size:12px; color:#a0a0a0; padding:6px 8px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
    .foot {{ text-align:center; margin-top:10px; }}
    .foot a {{ color:#8ab4f8; text-decoration:none; font-size:13px; }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"photomark-embed\" id=\"photomark-embed\" data-uid=\"{uid}\" data-limit=\"{limit}\"></div>
  </div>
  <script>
    // Lightweight inlined renderer so iframe works even without external script
    (function(){{
      function render(container, data, limit){{
        container.innerHTML='';
        var grid=document.createElement('div'); grid.className='grid';
        (data.photos||[]).slice(0, limit).forEach(function(p){{
          var card=document.createElement('div'); card.className='card';
          var img=document.createElement('img'); img.src=p.url; img.alt=p.name||'';
          var cap=document.createElement('div'); cap.className='cap'; cap.textContent=p.name||'';
          card.appendChild(img); card.appendChild(cap); grid.appendChild(card);
        }});
        container.appendChild(grid);
      }}
      function resolveBase(){{
        try {{ var u=new URL(window.location.href); return u.origin; }} catch(e){{ return window.location.origin; }}
      }}
      function init(){{
        var el=document.getElementById('photomark-embed'); if(!el) return;
        var uid=el.getAttribute('data-uid'); var limit=parseInt(el.getAttribute('data-limit')||'10',10)||10;
        var manifest=el.getAttribute('data-manifest');
        if(!manifest){{
          // fallback to same-origin static path
          var base=resolveBase(); manifest= base + '/static/users/'+uid+'/embed/latest.json';
        }}
        fetch(manifest,{{cache:'no-store'}})
          .then(function(r){{ if(!r.ok) throw new Error('manifest '+r.status); return r.json(); }})
          .then(function(data){{ render(el, data, limit); }})
          .catch(function(err){{ el.innerHTML='<div style=\"color:#f88; font-size:13px;\">Failed to load gallery</div>'; if(console&&console.warn) console.warn('embed iframe error', err); }});
      }}
      if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', init); else init();
    }})();
  </script>
</body>
</html>"""
    return _html_page(html)