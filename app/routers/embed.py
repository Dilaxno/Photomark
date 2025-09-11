from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
import json, os
from datetime import datetime
from app.routers.photos import _build_manifest
from app.core.config import s3, R2_BUCKET, R2_PUBLIC_BASE_URL, STATIC_DIR as static_dir

router = APIRouter(prefix="/embed", tags=["embed"])

def _html_page(content: str) -> HTMLResponse:
    return HTMLResponse(content=content, media_type="text/html; charset=utf-8")

def _color_theme(theme: str | None, bg: str | None):
    t = (theme or "dark").lower()
    cs = "light" if t == "light" else "dark"
    if t == "light":
        bg_default, fg, border, card_bg, cap, shadow = "#ffffff", "#111111", "#dddddd", "#ffffff", "#666666", "rgba(0,0,0,0.08)"
    else:
        bg_default, fg, border, card_bg, cap, shadow = "#0b0b0b", "#dddddd", "#2b2b2b", "#1a1a1a", "#a0a0a0", "rgba(0,0,0,0.35)"
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
    return cs, bg_value, fg, border, card_bg, cap, shadow

def _render_html(payload: dict, theme: str, bg: str | None, title: str):
    cs, bg_value, fg, border, card_bg, cap, shadow = _color_theme(theme, bg)
    data_json = json.dumps(payload, ensure_ascii=False)
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
    :root {{ color-scheme: {cs}; }}
    html, body {{ margin:0; height:100%; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:{bg_value}; color:{fg}; }}
    .grid {{ column-count: 2; column-gap: 0; }}
    @media (min-width: 1024px) {{ .grid {{ column-count: 4; }} }}
    .card {{ display:inline-block; width:100%; margin:0; border:none; border-radius:0; overflow:hidden; background:{card_bg}; break-inside: avoid; }}
    .card img {{ width:100%; display:block; aspect-ratio: 1 / 1; object-fit:cover; }}
</style>
</head>
<body>
<div class="grid" id="pm-grid"></div>
<script type="application/json" id="pm-data">{data_json}</script>
<script>
(function() {{
    var dataEl = document.getElementById('pm-data');
    if(!dataEl) return;
    var DATA = JSON.parse(dataEl.textContent);
    var grid = document.getElementById('pm-grid');
    if(!grid || !DATA.photos) return;
    var frag = document.createDocumentFragment();
    DATA.photos.forEach(function(p) {{
        var card = document.createElement('div');
        card.className = 'card';
        var img = document.createElement('img');
        img.loading = 'lazy';
        img.decoding = 'async';
        img.src = p.url;
        img.alt = '';
        frag.appendChild(card);
        card.appendChild(img);
    }});
    grid.appendChild(frag);
}})();
</script>
</body>
</html>"""

@router.get("/gallery")
def embed_gallery(
    uid: str = Query(..., min_length=3, max_length=64),
    limit: str = Query("10"),
    theme: str = Query("dark"),
    bg: str | None = Query(None, min_length=1, max_length=32),
    keys: str | None = Query(None, min_length=1),
):
    data = _build_manifest(uid)
    photos_all = data.get("photos") or []
    if keys and keys.strip():
        desired = [k.strip() for k in keys.split(',') if k.strip()]
        lookup = {p.get("key"): p for p in photos_all}
        photos = [lookup[k] for k in desired if k in lookup]
    else:
        if limit.lower() == "all":
            photos = photos_all
        else:
            try:
                n = int(limit)
            except:
                n = 10
            photos = photos_all[:max(1, min(n, 200))]
    return _html_page(_render_html({"photos": photos}, theme, bg, "Photomark Gallery"))

@router.get("/myuploads")
def embed_myuploads(
    uid: str = Query(..., min_length=3, max_length=64),
    limit: str = Query("10"),
    theme: str = Query("dark"),
    bg: str | None = Query(None, min_length=1, max_length=32),
    keys: str | None = Query(None, min_length=1),
):
    items: list[dict] = []
    prefix = f"users/{uid}/external/"
    if s3 and R2_BUCKET:
        try:
            client = s3.meta.client
            resp = client.list_objects_v2(Bucket=R2_BUCKET, Prefix=prefix, MaxKeys=1000)
            for entry in resp.get("Contents", []) or []:
                key = entry.get("Key", "")
                if not key or key.endswith("/"): continue
                name = os.path.basename(key)
                url = (
                    f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}" if R2_PUBLIC_BASE_URL else
                    client.generate_presigned_url("get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=3600)
                )
                items.append({"key": key, "url": url, "name": name, "last": (entry.get("LastModified") or datetime.utcnow()).isoformat()})
        except:
            items = []
    else:
        dir_path = os.path.join(static_dir, prefix)
        if os.path.isdir(dir_path):
            for root, _, files in os.walk(dir_path):
                for f in files:
                    local_path = os.path.join(root, f)
                    rel = os.path.relpath(local_path, static_dir).replace("\\", "/")
                    items.append({
                        "key": rel,
                        "url": f"/static/{rel}",
                        "name": f,
                        "last": datetime.utcfromtimestamp(os.path.getmtime(local_path)).isoformat(),
                    })
    items.sort(key=lambda x: x.get("last", ""), reverse=True)
    photos_all = [{"url": it["url"], "name": it["name"], "key": it["key"]} for it in items]
    if keys and keys.strip():
        desired = [k.strip() for k in keys.split(',') if k.strip()]
        lookup = {p.get("key"): p for p in photos_all}
        photos = [lookup[k] for k in desired if k in lookup]
    else:
        if limit.lower() == "all":
            photos = photos_all
        else:
            try:
                n = int(limit)
            except:
                n = 10
            photos = photos_all[:max(1, min(n, 200))]
    return _html_page(_render_html({"photos": photos}, theme, bg, "Photomark My Uploads"))

