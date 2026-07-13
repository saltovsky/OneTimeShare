"""SQLAlchemy async engine and session factory."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import AsyncIterator

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings as app_settings
from .models import Base, Setting, User

logger = logging.getLogger(__name__)

# `check_same_thread=False` is required for SQLite to work with aiosqlite
# across the asyncio event loop.
_engine_kwargs: dict = {"echo": False, "future": True}
if app_settings.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_async_engine(app_settings.DATABASE_URL, **_engine_kwargs)

async_session_factory = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def init_db() -> None:
    """Create all tables (idempotent) and run schema migrations.

    `Base.metadata.create_all` only creates NEW tables, it does NOT alter
    existing ones. Schema migrations for existing tables are applied here.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Apply column additions to existing tables (safe on SQLite)
        await _migrate_add_column_if_missing(conn, "links", "uploader_id")

    # Seed initial users and settings in a separate session
    async with async_session_factory() as session:
        await _seed_users(session)
        await _seed_settings(session)
        await session.commit()

    logger.info("Database initialised at %s", app_settings.DATABASE_URL)


async def _migrate_add_column_if_missing(conn, table_name: str, column_name: str) -> None:
    """Add a column to an existing SQLite table if it doesn't already exist.

    SQLite does not support IF NOT EXISTS on ALTER TABLE, so we inspect
    PRAGMA table_info first. Column addition is non-blocking for existing
    data (NULL for all existing rows).
    """
    if not app_settings.DATABASE_URL.startswith("sqlite"):
        return  # Only SQLite needs this manual migration path

    result = await conn.execute(
        text(f"SELECT name FROM pragma_table_info(:table) WHERE name = :col"),
        {"table": table_name, "col": column_name},
    )
    if result.fetchone() is not None:
        return  # Column already exists

    # The model defines: uploader_id VARCHAR(128) REFERENCES users(username) ON DELETE SET NULL
    await conn.execute(
        text(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} "
            "VARCHAR(128) REFERENCES users(username) ON DELETE SET NULL"
        )
    )
    await conn.commit()
    logger.info(
        "Migration: added column %s to table %s", column_name, table_name
    )


async def _seed_users(session: AsyncSession) -> None:
    """Create default admin and uploader from env vars if no users exist yet."""
    from .security import hash_password

    result = await session.execute(select(User))
    existing = result.scalars().first()
    if existing is not None:
        return  # Users already exist, skip seeding

    now = datetime.utcnow()

    # Seed admin
    admin = User(
        username=app_settings.ADMIN_USERNAME,
        password_hash=hash_password(app_settings.ADMIN_PASSWORD),
        role="admin",
        created_at=now,
    )
    session.add(admin)
    logger.info("Seeded admin user: %s", app_settings.ADMIN_USERNAME)

    # Seed uploader (only if different from admin)
    if app_settings.UPLOADER_USERNAME != app_settings.ADMIN_USERNAME:
        uploader = User(
            username=app_settings.UPLOADER_USERNAME,
            password_hash=hash_password(app_settings.UPLOADER_PASSWORD),
            role="uploader",
            created_at=now,
        )
        session.add(uploader)
        logger.info("Seeded uploader user: %s", app_settings.UPLOADER_USERNAME)
    else:
        logger.info("Uploader username matches admin; granting admin full access.")


async def _seed_settings(session: AsyncSession) -> None:
    """Ensure the footer_text setting exists with a default value."""
    result = await session.execute(
        select(Setting).where(Setting.key == "footer_text")
    )
    if result.scalar_one_or_none() is not None:
        return

    default_footer = "Self-hosted burn-after-reading file sharing"
    session.add(Setting(key="footer_text", value=default_footer))
    logger.info("Seeded default footer_text setting.")


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields an async session, ensures close on exit."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
