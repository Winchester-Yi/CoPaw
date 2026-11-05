# -*- coding: utf-8 -*-
"""Read-only legacy Cron runtime queries and verified file snapshots."""

import asyncio
import os
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from swe.app.crons import api
from swe.app.crons.models import CronJobState, CronJobSpec, JobsFile
from swe.app.crons.repo.json_repo import JsonJobRepository


def job(job_id="job-a", **updates):
    data = {
        "id": job_id,
        "name": "private definition",
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "task_type": "text",
        "text": "large private body",
        "schedule": {"cron": "* * * * *", "timezone": "UTC"},
        "dispatch": {"target": {"user_id": "alice", "session_id": "s"}},
        "meta": {
            "creator_user_id": "alice",
            "task_chat_id": "chat-a",
            "task_session_id": "session-a",
            "task_last_scheduled_preview": "结" * 300,
            "task_unread_execution_count": 2,
        },
    }
    data.update(updates)
    return CronJobSpec.model_validate(data)


def write(path, jobs):
    path.write_text(JobsFile(jobs=jobs).model_dump_json(), encoding="utf-8")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    write(
        path,
        [
            job(),
            job("no-binding", meta={"creator_user_id": "alice"}),
            job("other", meta={"creator_user_id": "bob"}),
        ],
    )
    repo = JsonJobRepository(path)
    states = {
        "job-a": CronJobState(last_status="running", last_error="错" * 1100),
    }
    mgr = SimpleNamespace(
        _repo=repo,
        _timezone="UTC",
        get_state=lambda key: states.get(key, CronJobState()),
    )
    calls = []

    async def manager(request):
        calls.append("manager")
        return mgr

    monkeypatch.setattr(api, "get_cron_manager", manager)
    app = FastAPI()
    app.include_router(api.router)
    return path, repo, mgr, app, calls


HEADERS = {
    "X-User-Id": "alice",
    "X-Tenant-Id": "tenant-a",
    "X-Source-Id": "source-a",
}


@pytest.mark.asyncio
async def test_asgi_contract_is_complete_ordered_small_and_read_only(setup):
    path, repo, mgr, app, _ = setup
    before = path.read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/cron/jobs/-/runtime-statuses",
            headers=HEADERS,
            json={
                "job_ids": ["job-a", "absent", "no-binding", "other", "job-a"],
            },
        )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [(v["id"], v["status"]) for v in items] == [
        ("job-a", "ok"),
        ("absent", "unavailable"),
        ("no-binding", "binding_missing"),
        ("other", "unavailable"),
    ]
    first = items[0]
    assert set(first) == {"id", "status", "enabled", "state", "task"}
    assert len(first["state"]["last_error"]) == 1024
    assert first["state"]["last_error_truncated"] is True
    assert len(first["task"]["latest_scheduled_preview"]) == 256
    assert first["task"]["latest_scheduled_preview_truncated"] is True
    assert first["task"]["is_running"] is True
    assert len(first["state"]["next_run_times"]) == 3
    assert path.read_bytes() == before
    assert mgr.get_state("job-a").next_run_times == []
    assert "private" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ids",
    [[], ["job-a"] * 201, ["x" * 129], ["../a"], [3]],
)
async def test_validation_precedes_manager_initialization(setup, ids):
    _, _, _, app, calls = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/cron/jobs/-/runtime-statuses",
            headers=HEADERS,
            json={"job_ids": ids},
        )
    assert response.status_code == 422
    assert calls == []


@pytest.mark.asyncio
async def test_snapshot_hot_read_mutable_model_isolation_and_replacement(
    setup,
    monkeypatch,
):
    path, repo, _, _, _ = setup
    first = await repo.runtime_snapshot()
    mutable = await repo.get_job("job-a")
    mutable.enabled = False
    assert (await repo.runtime_snapshot()) is first
    assert first["job-a"].enabled is True
    signature = path.stat()
    replacement = path.with_suffix(".replacement")
    original = path.read_text()
    replacement.write_text(
        original.replace('"enabled":true', '"enabled":null', 1),
    )
    # Same length and mtime still differs by inode/ctime.
    replacement.write_text(original.replace('"alice"', '"ALICE"'))
    os.utime(replacement, ns=(signature.st_atime_ns, signature.st_mtime_ns))
    replacement.replace(path)
    second = await repo.runtime_snapshot()
    assert second is not first
    assert second["job-a"].creator_user_id == "ALICE"
    path.write_text("{bad json")
    with pytest.raises(ValueError):
        await repo.runtime_snapshot()
    path.unlink()
    assert dict(await repo.runtime_snapshot()) == {}


@pytest.mark.asyncio
async def test_singleflight_waiter_cancellation_does_not_cancel_load(
    setup,
    monkeypatch,
):
    from swe.app.crons.runtime_snapshot import RuntimeSnapshotCache

    _, repo, _, _, _ = setup
    started, proceed = threading.Event(), threading.Event()
    original = RuntimeSnapshotCache._load_sync
    calls = []

    def blocked(self):
        calls.append(1)
        started.set()
        assert proceed.wait(5)
        return original(self)

    monkeypatch.setattr(RuntimeSnapshotCache, "_load_sync", blocked)
    first = asyncio.create_task(repo.runtime_snapshot())
    assert await asyncio.to_thread(started.wait, 5)
    second = asyncio.create_task(repo.runtime_snapshot())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    proceed.set()
    assert "job-a" in await second
    assert calls == [1]


@pytest.mark.asyncio
async def test_scope_mismatch_is_unavailable_and_manager_can_read_other_creator(
    setup,
):
    path, _, _, app, _ = setup
    write(
        path,
        [
            job(scope_id="foreign-scope"),
            job(
                "other",
                meta={
                    "creator_user_id": "bob",
                    "task_chat_id": "chat-b",
                    "task_session_id": "session-b",
                },
            ),
        ],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/cron/jobs/-/runtime-statuses",
            headers={**HEADERS, "X-User-Role": "manager"},
            json={"job_ids": ["job-a", "other"]},
        )
    assert response.status_code == 200
    assert response.json()["items"][0] == {
        "id": "job-a",
        "status": "unavailable",
    }
    assert response.json()["items"][1]["status"] == "ok"
    assert response.json()["items"][1]["task"]["visible_in_my_tasks"] is False


@pytest.mark.asyncio
async def test_hot_batch_has_one_stat_and_no_full_repository_reads(
    setup,
    monkeypatch,
):
    from swe.app.crons.runtime_snapshot import RuntimeSnapshotCache

    path, repo, _, app, _ = setup
    await repo.runtime_snapshot()
    stats = []
    original = RuntimeSnapshotCache._path_signature

    def stat(self):
        stats.append(1)
        return original(self)

    async def forbidden(*args):
        pytest.fail("runtime must not load the full mutable repository")

    monkeypatch.setattr(RuntimeSnapshotCache, "_path_signature", stat)
    monkeypatch.setattr(repo, "load", forbidden)
    monkeypatch.setattr(repo, "get_job", forbidden)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/cron/jobs/-/runtime-statuses",
            headers=HEADERS,
            json={"job_ids": ["job-a"] * 200},
        )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert stats == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [PermissionError("denied"), OSError("IO failed"), ValueError("bad state")],
)
async def test_read_errors_are_503_even_after_success(
    setup,
    monkeypatch,
    error,
):
    path, repo, _, app, _ = setup
    await repo.runtime_snapshot()

    def failed():
        raise error

    monkeypatch.setattr(repo._runtime_snapshot, "_path_signature", failed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/cron/jobs/-/runtime-statuses",
            headers=HEADERS,
            json={"job_ids": ["job-a"]},
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_conflicting_identity_rejected_before_workspace(setup):
    _, _, _, app, calls = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/cron/jobs/-/runtime-statuses",
            headers={**HEADERS, "X-Scope-Id": "foreign"},
            json={"job_ids": ["job-a"]},
        )
    assert response.status_code == 400
    assert calls == []


@pytest.mark.asyncio
async def test_replacement_during_open_retries_and_publishes_matching_content(
    setup,
    monkeypatch,
):
    from pathlib import Path

    path, repo, _, _, _ = setup
    original = Path.open
    opens = []

    def replacing(self, *args, **kwargs):
        if self == path and args == ("rb",):
            opens.append(1)
            if len(opens) == 1:
                replacement = path.with_suffix(".new")
                write(replacement, [job(enabled=False)])
                replacement.replace(path)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replacing)
    snapshot = await repo.runtime_snapshot()
    assert snapshot["job-a"].enabled is False
    assert len(opens) == 2
    assert await repo.runtime_snapshot() is snapshot


@pytest.mark.asyncio
async def test_continuously_replaced_file_retries_only_once_and_never_publishes(
    setup,
    monkeypatch,
):
    from pathlib import Path

    path, repo, _, _, _ = setup
    original = Path.open
    opens = []

    def replacing(self, *args, **kwargs):
        if self == path and args == ("rb",):
            opens.append(1)
            replacement = path.with_suffix(".new")
            write(replacement, [job(enabled=False)])
            replacement.replace(path)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replacing)
    with pytest.raises(OSError):
        await repo.runtime_snapshot()
    assert len(opens) == 2
    assert repo._runtime_snapshot._signature is None


def test_disabled_runtime_pause_matches_caller_specific_full_view():
    from swe.app.crons.runtime_snapshot import RuntimeJob
    from swe.app.crons.task_view import build_cron_task_view

    spec = job(enabled=False)
    projection = RuntimeJob.from_spec(spec)
    for actor in ("alice", "manager"):
        actual = projection.runtime_item(CronJobState(), actor, "UTC").task
        expected = build_cron_task_view(spec, CronJobState(), actor)
        assert actual.visible_in_my_tasks == expected.visible_in_my_tasks
        assert actual.is_paused == expected.is_paused
        assert actual.pause_reason == expected.pause_reason
