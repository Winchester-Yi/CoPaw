#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the NAS session lock and atomic JSON revision contract.

This is an operator-side check. It does not touch application session files;
it only creates ``.verification.json`` below the directory supplied by the
operator.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import multiprocessing as mp
import os
import signal
import tempfile
import time
from pathlib import Path


def _lock_path(root: Path) -> Path:
    return root / ".verification.json.lock"


def _marker(root: Path, name: str) -> Path:
    return root / f".verification.{name}"


def _wait_for(path: Path, timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for {path}")
        time.sleep(0.05)


def _crash_holder(root: str) -> None:
    with _acquire(_lock_path(Path(root)), non_blocking=False):
        _marker(Path(root), "ready").touch()
        time.sleep(30)


def _acquire(path: Path, non_blocking: bool) -> object:
    handle = path.open("a+")
    flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if non_blocking else 0)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BaseException:
        handle.close()
        raise
    return handle


def _holder(
    root: str,
    ready: mp.synchronize.Event,
    release: mp.synchronize.Event,
) -> None:
    with _acquire(_lock_path(Path(root)), non_blocking=False):
        ready.set()
        release.wait(30)


def _verify_contention(root: Path) -> None:
    ready = mp.Event()
    release = mp.Event()
    process = mp.Process(target=_holder, args=(str(root), ready, release))
    process.start()
    try:
        if not ready.wait(10):
            raise RuntimeError("lock holder did not enter within 10 seconds")
        try:
            with _acquire(_lock_path(root), non_blocking=True):
                raise RuntimeError("second worker unexpectedly acquired lock")
        except OSError as exc:
            if exc.errno not in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EWOULDBLOCK,
            }:
                raise
        release.set()
        process.join(10)
        if process.exitcode != 0:
            raise RuntimeError(f"lock holder exited with {process.exitcode}")
        with _acquire(_lock_path(root), non_blocking=True):
            pass
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()


def _verify_indexed_holder(root: Path, revisions: int) -> None:
    for name in (
        "ready",
        "contender-done",
        "released",
        "handoff-acquired",
        "passed",
        "reader-ready",
        "read-window",
        "read-observed",
    ):
        _marker(root, name).unlink(missing_ok=True)
    holder = mp.Process(target=_crash_holder, args=(str(root),))
    holder.start()
    try:
        _wait_for(_marker(root, "ready"))
        _wait_for(_marker(root, "contender-done"))
        holder.kill()
        holder.join(10)
        if holder.exitcode not in {-signal.SIGKILL, -9}:
            raise RuntimeError(
                f"crashed lock holder exited with {holder.exitcode}",
            )
        _marker(root, "released").touch()
        _wait_for(_marker(root, "handoff-acquired"))
        _verify_revisions(root, revisions, reader_barrier=True, cleanup=False)
        _wait_for(_marker(root, "reader-done"))
        _wait_for(_marker(root, "passed"))
        (root / ".verification.json").unlink(missing_ok=True)
        _lock_path(root).unlink(missing_ok=True)
    finally:
        if holder.is_alive():
            holder.kill()
            holder.join()
        for name in (
            "ready",
            "contender-done",
            "released",
            "handoff-acquired",
            "passed",
            "revisions-started",
            "revisions-done",
            "reader-done",
            "reader-ready",
            "read-window",
            "read-observed",
        ):
            _marker(root, name).unlink(missing_ok=True)


def _verify_indexed_contender(root: Path, revisions: int) -> dict[str, int]:
    _wait_for(_marker(root, "ready"))
    try:
        with _acquire(_lock_path(root), non_blocking=True):
            raise RuntimeError("second Pod unexpectedly acquired lock")
    except OSError as exc:
        if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            raise
    _marker(root, "contender-done").touch()
    _wait_for(_marker(root, "released"))
    with _acquire(_lock_path(root), non_blocking=True):
        pass
    _marker(root, "handoff-acquired").touch()
    _marker(root, "passed").touch()
    _wait_for(_marker(root, "revisions-started"))
    last_revision = -1
    samples = 0
    while not _marker(root, "revisions-done").exists():
        with (root / ".verification.json").open("r", encoding="utf-8") as file:
            state = json.load(file)
        revision = state.get("revision")
        if (
            type(revision) is not int
            or not last_revision <= revision <= revisions
        ):
            raise RuntimeError(f"invalid revision state: {state!r}")
        last_revision = revision
        samples += 1
        _marker(root, "reader-ready").touch()
        if (
            revision >= max(1, revisions // 2)
            and _marker(root, "read-window").exists()
        ):
            _marker(root, "read-observed").touch()
        time.sleep(0.01)
    final = json.loads((root / ".verification.json").read_bytes())
    if samples == 0 or final != {"revision": revisions}:
        raise RuntimeError("reader missed publication or final revision")
    _marker(root, "reader-done").touch()
    return {"read_samples": samples, "final_revision": revisions}


def _verify_hard_links(root: Path) -> None:
    """Exercise exclusive publication and survival after staging removal."""
    with tempfile.TemporaryDirectory(
        prefix=".verification.links-",
        dir=root,
    ) as path:
        staging = Path(path) / "staging.json"
        published = Path(path) / "published.json"
        _atomic_commit(staging, {"revision": 1})
        os.link(staging, published)
        try:
            os.link(staging, published)
        except FileExistsError:
            pass
        else:
            raise RuntimeError(
                "hard-link publication replaced an existing target",
            )
        staging.unlink()
        directory_fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if json.loads(published.read_bytes()) != {"revision": 1}:
            raise RuntimeError("hard-link publication lost staged data")


def _atomic_commit(path: Path, state: dict[str, int]) -> None:
    encoded = json.dumps(state, separators=(",", ":"), sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".verification.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(encoded)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def _verify_revisions(
    root: Path,
    count: int,
    *,
    reader_barrier: bool = False,
    cleanup: bool = True,
) -> None:
    path = root / ".verification.json"
    lock = _lock_path(root)
    with _acquire(lock, non_blocking=False):
        if reader_barrier:
            _atomic_commit(path, {"revision": 0})
            _marker(root, "revisions-started").touch()
            _wait_for(_marker(root, "reader-ready"))
        for revision in range(1, count + 1):
            state = {"revision": revision}
            _atomic_commit(path, state)
            with path.open("r", encoding="utf-8") as file:
                loaded = json.load(file)
            if loaded != state:
                raise RuntimeError(
                    f"unexpected state at revision {revision}: {loaded!r}",
                )
            if reader_barrier and revision == max(1, count // 2):
                _marker(root, "read-window").touch()
                _wait_for(_marker(root, "read-observed"))
        if reader_barrier:
            _marker(root, "revisions-done").touch()
    if cleanup:
        path.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_dir", type=Path)
    parser.add_argument(
        "--mode",
        choices=("all", "contention", "revisions", "indexed"),
        default="all",
    )
    parser.add_argument("--revisions", type=int, default=1000)
    parser.add_argument("--worker-index", type=int, default=None)
    args = parser.parse_args()
    if args.revisions <= 0:
        parser.error("--revisions must be positive")
    args.session_dir.mkdir(parents=True, exist_ok=True)
    _verify_hard_links(args.session_dir)
    report = {
        "passed": True,
        "mode": args.mode,
        "host": os.uname().nodename,
        "hard_links": True,
        "revisions": args.revisions,
    }
    if args.mode in {"all", "contention"}:
        _verify_contention(args.session_dir)
    if args.mode in {"all", "revisions"}:
        _verify_revisions(args.session_dir, args.revisions)
    if args.mode == "indexed":
        worker_index = args.worker_index
        if worker_index is None:
            worker_index = int(os.environ.get("JOB_COMPLETION_INDEX", "-1"))
        if worker_index == 0:
            _verify_indexed_holder(args.session_dir, args.revisions)
        elif worker_index == 1:
            report.update(
                _verify_indexed_contender(args.session_dir, args.revisions),
            )
        else:
            parser.error("indexed mode requires worker index 0 or 1")
        report["worker_index"] = worker_index
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
