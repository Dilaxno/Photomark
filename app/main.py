from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os

from app.core.config import logger

# Routers
from app.routers import images, photos, auth, convert, vaults, voice

app = FastAPI(title="Photo Watermarker")

# ---- CORS setup ----
CORS_ORIGINS = os.getenv("CORS_ORIGINS") or os.getenv("FRONTEND_ORIGIN") or ""
origins = [o.strip().rstrip("/") for o in CORS_ORIGINS.split(",") if o.strip()] if CORS_ORIGINS else []

if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"]
    )
else:
    # Allow common dev origins via regex; do not allow credentials in wildcard mode
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$|https?://.*\.ngrok-.*\.app$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"]
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

# collaboration endpoints
from app.routers import collab  # noqa: E402
app.include_router(collab.router)

# affiliate endpoints (secret invite sender)
from app.routers import affiliates  # noqa: E402
app.include_router(affiliates.router)

# lens simulation endpoints
from app.routers import lens  # noqa: E402
app.include_router(lens.router)


@app.get("/")
def root():
    return {"ok": True}