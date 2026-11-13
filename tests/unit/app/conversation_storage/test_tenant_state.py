# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.migration.journal import MigrationJournal
from swe.app.conversation_storage.migration.tenant_state import TenantState

JOB = "00000000-0000-4000-8000-000000000001"
EVIDENCE = {
    "workspaces": [],
    "shares": [],
    "service": {"service_unit_id": "svc", "nas_root": "/nas"},
}
PROOF = {
    "version": 1,
    "migration_id": JOB,
    "services": {"svc": {"nas_root": "/nas", "workspaces": []}},
}


class Database:
    def __init__(self):
        self.rows = {}

    @asynccontextmanager
    async def transaction(self):
        before = deepcopy(self.rows)
        try:
            yield self
        except BaseException:
            self.rows = before
            raise

    async def fetch_one(self, query, params):
        return self.rows.get(params)

    async def execute(self, query, params):
        if query.startswith("INSERT"):
            self.rows[params[:3]] = {
                "state": "discovered",
                "evidence_text": params[3],
                "verification_text": "{}",
            }
        elif "failure_text=%s" in query:
            self.rows[params[1:4]]["state"] = "failed"
        else:
            row = self.rows[params[4:7]]
            assert row["state"] == params[7]
            row["state"] = params[0]
            if params[1] is not None:
                row["verification_text"] = params[1]
        return 1


async def test_published_tenant_is_terminal_and_retry_does_not_change_proof():
    db = Database()
    store = TenantState(db, JOB)
    await store.register(
        [
            {
                "service_unit_id": "svc",
                "tenant_id": "a",
                "evidence": EVIDENCE,
            },
        ],
    )
    for state in ("importing", "imported", "verified"):
        await store.transition("svc", "a", state)
    await store.publish("svc", "a", PROOF)
    await store.publish("svc", "a", PROOF)
    with pytest.raises(ValueError, match="published"):
        await store.transition("svc", "a", "importing")
    with pytest.raises(ValueError, match="published"):
        await store.fail("svc", "a", "import", "invalid", "bad")
    with pytest.raises(ValueError, match="publication|published"):
        await store.publish(
            "svc",
            "a",
            {
                **PROOF,
                "services": {
                    "svc": {"nas_root": "/different", "workspaces": []},
                },
            },
        )


async def test_evidence_rebinding_and_premature_publication_are_rejected():
    store = TenantState(Database(), JOB)
    item = {
        "service_unit_id": "svc",
        "tenant_id": "a",
        "evidence": {**EVIDENCE, "root": "one"},
    }
    await store.register([item])
    with pytest.raises(ValueError, match="evidence changed"):
        await store.register([{**item, "evidence": {"root": "two"}}])
    with pytest.raises(ValueError, match="transition"):
        await store.publish("svc", "a", PROOF)


async def test_failure_and_retry_are_scoped_to_exact_service_tenant():
    store = TenantState(Database(), JOB)
    await store.register(
        [
            {"service_unit_id": service, "tenant_id": "a", "evidence": {}}
            for service in ("s1", "s2")
        ],
    )
    await store.fail("s1", "a", "inventory", "invalid", "bad")
    assert (await store.get("s2", "a"))["state"] == "discovered"
    await store.transition("s1", "a", "importing")
    assert (await store.get("s1", "a"))["state"] == "importing"


async def test_ownership_and_retention_queries_exclude_online_tenant():
    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value=None),
        fetch_all=AsyncMock(return_value=[]),
    )
    manifest = SimpleNamespace(
        migration_id=JOB,
        data={"version": 3, "tenant_isolation": True},
    )
    journal = MigrationJournal(db, manifest)
    journal.active_tenant = ("svc", "b")
    await journal.validate_ownership()
    for call in db.fetch_one.call_args_list:
        if len(call.args) == 1:
            assert "WHERE ts.id IS NULL" in call.args[0]
            continue
        assert (
            "b.service_unit_key=%s AND b.tenant_key=%s" in call.args[0]
            or "ts.service_unit_key=%s AND ts.tenant_key=%s" in call.args[0]
        )
        assert call.args[1][-2:] == (b"svc", b"b")
    assert db.fetch_all.call_args.args[1][:2] == (b"svc", b"b")
    db.fetch_one.side_effect = lambda sql, *args: (
        None if "WHERE ts.id IS NULL" in sql else {"id": 99}
    )
    with pytest.raises(ValueError, match="unknown existing"):
        await journal.validate_ownership()
    db.fetch_one.side_effect = lambda sql, *args: (
        {"id": 99} if "WHERE ts.id IS NULL" in sql else None
    )
    with pytest.raises(ValueError, match="orphan conversation row"):
        await journal.validate_ownership()


async def test_publication_rejects_omitted_workspace_and_wrong_nas():
    store = TenantState(Database(), JOB)
    workspace = {
        "runtime_root": "/workspace",
        "service_unit_id": "svc",
        "tenant_id": "a",
        "source_id": "source",
        "agent_id": "agent",
    }
    await store.register(
        [
            {
                "service_unit_id": "svc",
                "tenant_id": "a",
                "evidence": {**EVIDENCE, "workspaces": [workspace]},
            },
        ],
    )
    for state in ("importing", "imported", "verified"):
        await store.transition("svc", "a", state)
    with pytest.raises(ValueError, match="evidence scope"):
        await store.publish("svc", "a", PROOF)
    proof = deepcopy(PROOF)
    proof["services"]["svc"]["workspaces"] = [
        {"root": "/workspace", "scope": ["svc", "a", "source", "agent"]},
    ]
    wrong = deepcopy(proof)
    wrong["services"]["svc"]["nas_root"] = "/wrong"
    with pytest.raises(ValueError, match="evidence scope"):
        await store.publish("svc", "a", wrong)
    await store.publish("svc", "a", proof)


async def test_compare_and_set_failure_is_not_reported_success():
    store = TenantState(Database(), JOB)
    await store.register(
        [{"service_unit_id": "svc", "tenant_id": "a", "evidence": EVIDENCE}],
    )
    store.db.execute = AsyncMock(return_value=0)
    with pytest.raises(ValueError, match="compare-and-set"):
        await store.transition("svc", "a", "importing")
    with pytest.raises(ValueError, match="compare-and-set"):
        await store.fail("svc", "a", "inventory", "bad", "invalid")


@pytest.mark.parametrize(
    "isolation,tenants,error",
    [
        (1, [], "boolean"),
        (True, None, "frozen tenants"),
        (
            True,
            [{"service_unit_id": "svc", "tenant_id": "a"}] * 2,
            "duplicate",
        ),
        (True, [{"service_unit_id": "other", "tenant_id": "a"}], "undeclared"),
    ],
)
def test_manifest_rejects_ambiguous_tenant_registry(
    tmp_path,
    isolation,
    tenants,
    error,
):
    from swe.app.conversation_storage.migration.v3_manifest import V3Manifest
    from swe.app.conversation_storage.migration.manifest import SourceBoundary
    from dataclasses import asdict

    data = {
        "version": 3,
        "workflow": "direct",
        "migration_id": JOB,
        "source_boundary": asdict(
            SourceBoundary(
                "snapshot",
                "2026-10-30T00:00:00Z",
                "image@sha256:" + "a" * 64,
                ("evidence",),
            ),
        ),
        "services": [{"service_unit_id": "svc"}],
        "workspaces": [],
        "assignments": {},
        "batches": {},
        "tenant_isolation": isolation,
        "tenants": tenants,
    }
    with pytest.raises(ValueError, match=error):
        V3Manifest(tmp_path / "manifest.json", data)


def test_disk_manifest_header_keeps_tenant_bindings(tmp_path):
    import json
    from swe.app.conversation_storage.migration.disk_manifest import (
        read_manifest_header,
    )

    header = {
        "version": 3,
        "tenant_isolation": True,
        "tenants": [{"service_unit_id": "svc", "tenant_id": "a"}],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({**header, "assignments": {}, "batches": {}}))
    assert read_manifest_header(path) == header


async def test_orphan_turn_or_message_cannot_hide_outside_active_tenant():
    def row(query, params=None):
        return {"id": 99} if "WHERE ts.id IS NULL" in query else None

    db = SimpleNamespace(
        fetch_one=AsyncMock(side_effect=row),
        fetch_all=AsyncMock(return_value=[]),
    )
    manifest = SimpleNamespace(
        migration_id=JOB,
        data={"version": 3, "tenant_isolation": True},
    )
    journal = MigrationJournal(db, manifest)
    journal.active_tenant = ("svc", "b")
    with pytest.raises(ValueError, match="unattributable orphan"):
        await journal.validate_ownership()
