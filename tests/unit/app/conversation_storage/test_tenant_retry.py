# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.migration import tenant_retry as retry

JOB = "00000000-0000-4000-8000-000000000001"


def test_ticket_identity_with_slashes_is_parsed_as_json_not_split():
    row = {
        "source_key": '["session","svc","a/b","SRC","agent","user","console","sid"]/message:0',
        "details_text": None,
    }
    assert retry._ticket_owner(row) == ("svc", "a/b")
    with pytest.raises(ValueError, match="cannot be proved"):
        retry._ticket_owner(
            {"source_key": "unknown:ticket", "details_text": None},
        )


async def test_review_rejects_unknown_or_affected_ticket():
    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value=None),
        fetch_all=AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "source_key": '["session","svc","alice","SRC","agent"]',
                    "details_text": None,
                },
            ],
        ),
    )
    with pytest.raises(ValueError, match="ticket exists"):
        await retry._no_data(db, JOB, {("svc", "alice")})
    db.fetch_all.return_value = [
        {"id": 1, "source_key": "unknown", "details_text": None},
    ]
    with pytest.raises(ValueError):
        await retry._no_data(db, JOB, {("svc", "alice")})


async def test_review_rejects_business_data_without_deleting():
    async def existing_row(query, params=None):
        if "LEFT JOIN" in query:
            return None
        return {"id": 1}

    db = SimpleNamespace(fetch_one=AsyncMock(side_effect=existing_row))
    with pytest.raises(ValueError, match="business data exists"):
        await retry._no_data(db, JOB, {("svc", "alice")})


@pytest.mark.parametrize("outcome", ["old", "new", "unknown"])
async def test_recovery_requires_matching_sql_evidence_and_retains_unknown(
    tmp_path,
    monkeypatch,
    outcome,
):
    old, new = {"migration_id": JOB, "version": 3, "source": "old"}, {
        "migration_id": JOB,
        "version": 3,
        "source": "new",
    }
    monkeypatch.setattr(retry, "_source", lambda data: data["source"])
    snapshots = {}
    for label, data in (("old", old), ("new", new)):
        path = tmp_path / (label + ".json")
        retry._write(path, data)
        snapshots[label] = {"name": path.name, "sha256": retry._digest(data)}
    manifest = SimpleNamespace(
        path=tmp_path / "manifest.json",
        migration_id=JOB,
        data=dict(old),
        _persist=lambda: None,
    )
    pending = {
        "migration_id": JOB,
        "revision": "revision",
        "old_source": "old",
        "new_source": "new",
        "files": snapshots,
        "tenants": [
            {
                "service": "svc",
                "tenant": "alice",
                "old": "before",
                "new": "after",
                "old_state": "failed",
            },
        ],
    }
    retry._write(retry._pending(manifest), pending)

    @asynccontextmanager
    async def transaction():
        yield

    async def fetch(query, params):
        if "migration_jobs" in query:
            return {
                "source_binding_text": outcome,
                "verification_text": json.dumps(
                    (
                        {"tenant_review_revision": "revision"}
                        if outcome == "new"
                        else {}
                    ),
                ),
            }
        return {
            "state": "discovered" if outcome == "new" else "failed",
            "evidence_text": "after" if outcome == "new" else "before",
        }

    db = SimpleNamespace(transaction=transaction, fetch_one=fetch)
    monkeypatch.setattr(
        retry.V3Manifest,
        "load",
        lambda path: SimpleNamespace(
            data=json.loads(path.read_text()),
            migration_id=JOB,
        ),
    )
    if outcome == "unknown":
        with pytest.raises(ValueError, match="unknown"):
            await retry.recover_pending_review(db, manifest)
        assert retry._pending(manifest).exists()
    else:
        assert (await retry.recover_pending_review(db, manifest))[
            "outcome"
        ] == outcome
        assert manifest.data["source"] == outcome
        assert not retry._pending(manifest).exists()
        assert list(tmp_path.glob("*.audit.json"))


@pytest.mark.parametrize("unknown_commit", [True, False])
async def test_review_corrected_failed_tenant_preserves_published_scope_and_pending_on_unknown_commit(
    tmp_path,
    monkeypatch,
    unknown_commit,
):
    from copy import deepcopy

    service = {
        "service_unit_id": "svc",
        "source_root": "/frozen",
        "config_root": "/frozen",
        "original_root": "/runtime",
        "nas_root": "/nas",
    }
    alice = {
        "service_unit_id": "svc",
        "tenant_id": "alice",
        "source_id": "SRC",
        "agent_id": "a",
        "position": "alice",
        "root": "/frozen/a",
        "runtime_root": "/runtime/a",
        "counts": {"sessions": 0, "chats": 0},
        "empty": True,
    }
    tenants = [
        {
            "service_unit_id": "svc",
            "tenant_id": "alice",
            "state": "discovered",
            "blockers": [],
        },
        {
            "service_unit_id": "svc",
            "tenant_id": "bob",
            "state": "failed",
            "blockers": [{"code": "config_invalid"}],
        },
    ]
    data = {
        "version": 3,
        "migration_id": JOB,
        "tenant_isolation": True,
        "services": [service],
        "declared_services": [service],
        "source_shares": [],
        "shares": [],
        "tenants": tenants,
        "workspaces": [alice],
        "blockers": [{"code": "config_invalid"}],
        "ready": False,
        "status": "partial_success",
        "target": {
            "migration_id": JOB,
            "schema": "test",
            "nas_roots": {"svc": "/nas"},
        },
        "assignments": {},
        "batches": {},
    }
    report = {
        k: deepcopy(v)
        for k, v in data.items()
        if k not in ("target", "migration_id", "assignments", "batches")
    }
    bob = {
        **alice,
        "tenant_id": "bob",
        "position": "bob",
        "root": "/frozen/b",
        "runtime_root": "/runtime/b",
    }
    report["workspaces"].append(bob)
    report["tenants"][1].update(state="discovered", blockers=[])
    report.update(blockers=[], ready=True, status="success")
    monkeypatch.setattr(
        retry,
        "discover_services",
        lambda *args, **kwargs: report,
    )
    manifest = SimpleNamespace(
        data=data,
        path=tmp_path / "manifest.json",
        migration_id=JOB,
    )
    data["tenant_sync_started_at_ns"] = {
        "svc\x00alice": 100,
        "svc\x00bob": 100,
    }
    data["source_change_blockers"] = [
        {"scope": ["svc", "bob"], "detail": "old"},
        {"scope": ["svc", "alice"], "detail": "keep"},
    ]
    old_source = retry._source(data)
    records = {
        "alice": {
            "state": "published",
            "evidence_text": retry._json(
                retry._evidence(data, ("svc", "alice")),
            ),
        },
        "bob": {
            "state": "failed",
            "evidence_text": retry._json(
                retry._evidence(data, ("svc", "bob")),
            ),
        },
    }

    @asynccontextmanager
    async def transaction():
        yield
        if unknown_commit:
            raise ConnectionError("commit outcome unknown")

    async def fetch(query, params=None):
        if "migration_tenants" in query:
            return records[params[2].decode()]
        if "migration_jobs" in query:
            return {
                "source_binding_text": old_source,
                "verification_text": "{}",
            }
        return None

    db = SimpleNamespace(
        transaction=transaction,
        fetch_one=fetch,
        fetch_all=AsyncMock(return_value=[]),
        execute=AsyncMock(return_value=1),
    )
    if unknown_commit:
        with pytest.raises(ConnectionError, match="unknown"):
            await retry.review_retry(db, manifest, report)
    else:
        recover = AsyncMock(return_value={"outcome": "new"})
        monkeypatch.setattr(retry, "recover_pending_review", recover)
        assert (await retry.review_retry(db, manifest, report))[
            "outcome"
        ] == "new"
        recover.assert_awaited_once_with(db, manifest)
    assert retry._pending(manifest).exists()
    pending = json.loads(retry._pending(manifest).read_text())
    assert [r["tenant"] for r in pending["tenants"]] == ["bob"]
    new_data = json.loads(
        (tmp_path / pending["files"]["new"]["name"]).read_text(),
    )
    assert new_data["tenant_sync_started_at_ns"]["svc\x00alice"] == 100
    assert new_data["tenant_sync_started_at_ns"]["svc\x00bob"] > 100
    assert new_data["source_change_blockers"] == [
        {"scope": ["svc", "alice"], "detail": "keep"},
    ]
    assert manifest.data == data
    assert db.execute.await_count == 2


def test_workspace_prefix_ticket_uses_exact_original_position():
    position = '["workspace","svc","tenant/workspaces/agent"]'
    workspace = {
        "position": position,
        "service_unit_id": "svc",
        "tenant_id": "alice",
    }
    assert retry._ticket_owner(
        {
            "source_key": position + "/session:0/message:1",
            "details_text": None,
        },
        [workspace],
    ) == ("svc", "alice")
    with pytest.raises(ValueError):
        retry._ticket_owner(
            {"source_key": position + "evil/session:0", "details_text": None},
            [workspace],
        )


async def test_runtime_tickets_do_not_prevent_zero_data_tenant_review():
    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value=None),
        fetch_all=AsyncMock(
            side_effect=[
                [
                    {
                        "id": 1,
                        "entry_kind": b"runtime_id",
                        "source_key": b"opaque",
                        "details_text": None,
                    },
                ],
                [],
            ],
        ),
    )
    await retry._no_data(db, JOB, {("svc", "bob")})
    queries = [call.args[0] for call in db.fetch_one.call_args_list]
    assert any(
        "swe_conversation_turns b JOIN swe_conversation_sessions" in query
        for query in queries
    )
    assert any(
        "swe_conversation_messages b JOIN swe_conversation_sessions" in query
        for query in queries
    )


def test_retention_and_share_tickets_have_explicit_verified_owners():
    retention = {
        "entry_kind": b"retention",
        "source_key": b"retention",
        "details_text": json.dumps(
            {"service_unit_key": "svc", "tenant_key": "alice"},
        ),
    }
    assert retry._ticket_owner(retention) == ("svc", "alice")
    share = {
        "entry_kind": b"share",
        "source_key": b"opaque-token",
        "details_text": "{}",
    }
    assert retry._ticket_owner(
        share,
        shares=[
            {
                "token": "opaque-token",
                "service_unit_id": "svc",
                "tenant_id": "alice",
            },
        ],
    ) == ("svc", "alice")
    with pytest.raises(ValueError, match="share ownership"):
        retry._ticket_owner(share)
