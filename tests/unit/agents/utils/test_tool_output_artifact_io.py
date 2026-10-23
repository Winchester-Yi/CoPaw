# -*- coding: utf-8 -*-
"""Bounded historical artifact reads and cleanup scheduling."""

import asyncio
from collections import OrderedDict
from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from swe.agents.utils import tool_output_compaction as compaction


@pytest.fixture(autouse=True)
def isolated_caches(monkeypatch):
    monkeypatch.setattr(
        compaction,
        "_DISPLAY_CACHE",
        OrderedDict(),
        raising=False,
    )
    monkeypatch.setattr(
        compaction,
        "_DISPLAY_CACHE_BYTES_USED",
        0,
        raising=False,
    )
    monkeypatch.setattr(
        compaction,
        "_CLEANUP_NEXT_AT",
        OrderedDict(),
        raising=False,
    )


def _artifact(tmp_path, content):
    path = tmp_path / "tool_result" / ("a" * 32 + ".txt")
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    notice = compaction._truncation_notice(
        original_bytes=len(content),
        retained_bytes=512,
        artifact_reference=f"tool_result/{path.name}",
        max_bytes=512,
    )
    return path, "previous display" + notice


def _render(tmp_path, display, budget=512):
    return compaction._compact_display_text(
        display,
        max_bytes=budget,
        artifact_dir=tmp_path / "tool_result",
        workspace_dir=tmp_path,
    )


@pytest.fixture
def reads(monkeypatch):
    observed = []
    original_open = Path.open

    class CountedFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            data = self.stream.read(size)
            observed.append((size, len(data)))
            return data

    def counted_open(path, mode="r", *args, **kwargs):
        # Ownership transfers to CountedFile or the caller's with statement.
        # pylint: disable=consider-using-with
        stream = original_open(
            path,
            mode,
            *args,
            **kwargs,
        )
        return CountedFile(stream) if mode == "rb" else stream

    monkeypatch.setattr(Path, "open", counted_open)
    return observed


@pytest.mark.parametrize(
    "content",
    [
        b"long line" * 300000,
        ("中文🙂\n<<<TRUNCATED>>>\n" * 100000).encode(),
    ],
    ids=["long-ascii-line", "unicode-lines-marker"],
)
def test_historical_read_is_bounded_and_preserves_display(
    tmp_path,
    reads,
    content,
):
    _, display = _artifact(tmp_path, content)
    rendered = _render(tmp_path, display)
    notice = compaction._truncation_notice(
        original_bytes=len(content),
        retained_bytes=512,
        artifact_reference=f"tool_result/{'a' * 32}.txt",
        max_bytes=512,
    ).encode()
    assert rendered == compaction._compacted_display(
        content,
        notice=notice,
        max_bytes=512,
    )
    assert sum(size for _, size in reads) <= 768
    assert all(requested >= 0 for requested, _ in reads)


def test_same_signature_and_budget_reuses_display(tmp_path, reads):
    _, display = _artifact(tmp_path, b"source line\n" * 10000)
    first = _render(tmp_path, display)
    reads.clear()
    assert _render(tmp_path, first) == first
    assert reads == []
    smaller = _render(tmp_path, first, 256)
    assert len(smaller.encode()) == 256
    assert reads


def test_file_change_and_deletion_invalidate_display(tmp_path, reads):
    path, display = _artifact(tmp_path, b"a" * 10000)
    first = _render(tmp_path, display)
    previous = path.stat()
    path.write_bytes(b"b" * 10000)
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    reads.clear()
    updated = _render(tmp_path, first)
    assert updated != first
    assert updated.startswith("b")
    assert reads
    path.unlink()
    assert _render(tmp_path, updated, 256) == updated


def test_cache_isolated_by_workspace(tmp_path):
    _, first = _artifact(tmp_path / "alice", b"a" * 10000)
    _, second = _artifact(tmp_path / "bob", b"b" * 10000)
    assert _render(tmp_path / "alice", first).startswith("a")
    assert _render(tmp_path / "bob", second).startswith("b")


@pytest.mark.parametrize("entry_limit, byte_limit", [(2, 100000), (128, 600)])
def test_display_cache_evicts_by_entries_and_bytes(
    tmp_path,
    reads,
    monkeypatch,
    entry_limit,
    byte_limit,
):
    monkeypatch.setattr(
        compaction,
        "_DISPLAY_CACHE_MAX_ENTRIES",
        entry_limit,
        raising=False,
    )
    monkeypatch.setattr(
        compaction,
        "_DISPLAY_CACHE_MAX_BYTES",
        byte_limit,
        raising=False,
    )
    _, display = _artifact(tmp_path, b"a" * 10000)
    first = _render(tmp_path, display, 256)
    _render(tmp_path, display, 384)
    _render(tmp_path, display, 512)
    assert 0 < len(compaction._DISPLAY_CACHE) <= entry_limit
    assert compaction._DISPLAY_CACHE_BYTES_USED <= byte_limit
    reads.clear()
    assert _render(tmp_path, first, 256) == first
    assert reads  # The oldest budget was evicted.


def test_oversized_display_is_returned_without_caching(
    tmp_path,
    reads,
    monkeypatch,
):
    monkeypatch.setattr(
        compaction,
        "_DISPLAY_CACHE_MAX_BYTES",
        128,
        raising=False,
    )
    _, display = _artifact(tmp_path, b"a" * 10000)
    first = _render(tmp_path, display)
    assert len(first.encode()) == 512
    reads.clear()
    assert _render(tmp_path, first) == first
    assert reads
    assert not compaction._DISPLAY_CACHE


def test_artifact_read_failure_keeps_previous_display(tmp_path, monkeypatch):
    _, display = _artifact(tmp_path, b"a" * 10000)

    def failed_open(*args, **kwargs):
        raise OSError("artifact is unreadable")

    monkeypatch.setattr(Path, "open", failed_open)
    assert _render(tmp_path, display) == display


@pytest.mark.parametrize("budget", [96, 128, 511, 1024])
@pytest.mark.parametrize(
    "source",
    ["中文🙂long line", "first\nsecond\n", "<<<TRUNCATED>>>\n"],
)
def test_bounded_excerpt_matches_original_for_budget_changes(
    tmp_path,
    budget,
    source,
):
    content = (source * 1000).encode()
    path, display = _artifact(tmp_path, content)
    notice = compaction._truncation_notice(
        original_bytes=len(content),
        retained_bytes=budget,
        artifact_reference=f"tool_result/{path.name}",
        max_bytes=budget,
    ).encode()
    assert _render(tmp_path, display, budget) == compaction._compacted_display(
        content,
        notice=notice,
        max_bytes=budget,
    )


def test_changed_file_during_read_is_not_cached(tmp_path, monkeypatch):
    path, display = _artifact(tmp_path, b"a" * 10000)
    original_open = Path.open

    class ChangingFile:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            data = self.stream.read(size)
            path.write_bytes(b"b" * 10000)
            return data

    @contextmanager
    def changing_open(candidate, mode="r", *args, **kwargs):
        with original_open(candidate, mode, *args, **kwargs) as stream:
            yield ChangingFile(stream) if mode == "rb" else stream

    monkeypatch.setattr(Path, "open", changing_open)
    assert _render(tmp_path, display) == display
    assert not compaction._DISPLAY_CACHE


def test_cached_display_does_not_follow_replaced_external_symlink(tmp_path):
    path, display = _artifact(tmp_path, b"a" * 10000)
    first = _render(tmp_path, display)
    private = tmp_path / "private.txt"
    private.write_bytes(b"private" * 10000)
    path.unlink()
    path.symlink_to(private)
    assert _render(tmp_path, first) == first


def test_external_tool_result_directory_is_not_read_or_cached(tmp_path, reads):
    _, display = _artifact(tmp_path / "outside", b"private-content" * 10000)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tool_result").symlink_to(
        tmp_path / "outside" / "tool_result",
    )
    assert _render(workspace, display) == display
    assert reads == []
    assert not compaction._DISPLAY_CACHE


def test_inside_directory_alias_retains_resolved_reference(tmp_path):
    path, display = _artifact(tmp_path / "nested", b"a" * 10000)
    (tmp_path / "tool_result").symlink_to(path.parent)
    nested_display = _render(tmp_path / "nested", display)
    root_display = _render(tmp_path, display)
    assert f"read_file tool_result/{path.name}" in nested_display
    assert f"read_file nested/tool_result/{path.name}" in root_display


@pytest.mark.asyncio
async def test_cleanup_records_are_bounded_and_invalid_retention_still_raises(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(compaction, "_CLEANUP_MAX_ENTRIES", 2)
    for index in range(3):
        await compaction.compact_tool_result_messages_async(
            [],
            old_max_bytes=512,
            recent_max_bytes=512,
            recent_n=1,
            artifact_dir=tmp_path / str(index),
            workspace_dir=tmp_path,
            retention_days=7,
        )
    assert len(compaction._CLEANUP_NEXT_AT) == 2
    with pytest.raises(ValueError, match="retention_days must be positive"):
        await compaction.compact_tool_result_messages_async(
            [],
            old_max_bytes=512,
            recent_max_bytes=512,
            recent_n=1,
            artifact_dir=tmp_path,
            workspace_dir=tmp_path,
            retention_days=0,
        )


@pytest.mark.asyncio
async def test_cleanup_scans_once_per_interval_and_retention(
    tmp_path,
    monkeypatch,
):
    clock = [100.0]
    monkeypatch.setattr(
        compaction,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            time=lambda: 100.0,
        ),
    )
    scans = []
    monkeypatch.setattr(
        compaction,
        "cleanup_expired_artifacts",
        lambda path, **kw: scans.append((path, kw)),
    )

    async def compact(retention=7):
        await compaction.compact_tool_result_messages_async(
            [],
            old_max_bytes=512,
            recent_max_bytes=512,
            recent_n=1,
            artifact_dir=tmp_path,
            workspace_dir=tmp_path,
            retention_days=retention,
        )

    await asyncio.gather(compact(), compact())
    assert len(scans) == 1
    clock[0] = 399.0
    await compact()
    assert len(scans) == 1
    clock[0] = 400.0
    await compact()
    assert len(scans) == 2
    await compact(3)
    assert len(scans) == 3
