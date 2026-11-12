# -*- coding: utf-8 -*-
"""Progress reports must remain out of stdout and observe real IO drain."""

import asyncio
import io
import json
import threading

import pytest

from swe.app.conversation_storage.migration.parallel import (
    MigrationIO,
    MigrationCoordinator,
)
from swe.app.conversation_storage.migration.progress import (
    MigrationProgress,
    current_progress,
)


async def test_progress_reports_counters_and_no_sql_or_parameters(
    tmp_path,
    capsys,
):
    output = io.StringIO()
    progress = MigrationProgress(
        "import",
        "migration",
        stream=output,
        interval=0.001,
    )
    owner = asyncio.current_task()

    async def sql(_secret):
        assert asyncio.current_task() is owner
        await asyncio.sleep(0.002)

    async def run():
        async with MigrationIO(1) as executor:

            async def worker(value, request):
                await executor.call(lambda: None)
                await request("fixed_domain_operation", value)

            await MigrationCoordinator(
                {"fixed_domain_operation": sql},
                workers=1,
            ).run(["private body"], worker)

    with progress.activate():
        await progress.run(run())
    assert current_progress() is None
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(records) >= 2
    assert records[-1]["io_drained"] is True
    assert records[-1]["io_calls"] == 1
    assert records[-1]["completed"] == 1
    assert records[-1]["sql_seconds"] > 0
    assert "private body" not in output.getvalue()
    assert capsys.readouterr().out == ""
    progress.save_failure(
        tmp_path / "manifest.json",
        ValueError("private body"),
    )
    failure = json.loads((tmp_path / "manifest.json.failure.json").read_text())
    assert failure["error_type"] == "ValueError"
    assert failure["io_drained"] is True
    assert "private body" not in json.dumps(failure)


async def test_cancelled_io_is_not_reported_drained_until_thread_finishes():
    progress = MigrationProgress("import", "migration", stream=io.StringIO())
    started, release = threading.Event(), threading.Event()

    def blocked():
        started.set()
        assert release.wait(timeout=5)

    with progress.activate():
        async with MigrationIO(1) as executor:
            task = asyncio.create_task(executor.call(blocked))
            while not started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            task.cancel()
            assert progress.snapshot()["io_drained"] == "unknown"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert progress.snapshot()["io_drained"] is True


def test_disk_peak_counts_overlapping_spools():
    progress = MigrationProgress("verify", "migration", stream=io.StringIO())
    progress.disk_change(10)
    progress.disk_change(20)
    progress.disk_change(-10)
    assert progress.snapshot()["temp_peak_bytes"] == 30


async def test_progress_output_failure_preserves_operation_error():
    class BrokenOutput:
        def write(self, _value):
            raise BrokenPipeError("closed stderr")

    progress = MigrationProgress("verify", "migration", stream=BrokenOutput())

    async def fail():
        raise ValueError("original failure")

    with pytest.raises(ValueError, match="original failure"):
        await progress.run(fail())


async def test_closed_stream_preserves_original_exception():
    stream = io.StringIO()
    stream.close()
    progress = MigrationProgress("verify", "migration", stream=stream)

    async def fail():
        raise RuntimeError("original failure")

    with pytest.raises(RuntimeError, match="original failure"):
        await progress.run(fail())


async def test_concurrent_failure_records_failed_worker_not_last_started():
    progress = MigrationProgress("import", "migration", stream=io.StringIO())
    started = asyncio.Event()

    async def work(item, _request):
        if item["position"] == "A":
            await started.wait()
            raise ValueError("A failed")
        started.set()
        await asyncio.sleep(10)

    with progress.activate():
        with pytest.raises(ValueError, match="A failed"):
            await MigrationCoordinator({}, workers=2).run(
                [{"position": "A"}, {"position": "B"}],
                work,
            )
    assert progress.snapshot()["failure_position"] == "A"
    assert progress.snapshot()["position"] == "A"


async def test_coordinator_counts_reset_for_each_task_phase():
    progress = MigrationProgress("verify", "migration", stream=io.StringIO())

    async def work(_item, _request):
        pass

    with progress.activate():
        await MigrationCoordinator({}, workers=1).run(range(3), work)
        assert progress.snapshot()["completed"] == 3
        await MigrationCoordinator({}, workers=1).run(range(2), work)
    assert progress.snapshot()["completed"] == 2
    assert progress.snapshot()["task_total"] == 2
    assert progress.snapshot()["counter_unit"] == "tasks"
