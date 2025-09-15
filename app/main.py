from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os
import warnings

from app.core.config import logger

# Silence a noisy Kornia FutureWarning (does not affect our watermark pipeline)
warnings.filterwarnings(
    "ignore",
    message=r"`torch\.cuda\.amp\.custom_fwd",
    category=FutureWarning,
    module=r"kornia\.feature\.lightglue"
)

# Routers
from app.routers import images, photos, auth, convert, vaults, voice, collab, gallery_assistant, color_grading

# Pricing checkout (server-side) removed in favor of client-side overlay

app = FastAPI(title="Photo Watermarker")

# ---- CORS setup ----
# Prefer ALLOWED_ORIGINS, but also support legacy env names used in .env
_default_origins = ",".join([
    "https://photomark.cloud",
    "https://www.photomark.cloud",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
])
_origins_env = os.getenv("ALLOWED_ORIGINS") or os.getenv("CORS_ORIGINS") or os.getenv("FRONTEND_ORIGIN") or _default_origins
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]
# Optional regex to match any photomark.cloud subdomain and scheme
_origin_regex_env = os.getenv("ALLOWED_ORIGINS_REGEX") or os.getenv("CORS_ORIGIN_REGEX") or r"https?://([a-z0-9-]+\.)?photomark\.cloud(:\d+)?$"
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=_origin_regex_env,
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

# app.include_router(pricing_checkout.router)  # removed
# embed iframe endpoints
from app.routers import embed  # noqa: E402
app.include_router(embed.router)
# extra endpoints for frontend compatibility
from app.routers import upload, device  # noqa: E402
app.include_router(upload.router)
app.include_router(device.router)
# bookings endpoints
try:
    from app.routers import bookings  # noqa: E402
    app.include_router(bookings.router)
except Exception as _ex:
    logger.warning(f"bookings router not available: {_ex}")
# portfolio publish endpoints
try:
    from app.routers import portfolio  # noqa: E402
    app.include_router(portfolio.router)
except Exception as _ex:
    logger.warning(f"portfolio router not available: {_ex}")
# style LUT GPU endpoint
try:
    from app.routers import style_lut  # noqa: E402
    app.include_router(style_lut.router)
except Exception as _ex:
    logger.warning(f"style_lut router not available: {_ex}")

# style histogram matching endpoint
try:
    from app.routers import style_hist  # noqa: E402
    app.include_router(style_hist.router)
except Exception as _ex:
    logger.warning(f"style_hist router not available: {_ex}")

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

# Stable Diffusion img2img endpoint removed

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

# Pricing webhook (replaces legacy Dodo webhook)
try:
    from app.routers import pricing_webhook  # noqa: E402
    app.include_router(pricing_webhook.router)

    # Backward-compatible Dodo webhook path
    from app.routers.pricing_webhook import pricing_webhook as _pricing_webhook_handler  # type: ignore

    @app.post("/api/payments/dodo/webhook")
    async def dodo_webhook(request: Request):
        return await _pricing_webhook_handler(request)
except Exception as _ex:
    logger.warning(f"pricing webhook router not available: {_ex}")

# outreach email endpoint (photographer/artist introduction)
from app.routers import outreach  # noqa: E402
app.include_router(outreach.router)

# inbound email replies + list for UI
from app.routers import replies  # noqa: E402
app.include_router(replies.router)

# lens simulation tool removed

# product updates (changelog + email broadcast)
try:
    from app.routers import updates  # noqa: E402
    app.include_router(updates.router)
except Exception as _ex:
    logger.warning(f"updates router not available: {_ex}")



@app.get("/")
def root():
    return {"ok": True}