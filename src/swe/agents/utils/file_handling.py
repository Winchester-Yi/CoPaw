# -*- coding: utf-8 -*-
"""File handling utilities for downloading and managing files.

This module provides utilities for:
- Downloading files from base64 encoded data
- Downloading files from URLs
- Managing download directories
- Reading text files with encoding fallback for cross-platform compatibility
"""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import functools
import hashlib
import logging
import mimetypes
import os
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from ...config.context import get_current_workspace_dir
from ...constant import WORKING_DIR
from .async_download import (
    AsyncDownloadError,
    AsyncDownloadHTTPError,
    AsyncDownloadPolicyError,
    download_http_to_path,
)

logger = logging.getLogger(__name__)

_MAX_MEDIA_BYTES = 10 * 1024 * 1024
_MEDIA_TOTAL_TIMEOUT = 60.0
_MEDIA_CONNECT_TIMEOUT = 10.0
_MEDIA_READ_TIMEOUT = 30.0
_MEDIA_WORKER_COUNT = 4
_media_executor: ThreadPoolExecutor | None = None
_media_executor_lock = threading.Lock()
_media_slots = threading.BoundedSemaphore(_MEDIA_WORKER_COUNT)


def _get_media_executor() -> ThreadPoolExecutor:
    """Return the process-local bounded executor for blocking media work."""
    global _media_executor
    if _media_executor is None:
        with _media_executor_lock:
            if _media_executor is None:
                _media_executor = ThreadPoolExecutor(
                    max_workers=_MEDIA_WORKER_COUNT,
                    thread_name_prefix="swe-media",
                )
    return _media_executor


async def _run_media_worker(func, *args):
    """Run blocking media work on the process-local bounded executor."""
    loop = asyncio.get_running_loop()
    await _acquire_media_slot()
    try:
        future = loop.run_in_executor(
            _get_media_executor(),
            functools.partial(func, *args),
        )
    except BaseException:
        _media_slots.release()
        raise
    released = False

    def _release_slot(_future=None) -> None:
        nonlocal released
        if not released:
            released = True
            _media_slots.release()

    try:
        result = await asyncio.shield(future)
    except asyncio.CancelledError:
        # Keep the slot held until the underlying worker actually exits;
        # otherwise a cancelled coroutine can temporarily exceed the process
        # concurrency bound while its executor task is still running.
        future.add_done_callback(_release_slot)
        raise
    except BaseException:
        _release_slot()
        raise
    _release_slot()
    return result


async def _run_async_media_slot() -> float:
    """Acquire the shared process-level media slot without blocking the loop."""
    queued_at = time.monotonic()
    await _acquire_media_slot()
    return (time.monotonic() - queued_at) * 1000


async def _acquire_media_slot() -> None:
    """Acquire a media slot without leaving an orphaned waiter on cancel."""
    while not _media_slots.acquire(  # pylint: disable=consider-using-with
        blocking=False,
    ):
        await asyncio.sleep(0.005)


async def _run_async_media_slot_with_cleanup(temp_path: Path) -> float:
    """Acquire a media slot and clean its prepared target if cancelled."""
    try:
        return await _run_async_media_slot()
    except BaseException:
        await asyncio.to_thread(temp_path.unlink, True)
        raise


def read_text_file_with_encoding_fallback(file_path: Path | str) -> str:
    """Read text file with multiple encoding attempts for cross-platform
    compatibility.

    This function handles files created with different text editors on
    different platforms, especially addressing the common issue where Windows
    Notepad saves files in GBK encoding while most editors use UTF-8.

    Tries common encodings in order:
    1. UTF-8 with BOM (Windows Notepad with "UTF-8" option) - tried first
       to handle BOM correctly
    2. UTF-8 (default, most common on macOS/Linux)
    3. GBK/CP936 (Windows Notepad default for Chinese)
    4. CP1252/Latin-1 (Windows Notepad default for Western languages)
    5. UTF-8 with errors='replace' as final fallback

    Args:
        file_path: Path to the file to read (Path object or string)

    Returns:
        File content as string (with original whitespace preserved)

    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If file cannot be read even with fallback encodings
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    encodings_to_try = [
        "utf-8-sig",  # UTF-8 with BOM - try first
        "utf-8",
        "gbk",  # Windows Chinese default
        "cp936",  # Alias for GBK
        "cp1252",  # Windows Western default
        "latin-1",  # Fallback for Western text
    ]

    for encoding in encodings_to_try:
        try:
            with open(file_path, "r", encoding=encoding) as f:
                content = f.read()
                if encoding not in ("utf-8", "utf-8-sig"):
                    logger.debug(
                        "File %s read with encoding: %s",
                        file_path.name,
                        encoding,
                    )
                return content
        except (UnicodeDecodeError, LookupError):
            continue

    # Final fallback: UTF-8 with error replacement
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            logger.warning(
                "File %s read with UTF-8 errors='replace' fallback, "
                "some characters may be corrupted",
                file_path.name,
            )
            return content
    except Exception as e:
        logger.error(
            "File %s cannot be read even with fallback: %s",
            file_path.name,
            e,
        )
        raise IOError(
            f"File {file_path.name} cannot be read even with fallback: {e}",
        ) from e


def _default_download_dir() -> str:
    """Return the default download directory under the current workspace."""
    base_dir = get_current_workspace_dir() or WORKING_DIR
    return str(base_dir / "downloads")


def _resolve_local_path(
    url: str,
    parsed: urllib.parse.ParseResult,
) -> Optional[str]:
    """Return local file path for file:// or plain path; None for remote."""
    if parsed.scheme == "file":
        local_path = Path(urllib.request.url2pathname(parsed.path))
        if not local_path.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")
        if local_path.is_file():
            size = local_path.stat().st_size
            if size == 0:
                raise ValueError(f"Local file is empty: {local_path}")
            if size > _MAX_MEDIA_BYTES:
                raise ValueError("Downloaded media exceeds 10 MiB limit")
        return str(local_path.resolve())
    if parsed.scheme == "" and parsed.netloc == "":
        p = Path(url).expanduser()
        if p.exists():
            if p.is_file():
                size = p.stat().st_size
                if size == 0:
                    raise ValueError(f"Local file is empty: {p}")
                if size > _MAX_MEDIA_BYTES:
                    raise ValueError("Downloaded media exceeds 10 MiB limit")
            return str(p.resolve())
    # Windows absolute path: urlparse("C:\\path") -> scheme="c", path="\\path"
    if (
        os.name == "nt"
        and len(parsed.scheme) == 1
        and parsed.scheme.isalpha()
        and (parsed.path.startswith("\\") or parsed.path.startswith("/"))
    ):
        p = Path(url.strip()).resolve()
        if p.exists() and p.is_file():
            size = p.stat().st_size
            if size == 0:
                raise ValueError(f"Local file is empty: {p}")
            if size > _MAX_MEDIA_BYTES:
                raise ValueError("Downloaded media exceeds 10 MiB limit")
            return str(p)
    return None


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Media download deadline exceeded")
    return remaining


def _redacted_url(url: str) -> str:
    """Return a log-safe URL containing no path, query, or fragment."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or "<invalid-host>"
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            port = ""
        return f"{parsed.scheme}://{host}{port}"
    return "<local-media>"


def _set_response_timeout(response, timeout: float) -> None:
    """Apply an idle read timeout to urllib's underlying socket when present."""
    candidates = [
        getattr(
            getattr(getattr(response, "fp", None), "raw", None),
            "_sock",
            None,
        ),
        getattr(response, "_sock", None),
    ]
    for sock in candidates:
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(timeout)
            return


def _download_remote_to_path(
    url: str,
    local_file_path: Path,
    deadline: float | None = None,
) -> None:
    """
    Download url to local_file_path via wget, curl, or urllib. Raises on fail.
    """
    deadline = deadline or (time.monotonic() + _MEDIA_TOTAL_TIMEOUT)
    try:
        subprocess.run(
            [
                "wget",
                "-q",
                "--connect-timeout=10",
                "--max-redirect=3",
                "--max-filesize=10m",
                "-O",
                str(local_file_path),
                url,
            ],
            capture_output=True,
            timeout=_remaining_timeout(deadline),
            check=True,
        )
        if local_file_path.stat().st_size > _MAX_MEDIA_BYTES:
            raise ValueError("Downloaded media exceeds 10 MiB limit")
        logger.debug("Downloaded file via wget to: %s", local_file_path)
        return
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.debug("wget failed, trying curl (%s)", type(e).__name__)
    try:
        subprocess.run(
            [
                "curl",
                "-s",
                "--fail",
                "-L",
                "--connect-timeout",
                str(int(_MEDIA_CONNECT_TIMEOUT)),
                "--max-redirs",
                "3",
                "--max-filesize",
                str(_MAX_MEDIA_BYTES),
                "-o",
                str(local_file_path),
                url,
            ],
            capture_output=True,
            timeout=_remaining_timeout(deadline),
            check=True,
        )
        if local_file_path.stat().st_size > _MAX_MEDIA_BYTES:
            raise ValueError("Downloaded media exceeds 10 MiB limit")
        logger.debug("Downloaded file via curl to: %s", local_file_path)
        return
    except (subprocess.CalledProcessError, FileNotFoundError) as curl_err:
        logger.debug(
            "curl failed, trying urllib (%s)",
            type(curl_err).__name__,
        )
    try:
        with (
            urllib.request.urlopen(
                url,
                timeout=min(
                    _MEDIA_CONNECT_TIMEOUT,
                    _remaining_timeout(deadline),
                ),
            ) as response,
            open(local_file_path, "wb") as output,
        ):
            total = 0
            while True:
                read_timeout = min(
                    _MEDIA_READ_TIMEOUT,
                    _remaining_timeout(deadline),
                )
                _set_response_timeout(response, read_timeout)
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_MEDIA_BYTES:
                    raise ValueError("Downloaded media exceeds 10 MiB limit")
                output.write(chunk)
        logger.debug("Downloaded file via urllib to: %s", local_file_path)
    except TimeoutError:
        raise
    except Exception as urllib_err:
        logger.error(
            "wget, curl and urllib all failed for %s: %s",
            _redacted_url(url),
            urllib_err,
        )
        raise RuntimeError(
            "Failed to download file: wget, curl and urllib all failed",
        ) from urllib_err


def _guess_suffix_from_url_headers(
    url: str,
    deadline: float | None = None,
) -> Optional[str]:
    """
    HEAD request to get Content-Type and return a suffix like '.pdf'.
    Used to fix DingTalk download URLs that always return .file extension.
    Returns None on any failure (e.g. OSS forbids HEAD or returns no type).
    """
    try:
        req = urllib.request.Request(url, method="HEAD")
        timeout = _MEDIA_CONNECT_TIMEOUT
        if deadline is not None:
            timeout = min(timeout, _remaining_timeout(deadline))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            if not raw:
                return None
            suffix = mimetypes.guess_extension(raw)
            return suffix if suffix else None
    except Exception:
        return None


# Magic bytes (prefix) -> suffix for .file fallback when HEAD fails (e.g. OSS).
_MAGIC_SUFFIX: list[tuple[bytes, str]] = [
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),
    (b"PK\x05\x06", ".zip"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"\xd0\xcf\x11\xe0", ".doc"),  # MS Office (doc, xls, ppt)
    (b"RIFF", ".webp"),  # or .wav; webp has RIFF....WEBP
]


def _guess_suffix_from_file_content(path: Path) -> Optional[str]:
    """
    Guess file extension from magic bytes. Used when URL HEAD fails (e.g. OSS).
    Returns suffix like '.pdf' or None.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(32)
        for magic, suffix in _MAGIC_SUFFIX:
            if head.startswith(magic):
                return suffix
        return None
    except Exception:
        return None


def _download_file_from_base64_sync(
    base64_data: str,
    filename: Optional[str] = None,
    download_dir: str = "",
) -> str:
    """
    Save base64-encoded file data to local download directory.

    Args:
        base64_data: Base64-encoded file content.
        filename: The filename to save. If not provided, will generate one.
        download_dir: The directory to save files. Defaults to
            workspace_dir/downloads.

    Returns:
        The local file path.
    """
    started_at = time.monotonic()
    try:
        if len(base64_data) > 14 * 1024 * 1024:
            raise ValueError("Base64 media exceeds 10 MiB limit")
        file_content = base64.b64decode(base64_data)
        if len(file_content) > _MAX_MEDIA_BYTES:
            raise ValueError("Base64 media exceeds 10 MiB limit")

        download_path = Path(
            download_dir if download_dir else _default_download_dir(),
        )
        download_path.mkdir(parents=True, exist_ok=True)

        if not filename:
            file_hash = hashlib.md5(file_content).hexdigest()
            filename = f"file_{file_hash}"

        local_file_path = download_path / filename
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{local_file_path.name}.part-",
            dir=str(download_path),
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with open(temp_path, "wb") as f:
                f.write(file_content)
            os.replace(temp_path, local_file_path)
        finally:
            temp_path.unlink(missing_ok=True)

        logger.debug(
            "media_download_ms=%.1f media_source_type=base64 media_method=decode "
            "media_bytes=%d",
            (time.monotonic() - started_at) * 1000,
            len(file_content),
        )
        return str(local_file_path.absolute())

    except Exception as e:
        logger.error("Failed to download file from base64: %s", e)
        raise


async def download_file_from_base64(
    base64_data: str,
    filename: Optional[str] = None,
    download_dir: str = "",
) -> str:
    """Async boundary for bounded, blocking base64 media persistence."""
    return await _run_media_worker(
        _download_file_from_base64_sync,
        base64_data,
        filename,
        download_dir,
    )


def _download_file_from_url_sync(
    url: str,
    filename: Optional[str] = None,
    download_dir: str = "",
    deadline: float | None = None,
) -> str:
    """
    Download a file from URL to local download directory using wget or curl.

    Args:
        url (`str`):
            The URL of the file to download.
        filename (`str`, optional):
            The filename to save. If not provided, will extract from URL or
            generate a hash-based name.
        download_dir (`str`):
            The directory to save files. Defaults to
            workspace_dir/downloads.

    Returns:
        `str`:
            The local file path.
    """
    started_at = time.monotonic()
    try:
        parsed = urllib.parse.urlparse(url)
        local = _resolve_local_path(url, parsed)
        if local is not None:
            return local

        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Unsupported media URL scheme")

        download_path = Path(
            download_dir if download_dir else _default_download_dir(),
        )
        download_path.mkdir(parents=True, exist_ok=True)
        if not filename:
            url_filename = os.path.basename(parsed.path)
            filename = (
                url_filename
                if url_filename
                else f"file_{hashlib.md5(url.encode()).hexdigest()}"
            )
        local_file_path = download_path / filename
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{local_file_path.name}.part-",
            dir=str(download_path),
        )
        os.close(fd)
        temp_path = Path(temp_name)
        deadline = deadline or (time.monotonic() + _MEDIA_TOTAL_TIMEOUT)
        try:
            _download_remote_to_path(url, temp_path, deadline)
            if not temp_path.exists():
                raise FileNotFoundError("Downloaded file does not exist")
            size = temp_path.stat().st_size
            if size == 0:
                raise ValueError("Downloaded file is empty")
            if size > _MAX_MEDIA_BYTES:
                raise ValueError("Downloaded media exceeds 10 MiB limit")
            # DingTalk (and similar) return URLs that save as .file; replace
            # with real extension. Try HEAD first; if that fails, use magic.
            if local_file_path.suffix == ".file":
                real_suffix = _guess_suffix_from_url_headers(url, deadline)
                if not real_suffix:
                    real_suffix = _guess_suffix_from_file_content(temp_path)
                if real_suffix:
                    local_file_path = local_file_path.with_suffix(real_suffix)
                    logger.debug(
                        "Replaced .file with %s for %s",
                        real_suffix,
                        local_file_path,
                    )
            os.replace(temp_path, local_file_path)
        finally:
            temp_path.unlink(missing_ok=True)
        logger.debug(
            "media_download_ms=%.1f media_source_type=url media_method=worker "
            "media_bytes=%d",
            (time.monotonic() - started_at) * 1000,
            size,
        )
        return str(local_file_path.absolute())
    except subprocess.TimeoutExpired as e:
        logger.error("Download timeout for %s", _redacted_url(url))
        raise TimeoutError("Download timeout") from e
    except Exception as e:
        logger.error(
            "Failed to download file from %s: %s",
            _redacted_url(url),
            e,
        )
        raise


def _prepare_remote_target(
    url: str,
    filename: Optional[str],
    download_dir: str,
) -> tuple[Path, Path, float] | str:
    """Prepare a remote destination in a worker-safe synchronous boundary."""
    parsed = urllib.parse.urlparse(url)
    local = _resolve_local_path(url, parsed)
    if local is not None:
        return local
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Unsupported media URL scheme")
    target_dir = Path(download_dir or _default_download_dir())
    target_dir.mkdir(parents=True, exist_ok=True)
    if not filename:
        url_filename = os.path.basename(parsed.path)
        filename = (
            url_filename
            if url_filename
            else f"file_{hashlib.md5(url.encode()).hexdigest()}"
        )
    final_path = target_dir / filename
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{final_path.name}.part-",
        dir=str(target_dir),
    )
    os.close(fd)
    return (
        final_path,
        Path(temp_name),
        time.monotonic() + _MEDIA_TOTAL_TIMEOUT,
    )


async def download_file_from_url(
    url: str,
    filename: Optional[str] = None,
    download_dir: str = "",
) -> str:
    """Download media without blocking the event loop.

    HTTP(S) uses streaming ``httpx`` first; the legacy wget/curl/urllib
    implementation remains an isolated worker fallback for compatibility.
    """
    loop = asyncio.get_running_loop()
    preparation = asyncio.create_task(
        asyncio.to_thread(
            _prepare_remote_target,
            url,
            filename,
            download_dir,
        ),
    )
    try:
        prepared = await asyncio.shield(preparation)
    except asyncio.CancelledError:

        def _cleanup_prepared(done: asyncio.Future) -> None:
            if done.cancelled():
                return
            try:
                result = done.result()
            except Exception:
                return
            if isinstance(result, tuple):
                temp_path = result[1]

                async def _cleanup() -> None:
                    try:
                        await asyncio.to_thread(temp_path.unlink, True)
                    except OSError:
                        pass

                if not loop.is_closed():
                    loop.create_task(_cleanup())

        preparation.add_done_callback(_cleanup_prepared)
        raise
    if isinstance(prepared, str):
        return prepared
    final_path, temp_path, deadline = prepared
    started_at = time.monotonic()
    queue_ms = await _run_async_media_slot_with_cleanup(temp_path)
    try:
        content_type = await download_http_to_path(
            url,
            temp_path,
            deadline=deadline,
            max_bytes=_MAX_MEDIA_BYTES,
        )
        size = await asyncio.to_thread(lambda: temp_path.stat().st_size)
        if size == 0:
            raise ValueError("Downloaded file is empty")
        if final_path.suffix == ".file":
            suffix = mimetypes.guess_extension(content_type or "")
            if not suffix:
                suffix = await asyncio.to_thread(
                    _guess_suffix_from_file_content,
                    temp_path,
                )
            if suffix:
                final_path = final_path.with_suffix(suffix)
        await asyncio.to_thread(os.replace, temp_path, final_path)
        logger.debug(
            "media_download_ms=%.1f media_source_type=url media_method=httpx "
            "media_bytes=%d media_worker_queue_ms=%.1f",
            (time.monotonic() - started_at) * 1000,
            size,
            queue_ms,
        )
        return str(final_path.absolute())
    except (AsyncDownloadHTTPError, AsyncDownloadPolicyError) as exc:
        await asyncio.to_thread(temp_path.unlink, True)
        error_kind = (
            "http_status" if isinstance(exc, AsyncDownloadHTTPError) else "http_policy"
        )
        logger.info(
            "media_timeout=false media_error=%s media_source_type=url " "error_type=%s",
            error_kind,
            type(exc).__name__,
        )
        raise
    except (AsyncDownloadError, TimeoutError):
        # Keep the legacy command-line/urllib path as a bounded worker
        # fallback, but never execute it on the event-loop thread.
        await asyncio.to_thread(temp_path.unlink, True)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _get_media_executor(),
            functools.partial(
                _download_file_from_url_sync,
                url,
                filename,
                download_dir,
                deadline,
            ),
        )
    finally:
        _media_slots.release()
        await asyncio.to_thread(temp_path.unlink, True)
