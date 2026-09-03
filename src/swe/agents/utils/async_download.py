# -*- coding: utf-8 -*-
"""Bounded streaming HTTP downloads for media blocks."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

_MAX_REDIRECTS = 3


class AsyncDownloadError(RuntimeError):
    """Raised when a streaming HTTP download cannot be completed safely."""


class AsyncDownloadPolicyError(AsyncDownloadError):
    """Raised when a URL violates the media download policy."""


class AsyncDownloadHTTPError(AsyncDownloadError):
    """Raised for an HTTP response status that must not use legacy fallback."""


_clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}
_clients_lock = threading.Lock()


async def _client_for_loop() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is not None:
        return client
    with _clients_lock:
        client = _clients.get(loop)
        if client is None:
            timeout = httpx.Timeout(
                connect=10.0,
                read=30.0,
                write=30.0,
                pool=10.0,
            )
            client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=4,
                    max_keepalive_connections=4,
                ),
            )
            _clients[loop] = client
    return client


def _validate_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AsyncDownloadPolicyError("Unsupported media URL scheme")


async def download_http_to_path(
    url: str,
    destination: Path,
    *,
    deadline: float,
    max_bytes: int,
) -> Optional[str]:
    """Stream an HTTP(S) response to *destination*.

    Returns the response content type (without parameters), if provided.
    Redirects are followed manually so each hop is revalidated and counted.
    """
    current = url
    try:
        client = await _client_for_loop()
        for redirect_count in range(_MAX_REDIRECTS + 1):
            _validate_http_url(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Media download deadline exceeded")
            try:
                async with asyncio.timeout(remaining):
                    async with client.stream("GET", current) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location or redirect_count >= _MAX_REDIRECTS:
                                raise AsyncDownloadPolicyError(
                                    "Too many media redirects",
                                )
                            current = urljoin(current, location)
                            _validate_http_url(current)
                            continue
                        response.raise_for_status()
                        raw_length = response.headers.get("content-length")
                        if raw_length:
                            try:
                                content_length = int(raw_length)
                            except ValueError:
                                content_length = None
                            if (
                                content_length is not None
                                and content_length > max_bytes
                            ):
                                raise ValueError(
                                    "Downloaded media exceeds 10 MiB limit",
                                )
                        total = 0
                        output = await asyncio.to_thread(
                            destination.open,
                            "wb",
                        )
                        try:
                            async for chunk in response.aiter_bytes(64 * 1024):
                                total += len(chunk)
                                if total > max_bytes:
                                    raise ValueError(
                                        "Downloaded media exceeds 10 MiB limit",
                                    )
                                await asyncio.to_thread(output.write, chunk)
                        finally:
                            await asyncio.to_thread(output.close)
                        content_type = (
                            (response.headers.get("content-type") or "")
                            .split(";", 1)[0]
                            .strip()
                        )
                        return content_type or None
            except httpx.TimeoutException as exc:
                raise TimeoutError("Media download timeout") from exc
            except httpx.HTTPStatusError as exc:
                raise AsyncDownloadHTTPError(
                    f"HTTP media download returned {exc.response.status_code}",
                ) from exc
            except httpx.HTTPError as exc:
                raise AsyncDownloadError(
                    "HTTP media download failed",
                ) from exc
        raise AsyncDownloadPolicyError("Too many media redirects")
    except BaseException:
        await asyncio.to_thread(destination.unlink, True)
        raise
