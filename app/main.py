from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os

from app.core.config import logger

# Routers
from app.routers import images, photos, auth, convert, vaults, voice, collab, gallery_assistant, color_grading

app = FastAPI(title="Photo Watermarker")

# ---- CORS setup ----
# Prefer ALLOWED_ORIGINS, but also support legacy env names used in .env
_origins_env = os.getenv("ALLOWED_ORIGINS") or os.getenv("CORS_ORIGINS") or os.getenv("FRONTEND_ORIGIN") or "https://photomark.cloud"
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]
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



# ---- Include routers ----
app.include_router(images.router)
app.include_router(photos.router)
app.include_router(auth.router)
app.include_router(convert.router)
app.include_router(vaults.router)
app.include_router(voice.router)
app.include_router(collab.router)
# Gallery assistant (chat + actions)
app.include_router(gallery_assistant.router)
# Color grading (LUT)
app.include_router(color_grading.router)
# embed iframe endpoints
from app.routers import embed  # noqa: E402
app.include_router(embed.router)
# extra endpoints for frontend compatibility
from app.routers import upload, device  # noqa: E402
app.include_router(upload.router)
app.include_router(device.router)
# style LUT GPU endpoint
try:
    from app.routers import style_lut  # noqa: E402
    app.include_router(style_lut.router)
except Exception as _ex:
    logger.warning(f"style_lut router not available: {_ex}")

# new endpoints for signup and account email change
from app.routers import auth_ip, account  # noqa: E402
app.include_router(auth_ip.router)
app.include_router(account.router)

# retouch endpoints (AI background)
from app.routers import retouch  # noqa: E402
app.include_router(retouch.router)

# Moodboard generator
try:
    from app.routers import moodboard  # noqa: E402
    app.include_router(moodboard.router)
except Exception as _ex:
    logger.warning(f"moodboard router not available: {_ex}")

# Stable Diffusion img2img endpoint
try:
    from app.routers import sd_img2img  # noqa: E402
    app.include_router(sd_img2img.router)
except Exception as _ex:
    logger.warning(f"sd_img2img router not available: {_ex}")

# instructions-based edit tool removed

# prelaunch subscription endpoint
try:
    from app.routers import prelaunch  # noqa: E402
    app.include_router(prelaunch.router)
except Exception as _ex:
    logger.warning(f"prelaunch router not available: {_ex}")



# affiliate endpoints (secret invite sender)
from app.routers import affiliates  # noqa: E402
app.include_router(affiliates.router)

# Dodo payment webhook for affiliate conversions
try:
    from app.routers.webhooks_dodo import router as dodo_router  # noqa: E402
    app.include_router(dodo_router)
except Exception as _ex:
    logger.warning(f"dodo webhook router not available: {_ex}")

# outreach email endpoint (photographer/artist introduction)
from app.routers import outreach  # noqa: E402
app.include_router(outreach.router)

# inbound email replies + list for UI
from app.routers import replies  # noqa: E402
app.include_router(replies.router)

# lens simulation + camera DB endpoints
from app.routers import lens, camera_db  # noqa: E402
app.include_router(lens.router)
app.include_router(camera_db.router)

# product updates (changelog + email broadcast)
try:
    from app.routers import updates  # noqa: E402
    app.include_router(updates.router)
except Exception as _ex:
    logger.warning(f"updates router not available: {_ex}")



@app.get("/")
def root():
    return {"ok": True}