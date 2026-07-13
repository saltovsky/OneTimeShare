"""Authentication routes: login (POST) and logout (GET).

The login form lives on the main page (GET /) — this router only handles
the form submission and session teardown.
"""
from __future__ import annotations

import logging
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..security import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    authenticate_user,
    get_session_user,
    issue_session_token,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _safe_next(next_url: str) -> str:
    """Allow only same-site, path-only redirects to prevent open-redirect."""
    if not next_url:
        return "/"
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return "/"
    if not next_url.startswith("/"):
        return "/"
    return next_url


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(..., min_length=1, max_length=128),
    password: str = Form(..., min_length=1, max_length=256),
    next: str = Form(default="/"),
    db: AsyncSession = Depends(get_db),
):
    """Verify credentials against the database, set a signed session cookie, redirect."""
    next_url = _safe_next(next)

    session_user = await authenticate_user(db, username, password)

    if session_user is None:
        logger.info(
            "Failed login attempt for user=%r from %s",
            username,
            request.client.host if request.client else "?",
        )
        return RedirectResponse(
            url=f"/?error=invalid&next={quote(next_url, safe='')}",
            status_code=303,
        )

    # If the requested page requires admin and the user isn't admin, refuse.
    if next_url.startswith("/admin") and not session_user.is_admin:
        return RedirectResponse(
            url=f"/?error=forbidden&next={quote(next_url, safe='')}",
            status_code=303,
        )

    token = issue_session_token(session_user.username, session_user.role)
    logger.info(
        "Login OK: user=%r role=%s from %s",
        session_user.username,
        session_user.role,
        request.client.host if request.client else "?",
    )

    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # flip to True when serving strictly over HTTPS
        path="/",
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    """Clear the session cookie and redirect home."""
    user = get_session_user(request)
    if user is not None:
        logger.info("Logout: user=%r role=%s", user.username, user.role)
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
