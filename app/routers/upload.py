"""Upload routes: web page (GET /), form upload (POST /upload), JSON API (POST /api/upload).

  GET  /              -> if no session: shows the LOGIN FORM. If session valid:
                          shows the UPLOAD FORM.
  POST /upload        -> regular form submit: process upload, redirect to success page.
  GET  /upload/success -> success page showing the generated link.
  POST /api/upload    -> JSON API (for curl / programmatic clients).

Upload bodies are consumed by `app.upload_stream`, which writes multipart file
parts straight into the upload directory. FastAPI's `UploadFile` is deliberately
not used: it spools every file to a temporary file first, and the handler then
has to copy it to permanent storage - that second full pass over a large file is
what makes the share link appear long after the browser reached 100%.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.requests import ClientDisconnect

from ..config import settings
from ..database import get_db
from ..models import File, Link, Setting
from ..security import (
    get_current_user_optional,
    hash_password,
    require_uploader_or_session,
    SessionUser,
)
from ..upload_stream import (
    MULTIPART_OVERHEAD_ALLOWANCE,
    MultipartError,
    UploadPayload,
    UploadTooLarge,
    read_multipart_upload,
    remove_files,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

# The only non-file form field we accept besides ``files`` is ``password``.
_MAX_PASSWORD_LENGTH = 256
_MAX_FORM_FIELDS = 16


@dataclass
class UploadResult:
    link_id: str
    url: str
    has_password: bool
    files_count: int
    total_size: int
    file_names: List[str]
    file_infos: List[dict]  # [{"name": str, "size": int}, ...]


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def _reject_oversized_body(request: Request) -> None:
    """Fail fast on obviously oversized bodies instead of streaming them in."""
    raw = request.headers.get("content-length")
    if not raw:
        return  # chunked upload: limits are still enforced while streaming
    try:
        length = int(raw)
    except ValueError:
        raise HTTPException(400, "Invalid Content-Length header") from None
    if length > settings.MAX_TOTAL_SIZE + MULTIPART_OVERHEAD_ALLOWANCE:
        raise HTTPException(413, "Total upload exceeds size limit")


async def _discard_hash_task(task: Optional["asyncio.Task[str]"]) -> None:
    """Cancel/consume the Argon2 task so a failed upload leaves nothing running."""
    if task is None:
        return
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


async def _receive_upload(request: Request) -> tuple[UploadPayload, Optional["asyncio.Task[str]"]]:
    """Read the request body and persist its parts; start hashing the password early."""
    password_task: Optional["asyncio.Task[str]"] = None
    password_seen = False

    def _on_field(name: str, value: str) -> None:
        nonlocal password_task, password_seen
        if name != "password" or not value:
            return
        if password_seen:
            raise MultipartError("Duplicate password field")
        if len(value) > _MAX_PASSWORD_LENGTH:
            raise MultipartError(
                f"Password exceeds {_MAX_PASSWORD_LENGTH} characters"
            )
        password_seen = True
        # Argon2id is intentionally CPU/memory heavy. Start it while the
        # remaining body parts are still arriving instead of adding ~100ms to
        # the very end of the upload.
        password_task = asyncio.create_task(asyncio.to_thread(hash_password, value))

    try:
        payload = await read_multipart_upload(
            request,
            upload_dir=str(settings.UPLOAD_DIR),
            max_file_size=settings.MAX_FILE_SIZE,
            max_total_size=settings.MAX_TOTAL_SIZE,
            max_files=settings.MAX_FILES_PER_UPLOAD,
            max_fields=_MAX_FORM_FIELDS,
            buffer_size=settings.UPLOAD_CHUNK_SIZE,
            on_field=_on_field,
        )
    except UploadTooLarge as exc:
        await _discard_hash_task(password_task)
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except MultipartError as exc:
        await _discard_hash_task(password_task)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ClientDisconnect as exc:
        await _discard_hash_task(password_task)
        raise HTTPException(status_code=400, detail="Upload interrupted") from exc
    except OSError as exc:
        await _discard_hash_task(password_task)
        logger.exception("Storage error while receiving an upload")
        raise HTTPException(
            status_code=507, detail="Server storage is full or unavailable"
        ) from exc
    except Exception:
        await _discard_hash_task(password_task)
        raise
    return payload, password_task


async def _process_upload(
    request: Request,
    db: AsyncSession,
    uploader: str,
) -> UploadResult:
    """Shared upload logic used by both form and JSON endpoints."""
    received_at = time.perf_counter()
    _reject_oversized_body(request)

    payload, password_task = await _receive_upload(request)
    received_seconds = time.perf_counter() - received_at

    if not payload.files:
        await _discard_hash_task(password_task)
        raise HTTPException(status_code=400, detail="No files provided")

    file_infos = [{"name": part.original_filename, "size": part.size} for part in payload.files]

    link = Link(uploader_id=uploader)
    db.add(link)
    try:
        await db.flush()
        for part in payload.files:
            db.add(
                File(
                    link_id=link.id,
                    original_filename=part.original_filename,
                    stored_filepath=part.stored_path,
                    size_bytes=part.size,
                )
            )
        if password_task is not None:
            link.password_hash = await password_task
        link.total_size_bytes = payload.total_size
        await db.commit()
    except Exception:
        await _discard_hash_task(password_task)
        await db.rollback()
        remove_files(payload.files)
        raise

    share_url = f"{settings.APP_URL}/d/{link.id}"
    logger.info(
        "Uploader=%s created link %s: %d file(s), %d bytes "
        "(receive=%.2fs, persist=%.2fs)",
        uploader,
        link.id,
        len(payload.files),
        payload.total_size,
        received_seconds,
        time.perf_counter() - received_at - received_seconds,
    )

    return UploadResult(
        link_id=link.id,
        url=share_url,
        has_password=password_task is not None,
        files_count=len(payload.files),
        total_size=payload.total_size,
        file_names=[part.original_filename for part in payload.files],
        file_infos=file_infos,
    )


# ---------------------------------------------------------------------------
# GET / — main page (login or upload form)
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
) -> HTMLResponse:
    """Render the upload form, or the login form if not signed in."""
    result = await db.execute(select(Setting).where(Setting.key == "footer_text"))
    setting = result.scalar_one_or_none()
    footer_text = setting.value if setting else None

    if user is None or not user.can_upload:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "show_login": True,
                "next": request.query_params.get("next", "/"),
                "error_code": request.query_params.get("error", ""),
                "current_user": None,
                "footer_text": footer_text,
            },
            status_code=200,
        )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "show_login": False,
            "uploader_user": user.username,
            "is_admin": user.is_admin,
            "current_user": user,
            "footer_text": footer_text,
        },
    )


# ---------------------------------------------------------------------------
# POST /upload — form submit (redirects to success page)
# ---------------------------------------------------------------------------
@router.post("/upload")
async def upload_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    uploader: str = Depends(require_uploader_or_session),
):
    """Process the upload form and redirect to the success page."""
    try:
        result = await _process_upload(request, db, uploader)
    except HTTPException as exc:
        # Re-render the upload page with an error message
        user = get_current_user_optional(request)
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "show_login": False,
                "uploader_user": uploader,
                "is_admin": user.is_admin if user is not None else False,
                "current_user": user,
                "footer_text": None,
                "upload_error": exc.detail,
            },
            status_code=exc.status_code if exc.status_code < 500 else 500,
        )

    # Encode link data in query params for the success page
    params = (
        f"link_id={quote(result.link_id)}"
        f"&url={quote(result.url, safe='')}"
        f"&files_count={result.files_count}"
        f"&total_size={result.total_size}"
        f"&has_password={1 if result.has_password else 0}"
    )
    return RedirectResponse(url=f"/upload/success?{params}", status_code=303)


# ---------------------------------------------------------------------------
# GET /upload/success — success page
# ---------------------------------------------------------------------------
@router.get("/upload/success", response_class=HTMLResponse)
async def upload_success(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
):
    """Show the success page with the generated share link."""
    if user is None:
        return RedirectResponse(url="/", status_code=303)

    result = await db.execute(select(Setting).where(Setting.key == "footer_text"))
    setting = result.scalar_one_or_none()
    footer_text = setting.value if setting else None

    link_id = request.query_params.get("link_id", "")
    share_url = request.query_params.get("url", "")
    files_count = request.query_params.get("files_count", "0")
    total_size = request.query_params.get("total_size", "0")
    has_password = request.query_params.get("has_password", "0") == "1"

    # Fetch file names from DB if the link exists
    file_names: List[str] = []
    if link_id:
        link_result = await db.execute(
            select(Link).options(selectinload(Link.files)).where(Link.id == link_id)
        )
        link = link_result.scalar_one_or_none()
        if link:
            file_names = [f.original_filename for f in link.files]
            if not share_url:
                share_url = f"{settings.APP_URL}/d/{link_id}"
            if total_size == "0":
                total_size = str(link.total_size_bytes)

    return templates.TemplateResponse(
        "success.html",
        {
            "request": request,
            "current_user": user,
            "footer_text": footer_text,
            "share_url": share_url,
            "files_count": int(files_count),
            "total_size_bytes": int(total_size),
            "total_size": _human_size(int(total_size or 0)),
            "has_password": has_password,
            "file_names": file_names,
        },
    )


# ---------------------------------------------------------------------------
# POST /api/upload — JSON API (curl / programmatic clients)
# ---------------------------------------------------------------------------
@router.post("/api/upload")
async def upload_json(
    request: Request,
    db: AsyncSession = Depends(get_db),
    uploader: str = Depends(require_uploader_or_session),
):
    """Accept uploaded files, persist them, return the generated share URL as JSON."""
    result = await _process_upload(request, db, uploader)

    return {
        "link_id": result.link_id,
        "url": result.url,
        "has_password": result.has_password,
        "files": result.file_infos,
        "total_size": result.total_size,
    }
