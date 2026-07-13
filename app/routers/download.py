"""Download routes — HTML page, password unlock, and the actual stream endpoint.

Flow:
  GET  /d/{id}            -> render password form OR download button
  POST /d/{id}/unlock     -> verify password, set signed cookie, redirect to GET
  GET  /d/{id}/file       -> stream the file(s) and burn-after-reading

No authentication is required for these endpoints: possession of the link
(and the password, if any) is the credential.
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..database import get_db
from ..models import File, Link, Setting
from ..security import (
    COOKIE_MAX_AGE,
    DOWNLOAD_COOKIE_NAME,
    get_current_user_optional,
    issue_download_token,
    sanitize_filename,
    verify_download_token,
    verify_password,
)
from ..streaming import burn_after_stream, stream_single_file, stream_zip_files

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_link(db: AsyncSession, link_id: str) -> Optional[Link]:
    result = await db.execute(
        select(Link).options(selectinload(Link.files)).where(Link.id == link_id)
    )
    return result.scalar_one_or_none()


async def _get_footer_text(db: AsyncSession) -> Optional[str]:
    result = await db.execute(select(Setting).where(Setting.key == "footer_text"))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


def _cookie_name(link_id: str) -> str:
    """Kept for API stability — returns the single shared cookie name."""
    return DOWNLOAD_COOKIE_NAME


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


# ---------------------------------------------------------------------------
# GET /d/{id} — landing page
# ---------------------------------------------------------------------------


@router.get("/d/{link_id}", response_class=HTMLResponse)
async def download_page(
    link_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ots_dl: Optional[str] = Cookie(default=None, alias=DOWNLOAD_COOKIE_NAME),
):
    link = await _load_link(db, link_id)
    if not link or link.is_downloaded:
        raise HTTPException(404, "Link not found or already used")

    needs_password = bool(link.password_hash)
    unlocked = False
    if needs_password and ots_dl:
        unlocked = verify_download_token(ots_dl, link_id)

    footer_text = await _get_footer_text(db)
    current_user = get_current_user_optional(request)

    if needs_password and not unlocked:
        return templates.TemplateResponse(
            "password.html", {
                "request": request,
                "link_id": link_id,
                "footer_text": footer_text,
                "current_user": current_user,
            }
        )

    return templates.TemplateResponse(
        "download.html",
        {
            "request": request,
            "link_id": link_id,
            "files": [f.original_filename for f in link.files],
            "needs_password": needs_password,
            "total_size": _human_size(link.total_size_bytes),
            "total_size_bytes": link.total_size_bytes,  # raw integer for JS re-format
            "footer_text": footer_text,
            "current_user": current_user,
        },
    )


# ---------------------------------------------------------------------------
# POST /d/{id}/unlock — verify password, set signed cookie
# ---------------------------------------------------------------------------


@router.post("/d/{link_id}/unlock")
async def unlock(
    link_id: str,
    request: Request,
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    link = await _load_link(db, link_id)
    if not link or link.is_downloaded:
        raise HTTPException(404, "Link not found or already used")
    if not link.password_hash:
        # No password set — bounce to the download page
        return RedirectResponse(url=f"/d/{link_id}", status_code=303)

    footer_text = await _get_footer_text(db)
    current_user = get_current_user_optional(request)

    if not verify_password(link.password_hash, password):
        return templates.TemplateResponse(
            "password.html",
            {
                "request": request,
                "link_id": link_id,
                "error": True,
                "footer_text": footer_text,
                "current_user": current_user,
            },
            status_code=401,
        )

    token = issue_download_token(link_id)
    response = RedirectResponse(url=f"/d/{link_id}", status_code=303)
    response.set_cookie(
        key=_cookie_name(link_id),
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=False,  # set True behind TLS reverse proxy
        path="/",
    )
    return response


# ---------------------------------------------------------------------------
# GET /d/{id}/file — the streaming endpoint with burn-after-reading
# ---------------------------------------------------------------------------


@router.get("/d/{link_id}/file")
async def download_file(
    link_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ots_dl: Optional[str] = Cookie(default=None, alias=DOWNLOAD_COOKIE_NAME),
):
    link = await _load_link(db, link_id)
    if not link or link.is_downloaded:
        raise HTTPException(404, "Link not found or already used")

    # Auth gate: if the link has a password, the unlock cookie MUST be valid.
    if link.password_hash and not (
        ots_dl and verify_download_token(ots_dl, link_id)
    ):
        # Redirect to the password page rather than 401 — better UX.
        return RedirectResponse(url=f"/d/{link_id}", status_code=302)

    files: list[File] = list(link.files)
    if not files:
        raise HTTPException(404, "No files associated with this link")

    # --------------------------------------------------------------
    # Choose stream type and suggested filename
    # --------------------------------------------------------------
    if len(files) == 1:
        f = files[0]
        suggested_name = sanitize_filename(f.original_filename) or "download.bin"
        media_type = "application/octet-stream"
        source = stream_single_file(f.stored_filepath)
    else:
        suggested_name = f"onetimeshare_{link_id[:8]}.zip"
        media_type = "application/zip"
        source = stream_zip_files(
            (f.stored_filepath, sanitize_filename(f.original_filename) or f"file_{f.id}")
            for f in files
        )

    # RFC 5987 `filename*` for full Unicode support
    encoded = quote(suggested_name, safe="")
    content_disposition = f"attachment; filename=\"download.bin\"; filename*=UTF-8''{encoded}"

    # --------------------------------------------------------------
    # Burn-after-reading callback
    # --------------------------------------------------------------
    # NOTE: We do the cleanup in a FRESH session, not the request's `db`.
    # The request session can be closed or detached by the time the streaming
    # generator's `finally` block runs (FastAPI runs dependency cleanup
    # concurrently with response finalisation). A fresh session guarantees
    # the commit actually lands on disk.
    files_snapshot = list(files)  # capture for closure
    link_id_captured = link.id

    async def on_success() -> None:
        logger.info(
            "Burn-after-reading: deleting link %s (%d files)", link_id_captured, len(files_snapshot)
        )
        # 1) delete physical files first (idempotent; ignore missing)
        for f in files_snapshot:
            try:
                if f.stored_filepath and os.path.exists(f.stored_filepath):
                    os.remove(f.stored_filepath)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", f.stored_filepath, exc)
        # 2) mark link as consumed in a fresh session (cascade deletes File rows)
        from ..database import async_session_factory
        async with async_session_factory() as burn_db:
            link_row = await burn_db.get(Link, link_id_captured)
            if link_row is not None and not link_row.is_downloaded:
                link_row.is_downloaded = True
                await burn_db.commit()
                logger.info("Link %s marked as downloaded in DB", link_id_captured)

    wrapped = burn_after_stream(source, on_success)

    return StreamingResponse(
        wrapped,
        media_type=media_type,
        headers={
            "Content-Disposition": content_disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
