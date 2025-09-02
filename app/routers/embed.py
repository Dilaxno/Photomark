from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
import json
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
    keys: str | None = Query(None, min_length=1),
):
    # Build manifest server-side
    data = _build_manifest(uid)
    photos_all = data.get("photos") or []

    # If specific keys are provided, select only those (preserve order)
    photos = None
    if isinstance(keys, str) and keys.strip():
        desired = [k.strip() for k in keys.split(',') if k.strip()]
        lookup = {p.get("key"): p for p in photos_all}
        photos = [lookup[k] for k in desired if k in lookup]

    # Otherwise, handle limit
    if photos is None:
        if isinstance(limit, str) and limit.lower() == "all":
            photos = photos_all
        else:
            try:
                n = int(limit)
            except Exception:
                n = 10
            n = max(1, min(n, 200))
            photos = photos_all[:n]

    payload = json.dumps({"photos": photos}, ensure_ascii=False)

    # Theme defaults
    t = (theme or "dark").lower()
    cs = "light" if t == "light" else "dark"

    # Default colors
    if t == "light":
        bg_default = "#ffffff"
        fg = "#111111"
        border = "#dddddd"
        card_bg = "#ffffff"  # Cards match page background
        cap = "#666666"
        shadow = "rgba(0,0,0,0.08)"
    else:
        bg_default = "#0b0b0b"
        fg = "#dddddd"
        border = "#2b2b2b"
        card_bg = "#1a1a1a"
        cap = "#a0a0a0"
        shadow = "rgba(0,0,0,0.35)"

    # Allow custom background via ?bg=
    bg_value = bg_default
    if isinstance(bg, str):
        s = bg.strip()
        if s.lower() == "transparent":
            bg_value = "transparent"
            card_bg = "transparent"
        elif s.startswith('#'):
            h = s[1:]
            if len(h) in (3, 4, 6, 8) and all(c in '0123456789abcdefABCDEF' for c in h):
                bg_value = s
                card_bg = s

    # HTML
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Photomark Gallery</title>
<style>
    :root {{ color-scheme: {cs}; }}
    html, body {{ margin:0; height:100%; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:{bg_value}; color:{fg}; }}
    .wrap {{ padding:16px; }}
    .grid {{ display:grid; grid-template-columns: repeat(2, 1fr); gap:16px; }}
    @media (min-width: 1024px) {{ .grid {{ grid-template-columns: repeat(4, 1fr); }} }}
    .card {{ border:1px solid {border}; border-radius:10px; overflow:hidden; background:{card_bg}; box-shadow: 0 4px 12px {shadow}; }}
    .card img {{ width:100%; height:260px; object-fit:cover; display:block; background:#111; }}
    .cap {{ display:none; }}
</style>
</head>
<body>
<div class="wrap">
    <div id="pm-grid" class="grid"></div>
</div>
<script>
(function(){{
    var DATA = {payload};
    var grid = document.getElementById('pm-grid');
    if(!grid) return;
    var photos = (DATA && DATA.photos) || [];
    photos.forEach(function(p){{
        var card = document.createElement('div'); card.className='card';
        var img = document.createElement('img'); img.loading='lazy'; img.decoding='async'; img.src=p.url; img.alt='';
        card.appendChild(img);
        grid.appendChild(card);
    }});
}})();
</script>
</body>
</html>
"""
    return _html_page(html)
