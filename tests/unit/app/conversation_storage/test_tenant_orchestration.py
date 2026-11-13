# -*- coding: utf-8 -*-
"""Tenant failures continue, shared failures never become partial success."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mysql.connector.errors import OperationalError
from swe.app.conversation_storage.errors import UnknownCommit

from swe.cli import conversation_storage_tenants as cli

JOB = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def orchestration(monkeypatch, tmp_path):
    tenants = [
        {"service_unit_id": "svc", "tenant_id": name, "state": "discovered"}
        for name in ("a", "b", "c")
    ]
    manifest = SimpleNamespace(
        migration_id=JOB,
        path=tmp_path / "manifest.json",
        data={
            "tenants": tenants,
            "services": [{"service_unit_id": "svc", "nas_root": "/nas"}],
            "workspaces": [
                {
                    "service_unit_id": "svc",
                    "tenant_id": t["tenant_id"],
                    "root": "/frozen/" + t["tenant_id"],
                }
                for t in tenants
            ],
            "shares": [],
            "assignments": {},
            "batches": {},
        },
        _persist=Mock(),
    )
    rows = {
        name: {"state": "published" if name == "a" else "discovered"}
        for name in ("a", "b", "c")
    }
    states = SimpleNamespace(
        register=AsyncMock(),
        get=AsyncMock(side_effect=lambda service, tenant: rows[tenant]),
        fail=AsyncMock(),
        transition=AsyncMock(),
        publish=AsyncMock(),
    )
    monkeypatch.setattr(cli, "TenantState", lambda *_: states)
    journal = SimpleNamespace(
        _bindings=lambda: ("target", "source"),
        prepare=AsyncMock(),
    )
    monkeypatch.setattr(cli, "MigrationJournal", lambda *a, **kw: journal)
    db = SimpleNamespace(
        fetch_all=AsyncMock(
            return_value=[
                {
                    "migration_id": JOB.encode(),
                    "target_binding_text": "target",
                    "source_binding_text": "source",
                    "manifest_version": 3,
                },
            ],
        ),
        execute=AsyncMock(return_value=1),
    )
    guard = SimpleNamespace(server_uuid="server", check=AsyncMock())
    writer = Mock(return_value="failures.json")
    monkeypatch.setattr(cli, "_write_failures", writer)
    monkeypatch.setattr(cli, "_write_runtime", Mock())
    return SimpleNamespace(
        manifest=manifest,
        rows=rows,
        states=states,
        db=db,
        guard=guard,
        writer=writer,
        base=tmp_path,
    )


async def run(fixture, operation="resume"):
    return await cli._run_registered(
        operation,
        fixture.manifest,
        fixture.base,
        fixture.db,
        fixture.guard,
        fixture.base,
        {},
    )


async def test_published_a_skips_all_old_input_b_failure_does_not_stop_c(
    monkeypatch,
    orchestration,
):
    visited = []

    async def one(operation, view, *_args):
        visited.append(view.key[1])
        assert (
            view.key[1] != "a"
        ), "published tenant must never enter source reader"
        if view.key[1] == "b":
            raise cli.TenantFailure("bad input")

    monkeypatch.setattr(cli, "_one_tenant", one)
    result = await run(orchestration)
    assert visited == ["b", "c"]
    assert (
        result["published"] == 1
        and result["completed"] == 1
        and result["failed"] == 1
    )
    assert result["status"] == "partial_success"
    assert orchestration.states.fail.call_args.args[:2] == ("svc", "b")
    failures = orchestration.writer.call_args.args[2]
    assert failures[0]["tenant_id"] == "b" and "bad input" not in str(failures)


@pytest.mark.parametrize(
    "exception",
    [
        OperationalError("mysql disconnected"),
        UnknownCommit("commit result unknown"),
        RuntimeError("named lock lost"),
        asyncio.CancelledError(),
    ],
)
async def test_shared_failure_or_cancel_stops_before_next_tenant(
    monkeypatch,
    orchestration,
    exception,
):
    one = AsyncMock(side_effect=exception)
    monkeypatch.setattr(cli, "_one_tenant", one)
    with pytest.raises(type(exception)):
        await run(orchestration)
    assert one.await_count == 1
    orchestration.states.fail.assert_not_awaited()
    orchestration.db.execute.assert_not_awaited()
    orchestration.writer.assert_not_called()


async def test_initial_discovery_failure_is_recorded_and_other_tenant_runs(
    monkeypatch,
    orchestration,
):
    orchestration.manifest.data["tenants"][1]["state"] = "failed"
    one = AsyncMock()
    monkeypatch.setattr(cli, "_one_tenant", one)
    result = await run(orchestration)
    assert one.call_args.args[1].key == ("svc", "c")
    assert result["failed"] == 1
    assert orchestration.states.fail.call_args.args[2] == "discover"


async def test_job_binding_mismatch_stops_before_any_source_read(
    monkeypatch,
    orchestration,
):
    orchestration.db.fetch_all.return_value[0][
        "source_binding_text"
    ] = "changed"
    one = AsyncMock()
    monkeypatch.setattr(cli, "_one_tenant", one)
    with pytest.raises(ValueError, match="binding changed"):
        await run(orchestration)
    one.assert_not_awaited()


def test_tenant_manifest_persists_complete_parent_and_keeps_other_tenant_bindings(
    orchestration,
):
    parent = orchestration.manifest
    view = cli.TenantManifest(parent, parent.data["tenants"][1])
    assert [item["tenant_id"] for item in view.data["workspaces"]] == ["b"]
    view.data["assignments"] = {"chat": {"ticket": 42}}
    view.data["batches"] = {"batch": "committed"}
    view._persist()
    parent._persist.assert_called_once()
    assert len(parent.data["workspaces"]) == 3
    assert parent.data["assignments"] == {"chat": {"ticket": 42}}


def test_tenant_journal_binding_uses_complete_parent_not_filtered_view(
    orchestration,
):
    import json
    from swe.app.conversation_storage.migration.journal import MigrationJournal

    parent = orchestration.manifest
    parent.data["version"] = 3
    parent.data["target"] = {"schema": "test", "nas_roots": {"svc": "/nas"}}
    view = cli.TenantManifest(parent, parent.data["tenants"][1])
    journal = MigrationJournal(orchestration.db, view)
    journal.frozen_bindings_data = parent.data
    _, source = journal._bindings()
    assert len(json.loads(source)["workspaces"]) == 3
    assert len(view.data["workspaces"]) == 1


@pytest.mark.parametrize(
    "failure",
    [
        cli.MigrationDiskCapacityError("shared spool capacity exhausted"),
        FileNotFoundError(
            2,
            "spool work directory disappeared",
            "/work/spool",
        ),
        OSError(5, "shared IO failed"),
    ],
)
async def test_shared_spool_disk_missing_path_and_eio_are_not_isolated(
    monkeypatch,
    orchestration,
    failure,
):
    one = AsyncMock(side_effect=failure)
    monkeypatch.setattr(cli, "_one_tenant", one)
    with pytest.raises(type(failure)):
        await run(orchestration)
    assert one.await_count == 1
    orchestration.states.fail.assert_not_awaited()
    orchestration.db.execute.assert_not_awaited()


def share(token, service=None):
    result = {
        "token": token,
        "chat_id": "chat",
        "creator_id": "account",
        "tenant_id": "same-account",
        "snapshot_key": "same-account/chat_shares/" + token + ".json",
        "created_at": "2026-11-01T00:00:00+00:00",
        "access_count": 3,
        "last_access_at": None,
    }
    if service is not None:
        result["service_unit_id"] = service
    return result


async def test_same_account_across_services_compares_only_own_unpublished_shares():
    own, other = share("a", "s1"), share("b", "s2")
    parent = SimpleNamespace(
        data={"source_shares": [own, other], "shares": [own, other]},
    )
    view = SimpleNamespace(
        key=("s1", "same-account"),
        data={"shares": [own]},
        parent=parent,
    )
    db = SimpleNamespace(
        fetch_all=AsyncMock(
            return_value=[
                {**share("a"), "service_unit_key": None},
                {**share("b"), "service_unit_key": None},
            ],
        ),
    )
    await cli._compare_shares(db, view)
    db.fetch_all.return_value = [{**share("a"), "service_unit_key": b"s1"}]
    await cli._compare_shares(db, view)
    # Unknown NULL-service tokens cannot be silently excluded from coverage.
    db.fetch_all.return_value.append(
        {**share("unknown"), "service_unit_key": None},
    )
    with pytest.raises(cli.TenantFailure, match="unregistered"):
        await cli._compare_shares(db, view)


async def test_unsafe_tenant_source_path_is_isolated_before_next_tenant(
    monkeypatch,
    orchestration,
):
    from swe.app.conversation_storage.migration.source_inputs import (
        SourceInputInvalid,
    )

    async def inventory(view, spool, **resources):
        if view.key[1] == "b":
            raise SourceInputInvalid("source contains symlink")
        return {"ready": True, "blockers": [], "workspaces": []}

    monkeypatch.setattr(cli, "inventory_v3_spooled", inventory)
    visited = []

    async def one(operation, view, *_args):
        visited.append(view.key[1])
        await cli._inventory(view, None, {})

    monkeypatch.setattr(cli, "_one_tenant", one)
    result = await run(orchestration)
    assert visited == ["b", "c"] and result["failed"] == 1


def test_real_source_preflight_symlink_has_domain_specific_failure(tmp_path):
    from swe.app.conversation_storage.migration.inventory_parallel import (
        _preflight_workspace,
    )
    from swe.app.conversation_storage.migration.source_inputs import (
        SourceInputInvalid,
    )

    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "target"
    target.write_text("[]")
    (root / "chats.json").symlink_to(target)
    with pytest.raises(SourceInputInvalid, match="symlink"):
        _preflight_workspace({"root": str(root)}, 128 * 1024 * 1024)
    with pytest.raises(SourceInputInvalid, match="relative"):
        _preflight_workspace(
            {"root": str(root), "registry_path": "../outside"},
            128 * 1024 * 1024,
        )


def test_failure_report_has_actionable_safe_reason():
    tenant = {"service_unit_id": "svc", "tenant_id": "bob"}
    report = cli._failure(
        tenant,
        "finalize",
        error=cli.TenantFailure("complete tenant import is required"),
    )
    assert "resume --apply" in report["reason"]
    report = cli._failure(
        tenant,
        "resume",
        error=cli.TenantFailure("secret/path/private-data"),
    )
    assert "secret" not in str(report)
    report = cli._failure(
        tenant,
        "inventory",
        error=cli.MigrationCapacityError("private/path"),
    )
    assert report["code"] == "tenant_capacity_exceeded"
    assert "private" not in str(report)
