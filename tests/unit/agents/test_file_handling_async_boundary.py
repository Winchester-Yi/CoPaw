# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import base64
import threading
import time
from pathlib import Path

import pytest

from swe.agents.utils import file_handling


@pytest.mark.asyncio
async def test_base64_persistence_runs_outside_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = file_handling._download_file_from_base64_sync

    def slow_sync(*args, **kwargs):
        time.sleep(0.08)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        file_handling,
        "_download_file_from_base64_sync",
        slow_sync,
    )
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        for _ in range(4):
            ticks += 1
            await asyncio.sleep(0.02)

    payload = base64.b64encode(b"hello").decode()
    result, _ = await asyncio.gather(
        file_handling.download_file_from_base64(
            payload,
            "x.txt",
            str(tmp_path),
        ),
        heartbeat(),
    )

    assert ticks == 4
    assert Path(result).read_bytes() == b"hello"
    assert not list(tmp_path.glob("*.part-*"))


@pytest.mark.asyncio
async def test_base64_size_limit_is_enforced(tmp_path: Path) -> None:
    payload = base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode()
    with pytest.raises(ValueError, match="10 MiB"):
        await file_handling.download_file_from_base64(
            payload,
            "oversized.bin",
            str(tmp_path),
        )


@pytest.mark.asyncio
async def test_remote_download_rejects_non_http_scheme(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported media URL scheme"):
        await file_handling.download_file_from_url(
            "ftp://example.test/file.bin",
            download_dir=str(tmp_path),
        )


@pytest.mark.asyncio
async def test_local_media_size_limit_is_enforced(tmp_path: Path) -> None:
    local_path = tmp_path / "large.bin"
    local_path.write_bytes(b"x" * (10 * 1024 * 1024 + 1))

    with pytest.raises(ValueError, match="10 MiB"):
        await file_handling.download_file_from_url(
            local_path.as_uri(),
            download_dir=str(tmp_path / "downloads"),
        )


@pytest.mark.asyncio
async def test_http_policy_error_does_not_fallback_to_legacy_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_called = False

    async def reject_policy(*_args, **_kwargs):
        raise file_handling.AsyncDownloadPolicyError("redirect policy")

    def legacy_fallback(*_args, **_kwargs):
        nonlocal fallback_called
        fallback_called = True
        raise AssertionError("policy errors must not use legacy fallback")

    monkeypatch.setattr(file_handling, "download_http_to_path", reject_policy)
    monkeypatch.setattr(
        file_handling,
        "_download_file_from_url_sync",
        legacy_fallback,
    )

    with pytest.raises(file_handling.AsyncDownloadPolicyError):
        await file_handling.download_file_from_url(
            "https://example.test/redirect",
            download_dir=str(tmp_path),
        )

    assert not fallback_called
    assert not list(tmp_path.glob("*.part-*"))


@pytest.mark.asyncio
async def test_cancelled_remote_preparation_cleans_partial_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = file_handling._prepare_remote_target
    started = threading.Event()
    release = threading.Event()

    def slow_prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        started.set()
        release.wait(timeout=2)
        return result

    monkeypatch.setattr(file_handling, "_prepare_remote_target", slow_prepare)
    task = asyncio.create_task(
        file_handling.download_file_from_url(
            "https://example.test/cancelled.bin",
            download_dir=str(tmp_path),
        ),
    )
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.1)
    assert not list(tmp_path.glob("*.part-*"))


@pytest.mark.asyncio
async def test_cancelled_media_slot_waiter_cleans_prepared_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def blocked_slot() -> float:
        started.set()
        await asyncio.Future()
        return 0.0

    monkeypatch.setattr(file_handling, "_run_async_media_slot", blocked_slot)
    task = asyncio.create_task(
        file_handling.download_file_from_url(
            "https://example.test/cancelled-slot.bin",
            download_dir=str(tmp_path),
        ),
    )
    await started.wait()
    assert list(tmp_path.glob("*.part-*"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.glob("*.part-*"))


@pytest.mark.asyncio
async def test_media_worker_executor_is_bounded(monkeypatch) -> None:
    active = 0
    peak = 0

    def worker() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        time.sleep(0.03)
        active -= 1

    await asyncio.gather(
        *(file_handling._run_media_worker(worker) for _ in range(8)),
    )
    assert peak <= file_handling._MEDIA_WORKER_COUNT


@pytest.mark.asyncio
async def test_cancelled_media_slot_waiter_does_not_consume_slot() -> None:
    acquired = [
        file_handling._media_slots.acquire(  # pylint: disable=consider-using-with
            blocking=False,
        )
        for _ in range(file_handling._MEDIA_WORKER_COUNT)
    ]
    assert all(acquired)
    try:
        waiter = asyncio.create_task(
            file_handling._run_media_worker(lambda: None),
        )
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        for _ in acquired:
            file_handling._media_slots.release()

    reacquired = [
        file_handling._media_slots.acquire(  # pylint: disable=consider-using-with
            blocking=False,
        )
        for _ in range(file_handling._MEDIA_WORKER_COUNT)
    ]
    assert all(reacquired)
    for _ in reacquired:
        file_handling._media_slots.release()


@pytest.mark.asyncio
async def test_cancelled_running_media_worker_holds_slot_until_done() -> None:
    started = threading.Event()
    release_worker = threading.Event()

    def worker() -> None:
        started.set()
        release_worker.wait(timeout=2)

    task = asyncio.create_task(file_handling._run_media_worker(worker))
    await asyncio.to_thread(started.wait, 1)
    slots_while_running = file_handling._media_slots._value
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert file_handling._media_slots._value == slots_while_running
    release_worker.set()
    await asyncio.sleep(0.05)
    assert file_handling._media_slots._value == slots_while_running + 1
