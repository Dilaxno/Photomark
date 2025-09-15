import os
import json
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse

from app.core.config import STATIC_DIR

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])  # included by app.main


def _safe_site_name(name: str) -> str:
    n = (name or "").strip().lower()
    n = "".join(ch for ch in n if (ch.isalnum() or ch in ("-", "_")))
    if not n:
        raise ValueError("invalid site name")
    return n[:64]


def _ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)


TEMPLATE_DIR = os.path.abspath(r"D:\Software\Portfolio")

def _copytree(src: str, dst: str):
    import shutil
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

def _replace_placeholders(root: str, mapping: dict):
    # Replace {{PLACEHOLDER}} in common text files
    exts = {'.html', '.htm'}
    for base, _, files in os.walk(root):
        for name in files:
            _, ext = os.path.splitext(name)
            if ext.lower() in exts:
                path = os.path.join(base, name)
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        s = f.read()
                    for k, v in mapping.items():
                        s = s.replace('{{' + k + '}}', str(v))
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write(s)
                except Exception:
                    pass


@router.post("/publish")
async def publish_portfolio(
    site_name: str = Form(...),
    photos_json: str = Form("[]"),
    logo: Optional[UploadFile] = File(None),
    title_font: Optional[UploadFile] = File(None),
    body_font: Optional[UploadFile] = File(None),
    title_text: str = Form("Portfolio"),
    subtitle_text: str = Form(""),
):
    try:
        safe_name = _safe_site_name(site_name)
        base_dir = os.path.abspath(os.path.join(STATIC_DIR, "portfolios", safe_name))
        _ensure_dir(base_dir)
        assets_dir = os.path.join(base_dir, "assets")
        _ensure_dir(assets_dir)

        # Save assets
        logo_rel = ""
        if logo is not None:
            logo_ext = os.path.splitext(logo.filename or "")[1] or ".png"
            logo_path = os.path.join(assets_dir, f"logo{logo_ext}")
            with open(logo_path, "wb") as f:
                f.write(await logo.read())
            logo_rel = f"assets/logo{logo_ext}"

        title_font_rel = ""
        if title_font is not None:
            t_ext = os.path.splitext(title_font.filename or "")[1] or ".woff2"
            t_path = os.path.join(assets_dir, f"title{t_ext}")
            with open(t_path, "wb") as f:
                f.write(await title_font.read())
            title_font_rel = f"assets/title{t_ext}"

        body_font_rel = ""
        if body_font is not None:
            b_ext = os.path.splitext(body_font.filename or "")[1] or ".woff2"
            b_path = os.path.join(assets_dir, f"body{b_ext}")
            with open(b_path, "wb") as f:
                f.write(await body_font.read())
            body_font_rel = f"assets/body{b_ext}"

        # Parse photos
        try:
            photos = json.loads(photos_json or "[]")
            photos = [str(u) for u in (photos if isinstance(photos, list) else [])]
        except Exception:
            photos = []

        # Generate CSS with optional font-face
        css_parts = [
            "*{box-sizing:border-box}",
            "body{margin:0;background:#0b0b0c;color:#fafafa;font-family:system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica Neue, Arial, 'Apple Color Emoji','Segoe UI Emoji';}",
            ".container{max-width:1200px;margin:0 auto;padding:24px}",
            ".header{display:flex;align-items:center;gap:16px;padding:24px 0;border-bottom:1px solid rgba(255,255,255,.08)}",
            ".logo{height:44px}",
            ".title{font-size:clamp(28px,5vw,48px);font-weight:800;letter-spacing:-0.02em;line-height:1.1;margin:0}",
            ".subtitle{opacity:.75;margin-top:6px}",
            ".grid{display:grid;gap:12px;grid-template-columns:repeat(1,minmax(0,1fr))}",
            "@media(min-width:640px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}",
            "@media(min-width:1024px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}",
            ".card{border:1px solid rgba(255,255,255,.08);border-radius:16px;overflow:hidden;background:rgba(255,255,255,.03)}",
            ".img{width:100%;height:100%;object-fit:cover;display:block;background:#111}",
            ".figure{aspect-ratio:4/3}",
            ".footer{padding:24px 0;border-top:1px solid rgba(255,255,255,.08);opacity:.8;font-size:13px}"
        ]
        font_css = []
        if title_font_rel:
            font_css.append(f"@font-face{{font-family:'PortfolioTitle';src:url('{title_font_rel}') format('woff2');font-weight:400;font-style:normal;font-display:swap}}")
        if body_font_rel:
            font_css.append(f"@font-face{{font-family:'PortfolioBody';src:url('{body_font_rel}') format('woff2');font-weight:400;font-style:normal;font-display:swap}}")
        if font_css:
            css_parts.append("body{font-family:'PortfolioBody',system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,Helvetica Neue,Arial}" )
            css_parts.append(".title{font-family:'PortfolioTitle',inherit}")
        css = "\n".join(font_css + css_parts)

        # Build HTML
        logo_html = f"<img src='{logo_rel}' alt='' class='logo'/>" if logo_rel else ""
        figures = "\n".join([f"<figure class='card figure'><img class='img' src='{u}' alt=''/></figure>" for u in photos])
        html = f"""
<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>{site_name} — Portfolio</title>
<meta name=\"robots\" content=\"index,follow\" />
<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>
<style>{css}</style>
</head>
<body>
  <div class=\"container\">
    <header class=\"header\">{logo_html}<div>
      <h1 class=\"title\">{title_text}</h1>
      {f'<div class=\\"subtitle\\">{subtitle_text}</div>' if subtitle_text else ''}
    </div></header>

    <main class=\"main\">
      <section class=\"grid\">
        {figures}
      </section>
    </main>

    <footer class=\"footer\">Built with Photomark</footer>
  </div>
</body>
</html>
"""
        # Write files
        index_path = os.path.join(base_dir, "index.html")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(html)

        # Public URL (served via /static mount). Adjust to your domain as needed.
        public_path = f"/static/portfolios/{safe_name}/index.html"
        return JSONResponse({"ok": True, "url": public_path})
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)


@router.post("/apply-template")
async def apply_template(
    site_name: str = Form(...),
    display_name: str = Form(""),
    description: str = Form(""),
    about_text: str = Form(""),
    contact_text: str = Form(""),
    hero: Optional[UploadFile] = File(None),
    gallery_count: Optional[int] = Form(0),
    # Accept gallery files as gallery_0, gallery_1, ...
    **kwargs,
):
    try:
        safe_name = _safe_site_name(site_name)
        if not os.path.isdir(TEMPLATE_DIR):
            return JSONResponse({"error": "template_not_found", "path": TEMPLATE_DIR}, status_code=400)
        base_dir = os.path.abspath(os.path.join(STATIC_DIR, "portfolios", safe_name, "site"))
        _ensure_dir(os.path.dirname(base_dir))
        _copytree(TEMPLATE_DIR, base_dir)

        assets_dir = os.path.join(base_dir, "assets")
        gallery_dir = os.path.join(assets_dir, "gallery")
        _ensure_dir(assets_dir)
        _ensure_dir(gallery_dir)

        # Write hero
        if hero is not None:
            h_ext = os.path.splitext(hero.filename or "")[1] or ".jpg"
            hero_path = os.path.join(assets_dir, f"hero{h_ext}")
            with open(hero_path, "wb") as f:
                f.write(await hero.read())

        # Write gallery images
        try:
            n = int(gallery_count or 0)
        except Exception:
            n = 0
        for i in range(n):
            key = f"gallery_{i}"
            file = kwargs.get(key)
            if isinstance(file, UploadFile) and file.filename:
                g_ext = os.path.splitext(file.filename or "")[1] or ".jpg"
                g_path = os.path.join(gallery_dir, f"img{i+1}{g_ext}")
                with open(g_path, "wb") as f:
                    f.write(await file.read())

        # Replace placeholders in HTML files if present
        mapping = {
            'SITE_NAME': display_name or site_name,
            'DESCRIPTION': description,
            'ABOUT': about_text,
            'CONTACT': contact_text,
            # Common asset references if template uses placeholders
            'HERO_SRC': 'assets/hero.jpg',
        }
        _replace_placeholders(base_dir, mapping)

        public_path = f"/static/portfolios/{safe_name}/site/index.html"
        return JSONResponse({"ok": True, "url": public_path})
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
