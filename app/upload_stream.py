"""Streaming ``multipart/form-data`` reader that writes file parts straight to disk.

Why this module exists
======================

FastAPI/Starlette parse an upload into ``UploadFile`` objects backed by a
``SpooledTemporaryFile``: every file is first written to a temporary file
(``/tmp``, i.e. the container's writable layer or tmpfs) and the route handler
then copies it to permanent storage. For large files that means

  * the payload is written to disk **twice** (plus once more by a reverse proxy
    that buffers request bodies), so the share link only appears after the
    second full pass over the data;
  * the temporary spool needs as much free space in ``/tmp`` as the upload
    itself, which is a common source of ENOSPC/"stuck at 99%" reports;
  * the copy happens *after* the client has already finished sending the body,
    which is exactly the "100% uploaded but the link is still generating"
    pause users see.

This reader feeds ``python-multipart``'s streaming parser directly and writes
part data into the destination file. The payload touches the disk once, there
is no temporary spool, and the disk write pipeline is already saturated while
the body is still arriving, so finishing the request costs almost nothing.

Threading model
---------------
Parser callbacks are synchronous (they only append to an in-memory buffer).
The actual disk writes run in a worker thread in large buffers, so the asyncio
event loop never blocks on I/O and other uploads/downloads keep running.

Security
--------
  * every file/field count and byte budget is enforced *while* streaming, so an
    oversized body is rejected mid-transfer instead of after it was received;
  * stored names are generated (random prefix) and re-checked against the
    upload directory to rule out path traversal;
  * partial files are always removed when parsing fails or the client
    disconnects;
  * field values are capped (``MAX_FIELD_SIZE``) to avoid memory abuse;
  * decode/parse errors are reported as client errors (400/413), never as 500
    with internal details.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect, Request

from .security import sanitize_filename

logger = logging.getLogger(__name__)

# A non-file field (the only one we accept is ``password``) never legitimately
# needs more than a few hundred bytes. Cap it to keep memory bounded.
MAX_FIELD_SIZE = 64 * 1024
# Never buffer less than this before handing a write to the worker thread:
# smaller flushes would turn into thousands of thread hops per upload.
MIN_BUFFER_SIZE = 256 * 1024
# Bounds on per-part header parsing (nothing legitimate comes close).
MAX_HEADER_BYTES = 8 * 1024
MAX_HEADERS_PER_PART = 32
# Allowance for boundaries and part headers on top of the payload budget. Also
# used by the routes to reject an oversized ``Content-Length`` up front.
MULTIPART_OVERHEAD_ALLOWANCE = 2 * 1024 * 1024


class MultipartError(ValueError):
    """The request body is not a valid multipart form (client error, 400)."""


class UploadTooLarge(ValueError):
    """The upload exceeds the configured size limits (client error, 413)."""


@dataclass(frozen=True)
class StoredPart:
    """A file part that has been fully written to its destination path."""

    field_name: str
    original_filename: str
    stored_path: str
    size: int


@dataclass
class UploadPayload:
    files: List[StoredPart] = field(default_factory=list)
    fields: Dict[str, str] = field(default_factory=dict)

    @property
    def total_size(self) -> int:
        return sum(part.size for part in self.files)


def _user_safe_decode(src: bytes, codec: str) -> str:
    """Decode a header/filename value; fall back to latin-1 like Starlette does."""
    try:
        return src.decode(codec)
    except (UnicodeDecodeError, LookupError):
        return src.decode("latin-1")


class _FileSink:
    """Destination file for one part: in-memory buffer + writes in a thread."""

    def __init__(self, path: str) -> None:
        self.path = path
        # Unbuffered handle: the payload is already a large buffer, so an extra
        # userspace buffer would only cost a second memcpy.
        self._handle = open(path, "wb", buffering=0)  # noqa: SIM115 - closed in close()
        self.pending = bytearray()
        self.size = 0
        self.closed = False

    def append(self, data: bytes) -> None:
        self.pending += data
        self.size += len(data)

    async def flush(self) -> None:
        if not self.pending:
            return
        payload = bytes(self.pending)
        self.pending.clear()
        await asyncio.to_thread(self._write_all, payload)

    def _write_all(self, payload: bytes) -> None:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            chunk_size = self._handle.write(view[written:])
            if not chunk_size:
                raise OSError("Kernel accepted 0 bytes while writing the upload")
            written += chunk_size

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._handle.close()


class _MultipartCollector:
    """``python-multipart`` callbacks that store parts as they arrive."""

    def __init__(
        self,
        *,
        upload_dir: str,
        max_file_size: int,
        max_total_size: int,
        max_files: int,
        max_fields: int,
        on_field: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._upload_dir = os.path.realpath(upload_dir)
        self._max_file_size = max_file_size
        self._max_total_size = max_total_size
        self._max_files = max_files
        self._max_fields = max_fields
        self._on_field = on_field

        self._charset = "utf-8"
        self._header_field = b""
        self._header_value = b""
        self._part_name = ""
        self._part_filename: Optional[str] = None
        self._part_headers: List[tuple[bytes, bytes]] = []
        self._field_buffer = bytearray()
        self._active_sink: Optional[_FileSink] = None

        self._files_count = 0
        self._fields_count = 0
        self._total_size = 0

        self._finished_sinks: List[_FileSink] = []  # flushed + closed by the loop
        self._all_sinks: List[_FileSink] = []
        self.payload = UploadPayload()

    # -- callbacks ---------------------------------------------------------
    def on_part_begin(self) -> None:
        self._part_name = ""
        self._part_filename = None
        self._part_headers = []
        self._field_buffer = bytearray()
        self._active_sink = None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]
        if len(self._header_field) > MAX_HEADER_BYTES:
            raise MultipartError("Part header field is too long")

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]
        if len(self._header_value) > MAX_HEADER_BYTES:
            raise MultipartError("Part header value is too long")

    def on_header_end(self) -> None:
        self._part_headers.append((self._header_field.lower(), self._header_value))
        if len(self._part_headers) > MAX_HEADERS_PER_PART:
            raise MultipartError("Too many part headers")
        self._header_field = b""
        self._header_value = b""

    def on_headers_finished(self) -> None:
        disposition = b""
        for name, value in self._part_headers:
            if name == b"content-disposition":
                disposition = value
                break
        _, options = parse_options_header(disposition)

        try:
            self._part_name = _user_safe_decode(options[b"name"], self._charset)
        except KeyError:
            raise MultipartError(
                'Content-Disposition header is missing the "name" parameter'
            ) from None

        if b"filename" not in options:
            self._fields_count += 1
            if self._fields_count > self._max_fields:
                raise MultipartError(
                    f"Too many form fields (max {self._max_fields})"
                )
            return

        self._files_count += 1
        if self._files_count > self._max_files:
            raise MultipartError(f"Too many files (max {self._max_files})")
        self._part_filename = _user_safe_decode(options[b"filename"], self._charset)
        self._active_sink = self._open_sink(self._part_filename)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if not chunk:
            return
        sink = self._active_sink
        if sink is None:
            if len(self._field_buffer) + len(chunk) > MAX_FIELD_SIZE:
                raise MultipartError(
                    f"Form field exceeds {MAX_FIELD_SIZE // 1024}KB"
                )
            self._field_buffer += chunk
            return

        if sink.size + len(chunk) > self._max_file_size:
            raise UploadTooLarge("File exceeds the per-file size limit")
        if self._total_size + len(chunk) > self._max_total_size:
            raise UploadTooLarge("Upload exceeds the total size limit")

        sink.append(chunk)
        self._total_size += len(chunk)

    def on_part_end(self) -> None:
        sink = self._active_sink
        if sink is not None:
            self._finished_sinks.append(sink)
            self._active_sink = None
            self.payload.files.append(
                StoredPart(
                    field_name=self._part_name,
                    original_filename=sanitize_filename(self._part_filename or "file"),
                    stored_path=sink.path,
                    size=sink.size,
                )
            )
            return

        value = _user_safe_decode(bytes(self._field_buffer), self._charset)
        self._field_buffer = bytearray()
        self.payload.fields[self._part_name] = value
        if self._on_field is not None:
            self._on_field(self._part_name, value)

    def on_end(self) -> None:
        pass

    def callbacks(self) -> Dict[str, Callable[..., None]]:
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }

    # -- helpers -----------------------------------------------------------
    def _open_sink(self, filename: str) -> _FileSink:
        safe_name = sanitize_filename(filename or "file")
        stored_name = f"{uuid.uuid4().hex}_{safe_name}"
        stored_path = os.path.join(self._upload_dir, stored_name)
        # Defence in depth: reject anything that would escape the upload dir.
        if not os.path.realpath(stored_path).startswith(self._upload_dir + os.sep):
            raise MultipartError("Invalid filename")
        sink = _FileSink(stored_path)
        self._all_sinks.append(sink)
        return sink

    def set_charset(self, charset: bytes) -> None:
        self._charset = _user_safe_decode(charset, "latin-1")

    async def flush_if_needed(self, buffer_size: int) -> None:
        # Finish parts that already ended (in order), then keep the active
        # part's buffer bounded.
        while self._finished_sinks:
            sink = self._finished_sinks.pop(0)
            await sink.flush()
            sink.close()
        active = self._active_sink
        if active is not None and len(active.pending) >= buffer_size:
            await active.flush()

    async def finish(self) -> UploadPayload:
        for sink in self._finished_sinks:
            await sink.flush()
            sink.close()
        self._finished_sinks.clear()
        if self._active_sink is not None:
            raise MultipartError("Truncated multipart body")
        return self.payload

    def abort(self) -> None:
        """Close handles and delete every file created for this request."""
        for sink in self._all_sinks:
            try:
                sink.close()
            except OSError:
                pass
        for sink in self._all_sinks:
            try:
                if os.path.exists(sink.path):
                    os.remove(sink.path)
            except OSError as exc:  # pragma: no cover - best effort cleanup
                logger.warning("Could not remove partial upload %s: %s", sink.path, exc)
        self._all_sinks.clear()
        self._finished_sinks.clear()


def remove_files(parts: Iterable[StoredPart]) -> None:
    """Best-effort deletion of already stored parts (used when persistence fails)."""
    for part in parts:
        try:
            if os.path.exists(part.stored_path):
                os.remove(part.stored_path)
        except OSError as exc:  # pragma: no cover - best effort cleanup
            logger.warning("Could not remove %s: %s", part.stored_path, exc)


async def read_multipart_upload(
    request: Request,
    *,
    upload_dir: str,
    max_file_size: int,
    max_total_size: int,
    max_files: int,
    max_fields: int,
    buffer_size: int,
    on_field: Optional[Callable[[str, str], None]] = None,
) -> UploadPayload:
    """Consume a multipart request body and persist its file parts.

    Files are written directly into ``upload_dir`` under generated names.
    Any exception (limit violation, malformed body, client disconnect) removes
    the partially written files before propagating.

    ``on_field`` is invoked for every completed non-file part while the request
    body is still being received — the upload route uses it to start Argon2
    hashing of the optional password without delaying the response.
    """
    content_type = request.headers.get("content-type", "")
    media_type, params = parse_options_header(content_type)
    if media_type.lower() != b"multipart/form-data":
        raise MultipartError("Expected multipart/form-data request body")
    boundary = params.get(b"boundary")
    if not boundary:
        raise MultipartError("Missing multipart boundary")

    collector = _MultipartCollector(
        upload_dir=upload_dir,
        max_file_size=max_file_size,
        max_total_size=max_total_size,
        max_files=max_files,
        max_fields=max_fields,
        on_field=on_field,
    )
    collector.set_charset(params.get(b"charset", b"utf-8"))

    parser = MultipartParser(boundary, collector.callbacks())
    buffer_size = max(buffer_size, MIN_BUFFER_SIZE)

    succeeded = False
    body_budget = max_total_size + MULTIPART_OVERHEAD_ALLOWANCE
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > body_budget:
                raise UploadTooLarge("Request body exceeds the total size limit")
            try:
                parser.write(chunk)
            except (MultipartError, UploadTooLarge):
                raise
            except Exception as exc:
                raise MultipartError("Malformed multipart body") from exc
            await collector.flush_if_needed(buffer_size)

        try:
            parser.finalize()
        except (MultipartError, UploadTooLarge):
            raise
        except Exception as exc:
            raise MultipartError("Malformed multipart body") from exc

        payload = await collector.finish()
        succeeded = True
        return payload
    except ClientDisconnect:
        logger.info("Client disconnected during upload - partial files removed")
        raise
    finally:
        if not succeeded:
            collector.abort()
