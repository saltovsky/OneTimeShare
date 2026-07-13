"""Upload routes: web page (GET /), form upload (POST /upload), JSON API (POST /api/upload).

  GET  /              -> if no session: shows the LOGIN FORM. If session valid:
                          shows the UPLOAD FORM.
  POST /upload        -> regular form submit: process upload, redirect to success page.
  GET  /upload/success -> success page showing the generated link.
  POST /api/upload    -> JSON API (for curl / programmatic clients).
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote

import aiofiles
from fastapi import APIRouter, Depends, File as FastFile, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import settings
from ..database import get_db
from ..models import File, Link, Setting
from ..security import (
    get_current_user_optional,
    hash_password,
    require_uploader_or_session,
    sanitize_filename,
    SessionUser,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)

CHUNK = settings.CHUNK_SIZE


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


async def _process_upload(
    files: List[UploadFile],
    password: Optional[str],
    db: AsyncSession,
    uploader: str,
) -> UploadResult:
    """Shared upload logic used by both form and JSON endpoints."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    pwd_hash = hash_password(password) if password else None

    link = Link(password_hash=pwd_hash, uploader_id=uploader)
    db.add(link)
    await db.flush()

    total_size = 0
    saved: List[dict] = []
    file_infos: List[dict] = []
    file_names: List[str] = []
    created_paths: List[str] = []

    try:
        for upload_file in files:
            original = sanitize_filename(upload_file.filename or "file")
            stored_name = f"{uuid.uuid4().hex}_{original}"
            stored_path = os.path.join(settings.UPLOAD_DIR, stored_name)

            real_upload_dir = os.path.realpath(settings.UPLOAD_DIR)
            real_stored = os.path.realpath(os.path.join(real_upload_dir, stored_name))
            if not real_stored.startswith(real_upload_dir + os.sep):
                raise HTTPException(400, "Invalid filename")

            size = 0
            try:
                async with aiofiles.open(stored_path, "wb") as out:
                    while True:
                        chunk = await upload_file.read(CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > settings.MAX_FILE_SIZE:
                            raise HTTPException(
                                status_code=413,
                                detail=f"File '{original}' exceeds per-file size limit",
                            )
                        if total_size + size > settings.MAX_TOTAL_SIZE:
                            raise HTTPException(
                                status_code=413,
                                detail="Total upload exceeds size limit",
                            )
                        await out.write(chunk)
            except HTTPException:
                if os.path.exists(stored_path):
                    try:
                        os.remove(stored_path)
                    except OSError:
                        pass
                raise
            except Exception:
                logger.exception("Unexpected error writing %s", stored_path)
                if os.path.exists(stored_path):
                    try:
                        os.remove(stored_path)
                    except OSError:
                        pass
                raise HTTPException(500, "Upload failed")
            finally:
                await upload_file.close()

            created_paths.append(stored_path)
            total_size += size
            file_names.append(original)
            file_infos.append({"name": original, "size": size})

            db.add(
                File(
                    link_id=link.id,
                    original_filename=original,
                    stored_filepath=stored_path,
                    size_bytes=size,
                )
            )
            saved.append({"name": original, "size": size})

        link.total_size_bytes = total_size
        await db.commit()

    except Exception:
        await db.rollback()
        for p in created_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        raise

    share_url = f"{settings.APP_URL}/d/{link.id}"
    logger.info(
        "Uploader=%s created link %s: %d file(s), %d bytes",
        uploader, link.id, len(saved), total_size,
    )

    return UploadResult(
        link_id=link.id,
        url=share_url,
        has_password=bool(pwd_hash),
        files_count=len(saved),
        total_size=total_size,
        file_names=file_names,
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
    files: List[UploadFile] = FastFile(..., description="One or more files to share"),
    password: Optional[str] = Form(default=None, max_length=256),
    db: AsyncSession = Depends(get_db),
    uploader: str = Depends(require_uploader_or_session),
):
    """Process the upload form and redirect to the success page."""
    try:
        result = await _process_upload(files, password, db, uploader)
    except HTTPException as exc:
        # Re-render the upload page with an error message
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "show_login": False,
                "uploader_user": uploader,
                "is_admin": True,  # doesn't matter for error display
                "current_user": get_current_user_optional(request),
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
    files: List[UploadFile] = FastFile(..., description="One or more files to share"),
    password: Optional[str] = Form(default=None, max_length=256),
    db: AsyncSession = Depends(get_db),
    uploader: str = Depends(require_uploader_or_session),
):
    """Accept uploaded files, persist them, return the generated share URL as JSON."""
    result = await _process_upload(files, password, db, uploader)

    return {
        "link_id": result.link_id,
        "url": result.url,
        "has_password": result.has_password,
        "files": result.file_infos,
        "total_size": result.total_size,
    }
