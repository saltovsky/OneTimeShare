"""Admin routes: list pending links, revoke them, manage users and settings.

The admin page requires a valid session. Admins see all links; uploaders
see only their own links. Only admins can manage users and settings.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..database import get_db
from ..models import Link, Setting, User
from ..security import (
    get_current_user_optional,
    hash_password,
    require_admin_or_session,
    SessionUser,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


# ---------------------------------------------------------------------------
# Shared helper: fetch common admin data (links, users, footer_text, stats)
# ---------------------------------------------------------------------------
async def _admin_context(
    db: AsyncSession,
    user: SessionUser,
) -> dict:
    """Build the template context shared across all admin tabs."""

    footer_text: Optional[str] = None
    result = await db.execute(select(Setting).where(Setting.key == "footer_text"))
    setting = result.scalar_one_or_none()
    if setting:
        footer_text = setting.value

    # Links — filtered by uploader for non-admins
    query = (
        select(Link)
        .options(selectinload(Link.files))
        .where(Link.is_downloaded.is_(False))
    )
    if not user.is_admin:
        query = query.where(Link.uploader_id == user.username)

    query = query.order_by(Link.created_at.desc())
    result = await db.execute(query)
    links = result.scalars().all()

    # Users (only for admins)
    users: list[User] = []
    if user.is_admin:
        users_result = await db.execute(select(User).order_by(User.created_at.asc()))
        users = users_result.scalars().all()

    return {
        "links": links,
        "admin_user": user.username,
        "current_user": user,
        "human_size": _human_size,
        "users": users,
        "footer_text": footer_text,
    }


def _require_auth(user: Optional[SessionUser], redirect_path: str = "/admin") -> RedirectResponse | None:
    """If user is missing, redirect to login. Returns None if authenticated."""
    if user is None:
        return RedirectResponse(url=f"/?next={redirect_path}", status_code=303)
    return None


def _require_admin(user: Optional[SessionUser], redirect_path: str = "/admin") -> RedirectResponse | None:
    """If user is not admin, redirect to admin panel (uploaders can't see users/settings)."""
    if user is None:
        return RedirectResponse(url=f"/?next={redirect_path}", status_code=303)
    if not user.is_admin:
        return RedirectResponse(url="/admin", status_code=303)
    return None


# ---------------------------------------------------------------------------
# GET /admin            — Links panel (default tab)
# GET /admin/users      — User management tab (admin only)
# GET /admin/settings   — Settings tab (admin only)
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
async def admin_panel(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
):
    """List pending links. Admins see all; uploaders see only their own."""
    redirect = _require_auth(user, "/admin")
    if redirect:
        return redirect

    ctx = await _admin_context(db, user)
    ctx["request"] = request
    ctx["tab"] = "panel"
    return templates.TemplateResponse("admin.html", ctx)


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
):
    """User management — admin only."""
    redirect = _require_admin(user, "/admin/users")
    if redirect:
        return redirect

    ctx = await _admin_context(db, user)
    ctx["request"] = request
    ctx["tab"] = "users"
    return templates.TemplateResponse("admin.html", ctx)


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
):
    """Application settings — admin only."""
    redirect = _require_admin(user, "/admin/settings")
    if redirect:
        return redirect

    ctx = await _admin_context(db, user)
    ctx["request"] = request
    ctx["tab"] = "settings"
    return templates.TemplateResponse("admin.html", ctx)


# ---------------------------------------------------------------------------
# Revoke link — POST /admin/{link_id}/revoke
# ---------------------------------------------------------------------------
@router.post("/admin/{link_id}/revoke")
async def revoke_link(
    link_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth_user: str = Depends(require_admin_or_session),
):
    """Manually delete a pending link and its files."""
    result = await db.execute(
        select(Link).options(selectinload(Link.files)).where(Link.id == link_id)
    )
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(404, "Link not found")

    # Uploader can only revoke their own links; admin can revoke any
    session_user = get_current_user_optional(request)
    if session_user and not session_user.is_admin and link.uploader_id != session_user.username:
        raise HTTPException(403, "You can only revoke your own links")

    for f in link.files:
        try:
            if f.stored_filepath and os.path.exists(f.stored_filepath):
                os.remove(f.stored_filepath)
        except OSError as exc:
            logger.warning("Failed to remove %s: %s", f.stored_filepath, exc)

    link.is_downloaded = True
    await db.commit()

    logger.info("User %s revoked link %s", auth_user, link_id)
    return RedirectResponse(url="/admin", status_code=303)


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------
@router.post("/admin/users")
async def add_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
    username: str = Form(..., min_length=1, max_length=128),
    password: str = Form(..., min_length=1, max_length=256),
    role: str = Form(..., min_length=1, max_length=32),
):
    """Add a new user. Admin only."""
    if user is None or not user.is_admin:
        raise HTTPException(403, "Only admins can manage users")

    if role not in ("admin", "uploader"):
        return RedirectResponse(url="/admin/users?error=invalid_role", status_code=303)

    # Check if username already exists
    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none() is not None:
        return RedirectResponse(url="/admin/users?error=user_exists", status_code=303)

    new_user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        created_at=datetime.utcnow(),
    )
    db.add(new_user)
    await db.commit()

    logger.info("Admin %s created user %s with role %s", user.username, username, role)
    return RedirectResponse(url="/admin/users?saved=user", status_code=303)


@router.post("/admin/users/{target_username}/delete")
async def delete_user(
    target_username: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
):
    """Delete a user. Admin only. Cannot delete yourself."""
    if user is None or not user.is_admin:
        raise HTTPException(403, "Only admins can manage users")

    if target_username == user.username:
        return RedirectResponse(url="/admin/users?error=cannot_delete_self", status_code=303)

    result = await db.execute(select(User).where(User.username == target_username))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User not found")

    await db.delete(target)
    await db.commit()

    logger.info("Admin %s deleted user %s", user.username, target_username)
    return RedirectResponse(url="/admin/users?saved=deleted", status_code=303)


# ---------------------------------------------------------------------------
# Settings management (admin only)
# ---------------------------------------------------------------------------
@router.post("/admin/settings/footer")
async def update_footer_text(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
    footer_text: str = Form(..., max_length=1024),
):
    """Update the footer text shown on public download/upload pages. Admin only."""
    if user is None or not user.is_admin:
        raise HTTPException(403, "Only admins can change settings")

    result = await db.execute(select(Setting).where(Setting.key == "footer_text"))
    setting = result.scalar_one_or_none()

    if setting is None:
        setting = Setting(key="footer_text", value=footer_text)
        db.add(setting)
    else:
        setting.value = footer_text

    await db.commit()
    logger.info("Admin %s updated footer_text to: %s", user.username, footer_text)
    return RedirectResponse(url="/admin/settings?saved=footer", status_code=303)
