from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os

from app.core.config import logger

# Routers
from app.routers import images, photos, auth, convert, vaults, voice, collab, luts

app = FastAPI(title="Photo Watermarker")

# ---- CORS setup ----
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "https://photomark.cloud").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Static mount (local fallback) ----
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ---- LUTs init (optional) ----
try:
    from app.utils.luts import load_luts_from_dir
    LUTS_DIR = os.getenv("LUTS_DIR") or os.path.join(static_dir, "luts")
    load_luts_from_dir(LUTS_DIR)
except Exception as _ex:
    logger.warning(f"LUTs not initialized: {_ex}")

# ---- Include routers ----
app.include_router(images.router)
app.include_router(photos.router)
app.include_router(auth.router)
app.include_router(convert.router)
app.include_router(vaults.router)
app.include_router(voice.router)
app.include_router(collab.router)
# LUTs endpoints
app.include_router(luts.router)
# embed iframe endpoints
from app.routers import embed  # noqa: E402
app.include_router(embed.router)
# extra endpoints for frontend compatibility
from app.routers import upload, device  # noqa: E402
app.include_router(upload.router)
app.include_router(device.router)

# new endpoints for signup and account email change
from app.routers import auth_ip, account  # noqa: E402
app.include_router(auth_ip.router)
app.include_router(account.router)

# retouch endpoints (AI background)
from app.routers import retouch  # noqa: E402
app.include_router(retouch.router)

# instructions-based AI retouch (pix2pix)
try:
    from app.routers import instruct  # noqa: E402
    app.include_router(instruct.router)
except Exception as _ex:
    logger.warning(f"instruct router not available: {_ex}")



# affiliate endpoints (secret invite sender)
from app.routers import affiliates  # noqa: E402
app.include_router(affiliates.router)

# lens simulation + camera DB endpoints
from app.routers import lens, camera_db  # noqa: E402
app.include_router(lens.router)
app.include_router(camera_db.router)

# copyright defense endpoints
from app.routers import copyright_defense  # noqa: E402
app.include_router(copyright_defense.router)


@app.get("/")
def root():
    return {"ok": True}