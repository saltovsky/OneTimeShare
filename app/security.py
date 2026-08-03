"""Security helpers: password hashing, authentication, cookie signing, filename sanitisation.

Authentication model
====================

Users are stored in the `users` database table with roles:
  * **admin**    — full access to admin panel, sees all links, can manage users.
  * **uploader** — can create share links, sees only own links in admin panel.

Access channels:

  * **Web UI** (HTML pages) — uses a **signed session cookie** (`ots_session`)
    set by `POST /login`. Pages call `require_uploader_session` /
    `require_admin_session` which simply read the cookie.

  * **HTTP API** (`POST /api/upload`, `POST /admin/{id}/revoke`) — accepts
    EITHER the same session cookie (so the in-page upload form works without
    re-prompting) OR HTTP Basic Auth (so `curl -u user:pass ...` works).
    Dependencies: `require_uploader_or_session` / `require_admin_or_session`.

Initial admin and uploader credentials are seeded from env vars on first boot
(ADMIN_USERNAME/ADMIN_PASSWORD, UPLOADER_USERNAME/UPLOADER_PASSWORD).
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing — Argon2id (OWASP recommendation, 2024+).
# Used for BOTH user login passwords AND per-link download passwords.
# ---------------------------------------------------------------------------
_password_hasher = PasswordHasher()  # sane Argon2id defaults


def hash_password(plain: str) -> str:
    """Hash a password with Argon2id. Returns the encoded hash string."""
    return _password_hasher.hash(plain)


def verify_password(password_hash: str, plain: str) -> bool:
    """Constant-time-ish verify. Returns False on any failure (no exception leaks)."""
    try:
        _password_hasher.verify(password_hash, plain)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# Credential matching — DB first, then LDAP with auto-provisioning.
# Used by both session login and HTTP Basic.
# ---------------------------------------------------------------------------
async def authenticate_user(db: AsyncSession, username: str, password: str) -> Optional["SessionUser"]:
    """Authenticate via local DB first, then LDAP.

    - Locally-created users (admin, uploader) are validated against their
      Argon2 hash in the ``users`` table.
    - LDAP users are always validated against the directory.  On first
      login they are auto-created with role ``uploader`` and a random
      placeholder hash.
    - LDAP users can log in with or without ``@domain`` — the
      *canonical* login (value of ``login_attr``, e.g. ``sAMAccountName``)
      is used for the local database record, so the same LDAP principal
      always maps to a single local user.
    """
    from .models import User
    from .ldap_utils import authenticate_ldap_user, get_ldap_config

    # 1) Check local DB with the raw username.  This catches locally-
    #    created accounts (admin, uploader from Admin UI) immediately.
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if user is not None:
        if verify_password(user.password_hash, password):
            return SessionUser(username=user.username, role=user.role)
        # Password mismatch — don't bail out: this could be an LDAP user
        # with a placeholder hash.  Fall through to LDAP below.

    # 2) Try LDAP (if configured and enabled)
    ldap_config = await get_ldap_config(db)
    if not ldap_config.is_enabled:
        return None

    ldap_result = await authenticate_ldap_user(ldap_config, username, password)
    if ldap_result is None:
        return None

    # 3) Resolve the canonical login from LDAP attributes.
    #    This ensures that ``jdoe`` and ``jdoe@contoso.com`` map to the
    #    same local user record.
    canonical = ldap_result["canonical_username"]

    # 4) Look up or auto-create the canonical user in the local database.
    if user is not None and user.username == canonical:
        # Already looked up — either a pre-existing LDAP user or
        # the same username was used for both DB and canonical.
        pass
    else:
        result = await db.execute(select(User).where(User.username == canonical))
        user = result.scalar_one_or_none()

    if user is None:
        try:
            user = User(
                username=canonical,
                password_hash=hash_password(_generate_placeholder_password()),
                role="uploader",
            )
            db.add(user)
            await db.commit()
            logger.info(
                "Auto-created LDAP user %s (fullname: %s, email: %s)",
                canonical,
                ldap_result.get("fullname", ""),
                ldap_result.get("email", ""),
            )
        except Exception:
            await db.rollback()
            result = await db.execute(select(User).where(User.username == canonical))
            user = result.scalar_one_or_none()
            if user is None:
                logger.error("Failed to auto-create LDAP user %s", canonical)
                return None

    return SessionUser(username=user.username, role=user.role)


def _generate_placeholder_password() -> str:
    """Generate a cryptographically random placeholder password for LDAP users."""
    import secrets as _secrets
    return _secrets.token_urlsafe(48)


# ---------------------------------------------------------------------------
# HTTP Basic security (FastAPI dependency). `auto_error=False` so the
# dependency can also accept a session cookie and only fall back to Basic.
# ---------------------------------------------------------------------------
_basic_security = HTTPBasic(auto_error=False)


# ---------------------------------------------------------------------------
# Session cookies — signed with HMAC, short-lived, httponly.
# Payload: {"u": username, "r": "uploader" | "admin"}.
# ---------------------------------------------------------------------------
_session_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="session-v1")
SESSION_COOKIE_NAME = "ots_session"
SESSION_MAX_AGE = 8 * 3600  # 8 hours


@dataclass(frozen=True)
class SessionUser:
    username: str
    role: str  # "uploader" or "admin"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_upload(self) -> bool:
        # Both uploader and admin roles can create links
        return self.role in ("uploader", "admin")


def issue_session_token(username: str, role: str) -> str:
    return _session_serializer.dumps({"u": username, "r": role})


def parse_session_token(token: str) -> Optional[SessionUser]:
    try:
        data = _session_serializer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    u = data.get("u")
    r = data.get("r")
    if not isinstance(u, str) or r not in ("uploader", "admin"):
        return None
    return SessionUser(username=u, role=r)


def get_session_user(request: Request) -> Optional[SessionUser]:
    """Read session cookie from a request, return user or None. No exceptions."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return parse_session_token(token)


# ---------------------------------------------------------------------------
# Hybrid dependencies for API endpoints: session cookie OR HTTP Basic.
# Session is checked first (it's what the in-page upload form uses).
# ---------------------------------------------------------------------------
async def _resolve_via_session_or_basic(
    request: Request,
    db: AsyncSession,
    credentials: Optional[HTTPBasicCredentials],
    require_role: str,  # "uploader" or "admin"
) -> str:
    """Shared auth resolver. `require_role` is the MINIMUM required role:
    'uploader' means uploader OR admin can pass; 'admin' means admin only."""

    def _role_ok(user_role: str) -> bool:
        if require_role == "uploader":
            return user_role in ("uploader", "admin")
        if require_role == "admin":
            return user_role == "admin"
        return False

    # 1) Session cookie (used by the web UI)
    user = get_session_user(request)
    if user is not None and _role_ok(user.role):
        return user.username

    # 2) HTTP Basic (used by curl / scripts) — verify against DB
    if credentials is not None:
        db_user = await authenticate_user(db, credentials.username, credentials.password)
        if db_user is not None and _role_ok(db_user.role):
            return db_user.username

    # 3) Reject — API must return 401, never redirect
    realm = "OneTimeShare Admin" if require_role == "admin" else "OneTimeShare Upload"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Authentication required ({require_role})",
        headers={"WWW-Authenticate": f'Basic realm="{realm}"'},
    )


async def require_uploader_or_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
    credentials: Optional[HTTPBasicCredentials] = Depends(_basic_security),
) -> str:
    """For `/api/upload` — accepts uploader or admin role via session OR Basic."""
    return await _resolve_via_session_or_basic(request, db, credentials, "uploader")


async def require_admin_or_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
    credentials: Optional[HTTPBasicCredentials] = Depends(_basic_security),
) -> str:
    """For `/admin/{id}/revoke` — accepts admin role only via session OR Basic."""
    return await _resolve_via_session_or_basic(request, db, credentials, "admin")


# ---------------------------------------------------------------------------
# Page-level dependencies (session only, no HTTP Basic on HTML routes).
# The handler is responsible for rendering the login form when the user
# is None; we just provide a clean way to read the current session.
# ---------------------------------------------------------------------------
def get_current_user_optional(request: Request) -> Optional[SessionUser]:
    return get_session_user(request)


# ---------------------------------------------------------------------------
# Download-cookie (separate, unrelated to login sessions) — set after
# password-unlock on a per-link basis, verified at stream time.
# ---------------------------------------------------------------------------
_download_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="dl-auth-v1")
DOWNLOAD_COOKIE_NAME = "ots_dl_auth"
COOKIE_MAX_AGE = 3600  # seconds


def issue_download_token(link_id: str) -> str:
    return _download_serializer.dumps({"link_id": link_id})


def verify_download_token(token: str, expected_link_id: str) -> bool:
    try:
        data = _download_serializer.loads(token, max_age=COOKIE_MAX_AGE)
    except BadSignature:
        return False
    return data.get("link_id") == expected_link_id


# ---------------------------------------------------------------------------
# Filename sanitisation — defence-in-depth against path traversal and RCE.
# 1. Null-byte / directory separator stripping before the regex.
# 2. Unicode-aware whitelist: \w (letters + digits from all scripts) plus
#    dot, hyphen, space. Everything else -> underscore.
#    Control chars (U+0000–U+001F, U+007F) and path separators are NOT
#    word chars and are replaced.
# 3. Leading/trailing dots stripped (no hidden files).
# 4. Length capped to 200 chars (Linux ext4 limit).
# ---------------------------------------------------------------------------
_SAFE_FILENAME_RE = re.compile(r"[^\w.\- ]", re.UNICODE)


def sanitize_filename(name: Optional[str]) -> str:
    if not name:
        return "file"
    # `os.path.basename` handles both POSIX and Windows separators, null bytes, etc.
    name = name.split("\x00", 1)[0]
    name = name.replace("\\", "/").split("/")[-1]
    name = _SAFE_FILENAME_RE.sub("_", name)
    name = name.strip().strip(".")
    if not name:
        return "file"
    # Truncate to fit filesystem limits while preserving extension when possible
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) < 20:
            name = stem[: 200 - len(ext) - 1] + "." + ext
        else:
            name = name[:200]
    return name
