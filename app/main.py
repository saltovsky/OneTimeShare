"""FastAPI application entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from .cleanup import cleanup_loop
from .config import settings
from .database import async_session_factory, init_db
from .models import Setting
from .routers import admin, auth, download, upload
from .security import get_current_user_optional

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("onetimeshare")


# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    await init_db()

    if settings.SECRET_KEY_AUTO_GENERATED:
        logger.warning(
            "SECRET_KEY env var is not set; a random one was generated. "
            "Set SECRET_KEY in .env so download cookies survive restarts."
        )

    cleanup_task = asyncio.create_task(cleanup_loop(), name="cleanup-loop")
    logger.info("OneTimeShare started; data dir = %s", settings.DATA_DIR)
    try:
        yield
    finally:
        # --- shutdown ---
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        logger.info("OneTimeShare stopped")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="OneTimeShare",
    description="Burn-after-reading file sharing",
    version="1.0.0",
    docs_url=None,        # keep the API surface minimal in production
    redoc_url=None,
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(download.router)
app.include_router(admin.router)

# Static files (i18n.js, etc.)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)


# ---------------------------------------------------------------------------
# Health endpoint (used by Docker healthcheck)
# ---------------------------------------------------------------------------
@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Friendly error pages
# ---------------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # For API endpoints, return JSON; for HTML pages, render the error template.
    accept = request.headers.get("accept", "")
    if "text/html" in accept and not request.url.path.startswith("/api/"):
        templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

        # Fetch footer text from DB
        footer_text = None
        try:
            async with async_session_factory() as db:
                result = await db.execute(select(Setting).where(Setting.key == "footer_text"))
                setting = result.scalar_one_or_none()
                if setting:
                    footer_text = setting.value
        except Exception:
            pass  # If DB is unavailable, fall back to None

        current_user = get_current_user_optional(request)

        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "status_code": exc.status_code,
                "detail": exc.detail,
                "footer_text": footer_text,
                "current_user": current_user,
            },
            status_code=exc.status_code,
        )
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers or None
    )
