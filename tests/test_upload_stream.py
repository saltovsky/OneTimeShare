"""Tests for the streaming multipart reader (no HTTP server required).

Run with:  python -m pytest tests -q
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import List

import pytest
from starlette.requests import Request

from app.upload_stream import (
    MultipartError,
    UploadTooLarge,
    read_multipart_upload,
)

BOUNDARY = "----testboundary0123456789"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def field_part(name: str, value: str) -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode()


def file_part(name: str, filename: str, payload: bytes) -> bytes:
    head = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    return head + payload + b"\r\n"


def body(*parts: bytes) -> bytes:
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


def make_request(
    data: bytes,
    *,
    content_type: str | None = None,
    chunk_size: int = 256 * 1024,
    on_receive=None,
) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/upload",
        "raw_path": b"/api/upload",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (
                b"content-type",
                (content_type or f"multipart/form-data; boundary={BOUNDARY}").encode(),
            ),
            (b"content-length", str(len(data)).encode()),
        ],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    offset = 0

    async def receive() -> dict:
        nonlocal offset
        if on_receive is not None:
            on_receive()
        if not data:
            # A real empty request still delivers one empty final body message.
            if offset == 0:
                offset = 1
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}
        if offset >= len(data):
            return {"type": "http.disconnect"}
        chunk = data[offset:offset + chunk_size]
        offset += len(chunk)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": offset < len(data),
        }

    return Request(scope, receive)


def read(
    data: bytes,
    upload_dir: Path,
    *,
    max_file_size: int = 10 * 1024 * 1024,
    max_total_size: int = 20 * 1024 * 1024,
    max_files: int = 10,
    max_fields: int = 10,
    on_field=None,
    loaded_progress: List[int] | None = None,
):
    kwargs = {}
    if loaded_progress is not None:
        def _track() -> None:
            loaded_progress.append(
                sum(p.stat().st_size for p in upload_dir.iterdir())
            )
        kwargs["on_receive"] = _track

    request = make_request(data, **kwargs)
    return asyncio.run(
        read_multipart_upload(
            request,
            upload_dir=str(upload_dir),
            max_file_size=max_file_size,
            max_total_size=max_total_size,
            max_files=max_files,
            max_fields=max_fields,
            buffer_size=1024 * 1024,
            on_field=on_field,
        )
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
def test_single_file_and_password_field(tmp_path: Path) -> None:
    payload = os.urandom(3 * 1024 * 1024)
    seen_fields: List[tuple[str, str]] = []

    result = read(
        body(
            file_part("files", "report.pdf", payload),
            field_part("password", "s3cret"),
        ),
        tmp_path,
        on_field=lambda name, value: seen_fields.append((name, value)),
    )

    assert len(result.files) == 1
    part = result.files[0]
    assert part.original_filename == "report.pdf"
    assert part.size == len(payload)
    assert Path(part.stored_path).read_bytes() == payload
    assert Path(part.stored_path).parent == tmp_path
    assert result.fields == {"password": "s3cret"}
    assert seen_fields == [("password", "s3cret")]
    assert result.total_size == len(payload)


def test_multiple_files(tmp_path: Path) -> None:
    first = b"a" * 700_000
    second = os.urandom(1_500_000)
    result = read(
        body(
            file_part("files", "a.bin", first),
            file_part("files", "b.bin", second),
        ),
        tmp_path,
    )

    assert [p.original_filename for p in result.files] == ["a.bin", "b.bin"]
    assert [p.size for p in result.files] == [len(first), len(second)]
    assert result.total_size == len(first) + len(second)
    stored = sorted(Path(p.stored_path).name for p in result.files)
    assert len(stored) == 2
    assert sorted(p.read_bytes() for p in tmp_path.iterdir()) == sorted([first, second])


def test_empty_file_is_stored(tmp_path: Path) -> None:
    result = read(body(file_part("files", "empty.txt", b"")), tmp_path)

    assert len(result.files) == 1
    assert result.files[0].size == 0
    assert Path(result.files[0].stored_path).read_bytes() == b""


def test_data_is_written_while_body_is_still_arriving(tmp_path: Path) -> None:
    progress: List[int] = []
    payload = os.urandom(4 * 1024 * 1024)
    read(body(file_part("files", "big.bin", payload)), tmp_path, loaded_progress=progress)

    # Bytes must already be on disk before the request body has been consumed.
    assert len(progress) > 2
    assert max(progress[:-1]) > 0


def test_filename_traversal_is_neutralised(tmp_path: Path) -> None:
    result = read(body(file_part("files", "../../etc/passwd", b"x")), tmp_path)

    stored = Path(result.files[0].stored_path)
    assert stored.parent == tmp_path
    assert ".." not in stored.name
    assert result.files[0].original_filename == "passwd"


# ---------------------------------------------------------------------------
# Limits and malformed input
# ---------------------------------------------------------------------------
def test_file_size_limit_removes_partial_file(tmp_path: Path) -> None:
    data = body(file_part("files", "big.bin", os.urandom(2 * 1024 * 1024)))
    with pytest.raises(UploadTooLarge):
        read(data, tmp_path, max_file_size=1024 * 1024)
    assert list(tmp_path.iterdir()) == []


def test_total_size_limit_removes_all_files(tmp_path: Path) -> None:
    data = body(
        file_part("files", "a.bin", os.urandom(1024 * 1024)),
        file_part("files", "b.bin", os.urandom(1024 * 1024)),
    )
    with pytest.raises(UploadTooLarge):
        read(data, tmp_path, max_total_size=1_500_000)
    assert list(tmp_path.iterdir()) == []


def test_too_many_files(tmp_path: Path) -> None:
    data = body(
        file_part("files", "a.bin", b"a"),
        file_part("files", "b.bin", b"b"),
    )
    with pytest.raises(MultipartError):
        read(data, tmp_path, max_files=1)
    assert list(tmp_path.iterdir()) == []


def test_oversized_field(tmp_path: Path) -> None:
    data = body(
        field_part("password", "x" * (70 * 1024)),
        file_part("files", "a.bin", b"a"),
    )
    with pytest.raises(MultipartError):
        read(data, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_oversized_part_header(tmp_path: Path) -> None:
    head = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{"a" * 10_000}"\r\n\r\n'
    ).encode()
    data = head + b"x" + b"\r\n" + f"--{BOUNDARY}--\r\n".encode()
    with pytest.raises(MultipartError):
        read(data, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_garbage_body_is_rejected(tmp_path: Path) -> None:
    # The stream must not be treated as a request that starts with a preamble:
    # python-multipart rejects data that does not begin with the boundary.
    data = b"x" * (3 * 1024 * 1024)
    with pytest.raises(MultipartError):
        read(data, tmp_path, max_total_size=1024)
    assert list(tmp_path.iterdir()) == []


def test_truncated_body_removes_partial_file(tmp_path: Path) -> None:
    data = f"--{BOUNDARY}\r\n".encode() + file_part(
        "files", "a.bin", os.urandom(1024 * 1024)
    )  # no closing boundary, no trailing CRLF
    with pytest.raises(MultipartError):
        read(data, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_missing_boundary(tmp_path: Path) -> None:
    with pytest.raises(MultipartError):
        asyncio.run(
            read_multipart_upload(
                make_request(b"whatever", content_type="multipart/form-data"),
                upload_dir=str(tmp_path),
                max_file_size=1024,
                max_total_size=2048,
                max_files=1,
                max_fields=1,
                buffer_size=1024,
            )
        )


def test_wrong_content_type(tmp_path: Path) -> None:
    with pytest.raises(MultipartError):
        asyncio.run(
            read_multipart_upload(
                make_request(b"{}", content_type="application/json"),
                upload_dir=str(tmp_path),
                max_file_size=1024,
                max_total_size=2048,
                max_files=1,
                max_fields=1,
                buffer_size=1024,
            )
        )


def test_empty_body(tmp_path: Path) -> None:
    result = read(b"", tmp_path)
    assert result.files == []
