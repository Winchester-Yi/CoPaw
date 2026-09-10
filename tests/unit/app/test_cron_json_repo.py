# -*- coding: utf-8 -*-
"""验证 cron JSON 仓库的非阻塞 I/O 与快照缓存语义。"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

import swe.app.crons.repo.json_repo as json_repo_module
from swe.app.crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    JobsFile,
    ScheduleSpec,
)
from swe.app.crons.repo.json_repo import JsonJobRepository


def _job(job_id: str, *, name: str | None = None) -> CronJobSpec:
    return CronJobSpec(
        id=job_id,
        name=name or job_id,
        schedule=ScheduleSpec(cron="* * * * *"),
        request=CronJobRequest(input="ping"),
        dispatch=DispatchSpec(
            target=DispatchTarget(
                user_id="user-a",
                session_id=f"session-{job_id}",
            ),
        ),
    )


def _write_jobs(path: Path, jobs: list[CronJobSpec]) -> None:
    payload = JobsFile(jobs=jobs).model_dump(mode="json")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_load_delegates_file_parse_and_validation_to_thread(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "jobs.json"
    _write_jobs(path, [_job("job-a")])
    repo = JsonJobRepository(path)
    calls: list[str] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        calls.append(func.__name__)
        return func(*args, **kwargs)

    monkeypatch.setattr(json_repo_module.asyncio, "to_thread", fake_to_thread)

    loaded = await repo.load()

    assert calls == ["_load_sync"]
    assert [job.id for job in loaded.jobs] == ["job-a"]


@pytest.mark.asyncio
async def test_save_delegates_dump_write_and_replace_to_thread(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "jobs.json"
    repo = JsonJobRepository(path)
    calls: list[str] = []

    async def fake_to_thread(func, /, *args, **kwargs):
        calls.append(func.__name__)
        return func(*args, **kwargs)

    monkeypatch.setattr(json_repo_module.asyncio, "to_thread", fake_to_thread)

    await repo.save(JobsFile(jobs=[_job("job-a", name="Saved Job")]))

    assert calls == ["_save_sync"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["jobs"][0]["id"] == "job-a"
    assert payload["jobs"][0]["name"] == "Saved Job"
    assert payload["version"] == 1


@pytest.mark.asyncio
async def test_get_job_uses_cached_index_when_file_signature_unchanged(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "jobs.json"
    _write_jobs(path, [_job("job-a")])
    repo = JsonJobRepository(path)
    load_calls = 0
    original_load = repo.load

    async def counted_load() -> JobsFile:
        nonlocal load_calls
        load_calls += 1
        return await original_load()

    monkeypatch.setattr(repo, "load", counted_load)

    assert (await repo.get_job("job-a")).id == "job-a"
    assert (await repo.get_job("job-a")).id == "job-a"
    assert load_calls == 1


@pytest.mark.asyncio
async def test_get_job_refreshes_cache_when_file_signature_changes(
    tmp_path,
) -> None:
    path = tmp_path / "jobs.json"
    _write_jobs(path, [_job("job-a", name="old")])
    repo = JsonJobRepository(path)

    assert (await repo.get_job("job-a")).name == "old"

    _write_jobs(path, [_job("job-a", name="new value with changed size")])

    assert (await repo.get_job("job-a")).name == "new value with changed size"


@pytest.mark.asyncio
async def test_invalid_jobs_file_raises_instead_of_returning_stale_cache(
    tmp_path,
) -> None:
    path = tmp_path / "jobs.json"
    _write_jobs(path, [_job("job-a")])
    repo = JsonJobRepository(path)

    assert (await repo.get_job("job-a")).id == "job-a"

    path.write_text("{invalid json that changes file size", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        await repo.get_job("job-a")


@pytest.mark.asyncio
async def test_concurrent_mutations_apply_an_execution_key_once(
    tmp_path,
) -> None:
    path = tmp_path / "jobs.json"
    _write_jobs(path, [_job("job-a")])
    first_repo = JsonJobRepository(path)
    second_repo = JsonJobRepository(path)
    execution_key = "execution-123"

    def record_success(jobs_file: JobsFile) -> tuple[bool, bool]:
        job = jobs_file.jobs[0]
        meta = dict(job.meta or {})
        completed_keys = list(meta.get("completed_keys", []))
        if execution_key in completed_keys:
            return False, False
        meta["completed_keys"] = [*completed_keys, execution_key]
        meta["unread_count"] = int(meta.get("unread_count", 0)) + 1
        jobs_file.jobs[0] = job.model_copy(update={"meta": meta})
        return True, True

    results = await asyncio.gather(
        first_repo.mutate_jobs_file(record_success),
        second_repo.mutate_jobs_file(record_success),
    )

    assert [result[1] for result in results].count(True) == 1
    persisted = await JsonJobRepository(path).load()
    assert persisted.jobs[0].meta["completed_keys"] == [execution_key]
    assert persisted.jobs[0].meta["unread_count"] == 1


@pytest.mark.asyncio
async def test_cancelled_mutation_keeps_file_lock_until_write_finishes(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "jobs.json"
    _write_jobs(path, [_job("job-a")])
    first_repo = JsonJobRepository(path)
    second_repo = JsonJobRepository(path)
    write_started = threading.Event()
    allow_write = threading.Event()
    original_save = first_repo._save_sync

    def blocked_save(jobs_file: JobsFile):
        write_started.set()
        allow_write.wait(timeout=5)
        return original_save(jobs_file)

    monkeypatch.setattr(first_repo, "_save_sync", blocked_save)

    def update_name(jobs_file: JobsFile) -> tuple[bool, None]:
        job = jobs_file.jobs[0]
        jobs_file.jobs[0] = job.model_copy(update={"name": "updated"})
        return True, None

    first_mutation = asyncio.create_task(
        first_repo.mutate_jobs_file(update_name),
    )
    await asyncio.to_thread(write_started.wait, 5)
    first_mutation.cancel()

    second_mutation = asyncio.create_task(
        second_repo.mutate_jobs_file(update_name),
    )
    await asyncio.sleep(0.1)
    assert not second_mutation.done()

    allow_write.set()
    with pytest.raises(asyncio.CancelledError):
        await first_mutation
    await second_mutation


@pytest.mark.asyncio
async def test_cancelled_mutation_logs_background_write_failure(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "jobs.json"
    _write_jobs(path, [_job("job-a")])
    repo = JsonJobRepository(path)
    write_started = threading.Event()
    allow_write = threading.Event()
    logged_errors: list[str] = []

    def failed_save(_jobs_file: JobsFile):
        write_started.set()
        allow_write.wait(timeout=5)
        raise OSError("disk full")

    monkeypatch.setattr(repo, "_save_sync", failed_save)
    monkeypatch.setattr(
        json_repo_module.logger,
        "error",
        lambda message, *args: logged_errors.append(message % args),
    )

    def update_name(jobs_file: JobsFile) -> tuple[bool, None]:
        job = jobs_file.jobs[0]
        jobs_file.jobs[0] = job.model_copy(update={"name": "updated"})
        return True, None

    mutation = asyncio.create_task(repo.mutate_jobs_file(update_name))
    await asyncio.to_thread(write_started.wait, 5)
    mutation.cancel()
    allow_write.set()

    with pytest.raises(asyncio.CancelledError):
        await mutation
    assert "cancelled cron jobs write failed" in logged_errors[0]
