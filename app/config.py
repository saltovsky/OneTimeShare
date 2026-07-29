"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    # Storage
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "/app/data"))
    UPLOAD_DIR: Path = DATA_DIR / "uploads"

    # Database — SQLite for zero-config deployment
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{(DATA_DIR / 'onetimeshare.db').as_posix()}",
    )

    # Admin credentials
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "changeme")

    # Uploader credentials (required to create new share links).
    # Separate from admin so the upload role can be granted to untrusted
    # operators without giving them the ability to revoke others' links.
    UPLOADER_USERNAME: str = os.getenv("UPLOADER_USERNAME", "uploader")
    UPLOADER_PASSWORD: str = os.getenv("UPLOADER_PASSWORD", "changeme")

    # Crypto secret — REQUIRED for signing download cookies.
    # We generate a per-process random fallback so the service never silently
    # boots with a default secret in production. Always set SECRET_KEY in .env.
    SECRET_KEY: str = os.getenv("SECRET_KEY") or secrets.token_urlsafe(64)

    # Limits
    MAX_FILE_SIZE: int = int(os.getenv("MAX_FILE_SIZE", str(100 * 1024 * 1024)))     # 100 MB
    MAX_TOTAL_SIZE: int = int(os.getenv("MAX_TOTAL_SIZE", str(500 * 1024 * 1024)))   # 500 MB

    # Streaming
    CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", str(64 * 1024)))   # 64 KB
    # Multipart uploads have already been spooled by Starlette when the route
    # handler runs. Use a larger buffer for the one remaining copy into the
    # permanent storage, without increasing download-stream memory usage.
    UPLOAD_CHUNK_SIZE: int = int(os.getenv("UPLOAD_CHUNK_SIZE", str(1024 * 1024)))  # 1 MB

    # Background cleanup
    CLEANUP_INTERVAL_SECONDS: int = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))

    # Public-facing URL (used in UI to render the share link)
    APP_URL: str = os.getenv("APP_URL", "http://localhost:8000").rstrip("/")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # Use a flag to warn operators that the secret is auto-generated
    SECRET_KEY_AUTO_GENERATED: bool = os.getenv("SECRET_KEY") is None


settings = Settings()
