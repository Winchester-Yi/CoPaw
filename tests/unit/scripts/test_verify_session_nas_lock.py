# -*- coding: utf-8 -*-
"""Operator checks must exercise publication rather than pass vacuously."""

from contextlib import ExitStack
import errno
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts/verify_session_nas_lock.py"
)


def verifier():
    spec = importlib.util.spec_from_file_location("nas_verifier", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hard_link_publication_preserves_existing_target(tmp_path):
    verifier()._verify_hard_links(tmp_path)
    assert not list(tmp_path.iterdir())


def test_indexed_writer_publishes_initial_json_before_reader_barrier(
    tmp_path,
    monkeypatch,
):
    module = verifier()
    waited = []

    def wait(path):
        waited.append(path.name)
        if path.name == ".verification.reader-ready":
            assert json.loads(
                (tmp_path / ".verification.json").read_bytes(),
            ) == {
                "revision": 0,
            }

    monkeypatch.setattr(module, "_wait_for", wait)
    module._verify_revisions(tmp_path, 3, reader_barrier=True, cleanup=False)
    assert waited == [
        ".verification.reader-ready",
        ".verification.read-observed",
    ]
    assert json.loads((tmp_path / ".verification.json").read_bytes()) == {
        "revision": 3,
    }


def test_reader_does_not_acknowledge_midpoint_with_an_older_sample(
    tmp_path,
    monkeypatch,
):
    module = verifier()
    for name in ("ready", "released", "revisions-started", "read-window"):
        module._marker(tmp_path, name).touch()
    original_acquire = module._acquire
    acquisitions = []

    def acquire(path, non_blocking):
        if not acquisitions:
            acquisitions.append(True)
            raise BlockingIOError(errno.EAGAIN, "held by the other worker")
        return original_acquire(path, non_blocking)

    monkeypatch.setattr(module, "_acquire", acquire)
    state_path = tmp_path / ".verification.json"
    state_path.write_text('{"revision":0}')
    original_touch = Path.touch
    acknowledgements = []

    def touch(path, *args, **kwargs):
        if path.name == ".verification.read-observed":
            revision = json.loads(state_path.read_bytes())["revision"]
            assert revision >= 15
            acknowledgements.append(revision)
        return original_touch(path, *args, **kwargs)

    sleeps = []

    def sleep(_):
        sleeps.append(True)
        revision = 15 if len(sleeps) == 1 else 30
        state_path.write_text(json.dumps({"revision": revision}))
        if revision == 30:
            module._marker(tmp_path, "revisions-done").touch()

    monkeypatch.setattr(Path, "touch", touch)
    monkeypatch.setattr(module.time, "sleep", sleep)
    report = module._verify_indexed_contender(tmp_path, 30)
    assert acknowledgements == [15]
    assert report["read_samples"] == 2


def test_two_independent_workers_report_real_reads_and_final_revision(
    tmp_path,
):
    with ExitStack() as stack:
        processes = [
            stack.enter_context(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPT),
                        str(tmp_path),
                        "--mode",
                        "indexed",
                        "--worker-index",
                        str(index),
                        "--revisions",
                        "30",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
            )
            for index in range(2)
        ]
        try:
            reports = []
            for process in processes:
                output, error = process.communicate(timeout=40)
                assert process.returncode == 0, error
                reports.append(json.loads(output.splitlines()[-1]))
            assert all(report["passed"] for report in reports)
            assert reports[1]["read_samples"] > 0
            assert reports[1]["final_revision"] == 30
            assert all(report["hard_links"] for report in reports)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
