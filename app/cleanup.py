"""Background cleanup task.

Links have no time-based expiry anymore (they live until downloaded or
revoked), so the only remaining background job is removing **orphan files**:
anything left in the upload directory that is not referenced by any File
row in the database. This catches container crashes, manual tinkering, and
any future code path that deletes a DB row without cleaning up the disk.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterable

from sqlalchemy import select

from .config import settings
from .database import async_session_factory
from .models import File

logger = logging.getLogger(__name__)


def _delete_files_from_disk(paths: Iterable[str]) -> int:
    removed = 0
    for p in paths:
        try:
            if p and os.path.exists(p) and os.path.isfile(p):
                os.remove(p)
                removed += 1
        except OSError as exc:
            logger.warning("Failed to delete %s: %s", p, exc)
    return removed


async def cleanup_once() -> None:
    """Run one cleanup pass. Safe to call concurrently (uses its own session)."""
    async with async_session_factory() as db:
        if not os.path.isdir(settings.UPLOAD_DIR):
            return
        try:
            disk_files = set(os.listdir(settings.UPLOAD_DIR))
        except OSError as exc:
            logger.warning("Cannot list upload dir: %s", exc)
            return

        result = await db.execute(select(File.stored_filepath))
        db_files = {os.path.basename(p) for (p,) in result.all() if p}
        orphans = disk_files - db_files
        if orphans:
            for fname in orphans:
                full = os.path.join(settings.UPLOAD_DIR, fname)
                if os.path.isfile(full):
                    try:
                        os.remove(full)
                    except OSError as exc:
                        logger.warning("Failed to remove orphan %s: %s", full, exc)
            logger.info("Removed %d orphan file(s) from disk", len(orphans))


async def cleanup_loop() -> None:
    """Long-running task: sleep -> cleanup -> sleep -> ..."""
    logger.info(
        "Background orphan-cleanup started (interval=%ss)",
        settings.CLEANUP_INTERVAL_SECONDS,
    )
    while True:
        try:
            await cleanup_once()
        except Exception:
            logger.exception("Cleanup pass failed; will retry next interval")
        await asyncio.sleep(settings.CLEANUP_INTERVAL_SECONDS)
