"""LDAP integration: configuration persistence, authentication, and connection testing.

LDAP settings are stored as a JSON blob under the `ldap_config` key in the
`settings` table.  When LDAP is disabled (all fields empty) the login flow
falls back to the local `users` database table transparently.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import ldap3
from ldap3 import ALL_ATTRIBUTES as LDAP_ALL_ATTRS
from ldap3.core.exceptions import LDAPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Setting

logger = logging.getLogger(__name__)

SETTING_KEY = "ldap_config"

# Sensitive fields redacted in API responses / UI
SENSITIVE_FIELDS = {"bind_password"}

# ---------------------------------------------------------------------------
# LDAP configuration data class
# ---------------------------------------------------------------------------
@dataclass
class LDAPConfig:
    enabled: bool = False  # master on/off toggle
    host: str = ""
    port: int = 389
    protocol: str = "ldap"  # "ldap" | "ldaps"
    base_dn: str = ""
    search_filter: str = ""
    bind_dn: str = ""
    bind_password: str = ""
    login_attr: str = "sAMAccountName"
    fullname_attr: str = "displayName"
    email_attr: str = "mail"
    timeout: int = 5

    @property
    def is_enabled(self) -> bool:
        """LDAP is considered enabled when the master toggle is ON and
        the minimum connection parameters (host + base_dn) are set."""
        return self.enabled and bool(self.host and self.base_dn)

    @property
    def server_uri(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"

    def to_dict(self, redact_sensitive: bool = False) -> Dict[str, Any]:
        data = {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "protocol": self.protocol,
            "base_dn": self.base_dn,
            "search_filter": self.search_filter,
            "bind_dn": self.bind_dn,
            "bind_password": "********" if redact_sensitive and self.bind_password else self.bind_password,
            "login_attr": self.login_attr,
            "fullname_attr": self.fullname_attr,
            "email_attr": self.email_attr,
            "timeout": self.timeout,
        }
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LDAPConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            host=str(data.get("host", "")),
            port=int(data.get("port", 389)),
            protocol=str(data.get("protocol", "ldap")),
            base_dn=str(data.get("base_dn", "")),
            search_filter=str(data.get("search_filter", "")),
            bind_dn=str(data.get("bind_dn", "")),
            bind_password=str(data.get("bind_password", "")),
            login_attr=str(data.get("login_attr", "sAMAccountName")),
            fullname_attr=str(data.get("fullname_attr", "displayName")),
            email_attr=str(data.get("email_attr", "mail")),
            timeout=int(data.get("timeout", 5)),
        )


# ---------------------------------------------------------------------------
# DB persistence helpers
# ---------------------------------------------------------------------------
async def get_ldap_config(db: AsyncSession) -> LDAPConfig:
    """Load LDAP configuration from the database. Returns defaults if unset."""
    result = await db.execute(select(Setting).where(Setting.key == SETTING_KEY))
    row = result.scalar_one_or_none()
    if row is None or not row.value:
        return LDAPConfig()
    try:
        return LDAPConfig.from_dict(json.loads(row.value))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Corrupted LDAP config, using defaults: %s", exc)
        return LDAPConfig()


async def save_ldap_config(db: AsyncSession, config: LDAPConfig) -> None:
    """Persist LDAP configuration as a JSON blob in the settings table."""
    result = await db.execute(select(Setting).where(Setting.key == SETTING_KEY))
    row = result.scalar_one_or_none()
    payload = json.dumps(config.to_dict(), ensure_ascii=False)

    if row is None:
        db.add(Setting(key=SETTING_KEY, value=payload))
    else:
        row.value = payload
    await db.flush()


# ---------------------------------------------------------------------------
# LDAP server helper
# ---------------------------------------------------------------------------
def _make_server(config: LDAPConfig) -> ldap3.Server:
    """Build an ldap3 Server object from config."""
    return ldap3.Server(
        config.server_uri,
        get_info=ldap3.NONE,
        connect_timeout=config.timeout,
    )


def _resolve_search_attr(config: LDAPConfig, username: str) -> str:
    """Pick the LDAP attribute to search by.

    If the username contains ``@`` (UPN-style login, e.g.
    ``jdoe@contoso.com``), ``userPrincipalName`` is preferred.
    Otherwise the configured ``login_attr`` (default ``sAMAccountName``)
    is used.
    """
    if "@" in username:
        return "userPrincipalName"
    return config.login_attr


def _build_search_filter(config: LDAPConfig, username: str) -> str:
    """Build the LDAP search filter string.

    If a custom `search_filter` is configured, the literal `{username}`
    placeholder is replaced with the sanitised value.  Otherwise the
    default filter `(<attr>={username})` is used, where ``<attr>`` is
    auto-detected (``userPrincipalName`` for ``user@domain`` inputs,
    ``login_attr`` otherwise).
    """
    safe_username = _escape_ldap_filter(username)
    if config.search_filter and "{username}" in config.search_filter:
        return config.search_filter.replace("{username}", safe_username)
    attr = _resolve_search_attr(config, username)
    return f"({attr}={safe_username})"


def _escape_ldap_filter(value: str) -> str:
    """Escape special LDAP filter characters per RFC 4515."""
    mapping = {
        "\\": "\\5c",
        "*": "\\2a",
        "(": "\\28",
        ")": "\\29",
        "\x00": "\\00",
    }
    for char, replacement in mapping.items():
        value = value.replace(char, replacement)
    return value


# ---------------------------------------------------------------------------
# Core LDAP operations
# ---------------------------------------------------------------------------
async def authenticate_ldap_user(
    config: LDAPConfig,
    username: str,
    password: str,
) -> Optional[Dict[str, Any]]:
    """Authenticate a user against LDAP and return their attributes.

    Returns a dict with keys ``username``, ``fullname``, ``email``, ``dn``
    on success, or ``None`` on failure.  This function is deliberately
    synchronous-IO but wrapped in asyncio-friendly patterns — ldap3 does not
    have an async interface, so we rely on the default executor.
    """
    import asyncio

    loop = asyncio.get_running_loop()

    def _run() -> Optional[Dict[str, Any]]:
        server = _make_server(config)
        conn = None

        try:
            # Step 1 — bind with service account (or anonymous)
            conn = ldap3.Connection(
                server,
                user=config.bind_dn or None,
                password=config.bind_password or None,
                authentication=ldap3.SIMPLE if (config.bind_dn and config.bind_password) else ldap3.ANONYMOUS,
                read_only=True,
                receive_timeout=config.timeout,
            )

            if not conn.bind():
                logger.info(
                    "LDAP service bind failed for %s: %s",
                    config.bind_dn or "(anonymous)",
                    conn.result.get("description", "unknown error"),
                )
                return None

            # Step 2 — search for the user
            search_filter = _build_search_filter(config, username)
            attrs = [config.login_attr, "userPrincipalName", config.fullname_attr, config.email_attr]
            attrs = list(dict.fromkeys(a for a in attrs if a))  # dedupe, drop empties

            if not attrs:
                attrs = [config.login_attr]

            success = conn.search(
                search_base=config.base_dn,
                search_filter=search_filter,
                attributes=attrs,
                size_limit=1,
            )

            if not success or len(conn.entries) == 0:
                logger.info(
                    "LDAP search returned no results for filter=%r base=%r",
                    search_filter,
                    config.base_dn,
                )
                return None

            entry = conn.entries[0]
            user_dn = entry.entry_dn

            if not user_dn:
                logger.info("LDAP entry has no DN")
                return None

            # Step 3 — rebind as the user to verify password
            user_conn = ldap3.Connection(
                server,
                user=user_dn,
                password=password,
                authentication=ldap3.SIMPLE,
                read_only=True,
                receive_timeout=config.timeout,
            )

            if not user_conn.bind():
                logger.info(
                    "LDAP user bind failed for %s: %s",
                    user_dn,
                    user_conn.result.get("description", "unknown error"),
                )
                user_conn.unbind()
                return None

            user_conn.unbind()

            # Extract attributes
            canonical_username = _safe_attr(entry, config.login_attr) or username
            fullname = _safe_attr(entry, config.fullname_attr) or username
            email = _safe_attr(entry, config.email_attr) or ""

            logger.info(
                "LDAP auth OK: %s (canonical: %s, DN: %s, display: %s)",
                username,
                canonical_username,
                user_dn,
                fullname,
            )
            return {
                "username": username,
                "canonical_username": canonical_username,
                "fullname": fullname,
                "email": email,
                "dn": user_dn,
            }

        except LDAPException as exc:
            logger.warning("LDAP error during authentication of %s: %s", username, exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error during LDAP auth for %s: %s", username, exc, exc_info=True)
            return None
        finally:
            if conn is not None and not conn.closed:
                try:
                    conn.unbind()
                except Exception:
                    pass

    return await loop.run_in_executor(None, _run)


async def test_ldap_connection(config: LDAPConfig) -> Dict[str, Any]:
    """Test basic connectivity to the LDAP server.

    Returns ``{"success": True, "message": "..."}`` on success,
    or ``{"success": False, "error": "..."}`` on failure.
    """
    import asyncio

    loop = asyncio.get_running_loop()

    def _run() -> Dict[str, Any]:
        server = _make_server(config)
        conn = None
        try:
            conn = ldap3.Connection(
                server,
                user=config.bind_dn or None,
                password=config.bind_password or None,
                authentication=ldap3.SIMPLE if (config.bind_dn and config.bind_password) else ldap3.ANONYMOUS,
                read_only=True,
                receive_timeout=config.timeout,
            )
            if not conn.bind():
                return {
                    "success": False,
                    "error": conn.result.get("description", "Bind failed"),
                }
            return {"success": True, "message": f"Connected to {config.server_uri}"}
        except LDAPException as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "error": f"Unexpected error: {exc}"}
        finally:
            if conn is not None and not conn.closed:
                try:
                    conn.unbind()
                except Exception:
                    pass

    return await loop.run_in_executor(None, _run)


async def lookup_ldap_user(
    config: LDAPConfig,
    username: str,
) -> Dict[str, Any]:
    """Look up a username in LDAP (no password verification).

    Returns ``{"found": False, "error": "..."}`` or
    ``{"found": True, "attributes": {...}}`` with the parsed attributes.
    This is used by the admin UI for connection testing / user lookup.
    """
    import asyncio

    loop = asyncio.get_running_loop()

    def _run() -> Dict[str, Any]:
        server = _make_server(config)
        conn = None
        try:
            conn = ldap3.Connection(
                server,
                user=config.bind_dn or None,
                password=config.bind_password or None,
                authentication=ldap3.SIMPLE if (config.bind_dn and config.bind_password) else ldap3.ANONYMOUS,
                read_only=True,
                receive_timeout=config.timeout,
            )

            if not conn.bind():
                return {
                    "found": False,
                    "error": f"Service bind failed: {conn.result.get('description', 'unknown')}",
                }

            search_filter = _build_search_filter(config, username)
            # Fetch all attributes so the admin can see what's available
            success = conn.search(
                search_base=config.base_dn,
                search_filter=search_filter,
                attributes=LDAP_ALL_ATTRS,
                size_limit=1,
            )

            if not success or len(conn.entries) == 0:
                return {
                    "found": False,
                    "error": f"User '{username}' not found in LDAP directory.",
                }

            entry = conn.entries[0]

            # Build a readable map of all attributes (DN shown separately)
            attrs: Dict[str, list] = {}
            for attr in entry.entry_attributes:
                values = getattr(entry, attr)
                # ldap3 returns values as a list
                if isinstance(values, list):
                    attrs[attr] = values
                else:
                    attrs[attr] = [str(values)] if values else []

            # Also extract the mapped fields
            login_val = _safe_attr(entry, config.login_attr) or username
            fullname_val = _safe_attr(entry, config.fullname_attr) or ""
            email_val = _safe_attr(entry, config.email_attr) or ""

            return {
                "found": True,
                "attributes": {
                    "dn": str(entry.entry_dn),
                    "login": login_val,
                    "fullname": fullname_val,
                    "email": email_val,
                    "all": attrs,
                },
            }

        except LDAPException as exc:
            return {"found": False, "error": f"LDAP error: {exc}"}
        except Exception as exc:
            logger.error("Unexpected error during LDAP lookup: %s", exc, exc_info=True)
            return {"found": False, "error": f"Internal error: {exc}"}
        finally:
            if conn is not None and not conn.closed:
                try:
                    conn.unbind()
                except Exception:
                    pass

    return await loop.run_in_executor(None, _run)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_attr(entry, attr_name: str) -> Optional[str]:
    """Safely extract the first string value of an LDAP attribute."""
    if not attr_name:
        return None
    try:
        raw = getattr(entry, attr_name, None)
        if raw is None:
            return None
        if isinstance(raw, list) and len(raw) > 0:
            val = raw[0]
            return str(val) if val is not None else None
        return str(raw) if raw else None
    except Exception:
        return None
