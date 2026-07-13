"""File streaming + burn-after-reading.

Two responsibilities:

1. **Stream file(s) from disk to the network socket** without buffering the
   whole file in memory. For multi-file uploads we build a ZIP archive on
   the fly using `zipstream-ng` and pipe its chunks through a thread-safe
   queue so the sync ZIP producer does not block the asyncio event loop.

2. **`burn_after_stream` wrapper** — this is the heart of the burn-after-
   reading contract. The wrapped async generator invokes a user-supplied
   `on_success` coroutine **only if the iteration completes normally**.
   Any of the following — `asyncio.CancelledError`, `GeneratorExit`,
   `ConnectionResetError`, `BrokenPipeError` — is treated as an aborted
   download and the callback is NOT invoked, so the files are NOT deleted.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
from typing import AsyncIterator, Awaitable, Callable, Iterable, List, Tuple

import aiofiles
import zipstream

from .config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Single-file streaming
# ---------------------------------------------------------------------------


async def stream_single_file(filepath: str) -> AsyncIterator[bytes]:
    """Yield file contents in fixed-size chunks; no full read into memory."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(filepath)
    async with aiofiles.open(filepath, "rb") as f:
        while True:
            chunk = await f.read(settings.CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


# ---------------------------------------------------------------------------
# Multi-file streaming — ZIP on the fly, zero temp files
# ---------------------------------------------------------------------------


async def stream_zip_files(entries: Iterable[Tuple[str, str]]) -> AsyncIterator[bytes]:
    """Stream a ZIP archive containing the given (stored_path, arcname) pairs.

    `zipstream-ng` is synchronous and produces chunks lazily as you iterate
    the `ZipStream` object. We run its producer in a background thread and
    bridge chunks into the asyncio world via a bounded queue. If the client
    disconnects, the async consumer stops reading; the producer will block
    on `queue.put` and the daemon thread is reaped at process exit.
    """
    q: "queue.Queue[bytes | BaseException | None]" = queue.Queue(maxsize=8)
    entries_list: List[Tuple[str, str]] = list(entries)

    def _producer() -> None:
        try:
            z = zipstream.ZipStream()
            for stored_path, arcname in entries_list:
                if os.path.exists(stored_path):
                    z.add_path(stored_path, arcname)
            for chunk in z:
                q.put(chunk)
        except BaseException as exc:  # noqa: BLE001 - re-raised via queue
            q.put(exc)
        finally:
            q.put(None)  # sentinel: end of stream

    thread = threading.Thread(target=_producer, name="zipstream-producer", daemon=True)
    thread.start()

    loop = asyncio.get_running_loop()
    try:
        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        # If we exit early (client disconnect) make sure the producer thread
        # does not keep adding to a queue nobody reads. Daemon=True means the
        # thread will die with the process; we still call join(timeout) to
        # release file descriptors promptly.
        thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# Burn-after-reading wrapper
# ---------------------------------------------------------------------------

OnSuccess = Callable[[], Awaitable[None]]


async def burn_after_stream(
    source: AsyncIterator[bytes],
    on_success: OnSuccess,
) -> AsyncIterator[bytes]:
    """Pass-through generator that triggers `on_success` only on full delivery.

    Semantics:
      * iteration completes without exception  -> on_success() awaited
      * client disconnects / connection lost   -> on_success NOT called
      * any other exception during streaming   -> on_success NOT called

    The callback runs inside the generator's `finally` block; by the time
    it executes, the HTTP response body has been fully sent to the client
    (Starlette waits for the iterator to be exhausted before flushing the
    terminating empty body chunk).
    """
    success = False
    try:
        async for chunk in source:
            yield chunk
        success = True
    except (asyncio.CancelledError, GeneratorExit):
        # Normal FastAPI/Starlette cancellation when the client disconnects.
        logger.info("Download aborted by client - files will be preserved for retry")
        raise
    except (ConnectionResetError, BrokenPipeError):
        logger.info("Client connection lost during download - files preserved")
        raise
    except Exception:
        logger.exception("Unexpected streaming error - files preserved")
        raise
    finally:
        if success:
            try:
                await on_success()
            except Exception:
                # Never let cleanup failure crash the worker after a successful
                # download. Log and move on; admin can re-clean via UI.
                logger.exception("Burn-after-reading cleanup failed")
