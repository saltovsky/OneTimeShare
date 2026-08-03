"""Admin routes: list pending links, revoke them, manage users and settings.

The admin page requires a valid session. Admins see all links; uploaders
see only their own links. Only admins can manage users, settings, and LDAP.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..database import get_db
from ..ldap_utils import (
    LDAPConfig,
    get_ldap_config,
    lookup_ldap_user,
    save_ldap_config,
    test_ldap_connection,
)
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

    # LDAP config (admin only)
    ldap_config: Optional[LDAPConfig] = None
    ldap_config_dict: Optional[dict] = None
    if user.is_admin:
        ldap_config = await get_ldap_config(db)
        ldap_config_dict = ldap_config.to_dict(redact_sensitive=True)

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
        "ldap_config": ldap_config_dict,
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


# ---------------------------------------------------------------------------
# LDAP settings (admin only)
# ---------------------------------------------------------------------------
@router.get("/admin/ldap", response_class=HTMLResponse)
async def admin_ldap_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
):
    """LDAP configuration page — admin only."""
    redirect = _require_admin(user, "/admin/ldap")
    if redirect:
        return redirect

    ctx = await _admin_context(db, user)
    ctx["request"] = request
    ctx["tab"] = "ldap"
    return templates.TemplateResponse("admin.html", ctx)


@router.post("/admin/ldap")
async def save_ldap_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
    ldap_enabled: str = Form(default="off"),
    ldap_host: str = Form(default="", max_length=256),
    ldap_port: int = Form(default=389),
    ldap_protocol: str = Form(default="ldap"),
    ldap_base_dn: str = Form(default="", max_length=512),
    ldap_search_filter: str = Form(default="", max_length=512),
    ldap_bind_dn: str = Form(default="", max_length=512),
    ldap_bind_password: str = Form(default="", max_length=256),
    ldap_login_attr: str = Form(default="sAMAccountName", max_length=128),
    ldap_fullname_attr: str = Form(default="displayName", max_length=128),
    ldap_email_attr: str = Form(default="mail", max_length=128),
    ldap_timeout: int = Form(default=5),
):
    """Save LDAP configuration. Admin only."""
    if user is None or not user.is_admin:
        raise HTTPException(403, "Only admins can change LDAP settings")

    # Read existing config to preserve password if field was left blank
    existing = await get_ldap_config(db)
    bind_password = ldap_bind_password if ldap_bind_password else existing.bind_password

    config = LDAPConfig(
        enabled=(ldap_enabled.strip().lower() in ("on", "1", "true", "yes")),
        host=ldap_host.strip(),
        port=max(1, min(ldap_port, 65535)),
        protocol=ldap_protocol.strip() if ldap_protocol.strip() in ("ldap", "ldaps") else "ldap",
        base_dn=ldap_base_dn.strip(),
        search_filter=ldap_search_filter.strip(),
        bind_dn=ldap_bind_dn.strip(),
        bind_password=bind_password,
        login_attr=ldap_login_attr.strip(),
        fullname_attr=ldap_fullname_attr.strip(),
        email_attr=ldap_email_attr.strip(),
        timeout=max(1, min(ldap_timeout, 60)),
    )

    await save_ldap_config(db, config)
    await db.commit()

    logger.info("Admin %s updated LDAP configuration", user.username)
    return RedirectResponse(url="/admin/ldap?saved=ldap", status_code=303)


@router.post("/admin/ldap/test")
async def test_ldap_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
):
    """Test LDAP connection using saved settings. Admin only. Returns JSON."""
    if user is None or not user.is_admin:
        raise HTTPException(403, "Only admins can test LDAP connection")

    config = await get_ldap_config(db)
    if not config.is_enabled:
        return JSONResponse(
            {"success": False, "error": "LDAP is not configured (host and base DN required)"},
            status_code=400,
        )

    result = await test_ldap_connection(config)
    return JSONResponse(result)


@router.post("/admin/ldap/lookup")
async def lookup_ldap_user_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: Optional[SessionUser] = Depends(get_current_user_optional),
    ldap_lookup_username: str = Form(..., min_length=1, max_length=128),
):
    """Look up a user in LDAP by login. Admin only. Returns JSON."""
    if user is None or not user.is_admin:
        raise HTTPException(403, "Only admins can perform LDAP lookups")

    config = await get_ldap_config(db)
    if not config.is_enabled:
        return JSONResponse(
            {"found": False, "error": "LDAP is not configured (host and base DN required)"},
            status_code=400,
        )

    result = await lookup_ldap_user(config, ldap_lookup_username.strip())
    return JSONResponse(result)
