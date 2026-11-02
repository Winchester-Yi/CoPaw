# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.errors import IntegrityError
from swe.app.conversation_storage.migration import authority

JOB = "00000000-0000-4000-8000-000000000001"


def receipt(root):
    return {
        "version": 1,
        "migration_id": JOB,
        "services": {
            "svc": {
                "nas_root": str(root),
                "workspaces": [
                    {
                        "root": str(root / "workspace"),
                        "scope": ["svc", "tenant", "source", "agent"],
                    },
                ],
            },
        },
    }


class Database:
    def __init__(self, proof=None, state="ready", tables=True):
        self.config = SimpleNamespace(database="target")
        self.proof = proof
        self.state = state
        self.tables = tables
        self.insert = AsyncMock(return_value=1)

    async def fetch_one(self, query, params=None):
        if "information_schema.TABLES" in query:
            return {"n": 2 if self.tables else 0}
        if "@@server_uuid" in query:
            return {"server_uuid": "server", "schema_name": "target"}
        raise AssertionError(query)

    async def fetch_all(self, query, params=None):
        assert "swe_conversation_migration_jobs" in query
        if self.proof is None:
            return []
        return [
            {
                "migration_id": JOB.encode(),
                "state": self.state,
                "target_binding_text": json.dumps(
                    {
                        "schema": "target",
                        "server_uuid": "server",
                        "migration_id": JOB,
                        "nas_roots": {
                            service: entry["nas_root"]
                            for service, entry in self.proof[
                                "services"
                            ].items()
                        },
                    },
                ),
                "verification_text": json.dumps({"authority": self.proof}),
            },
        ]

    @asynccontextmanager
    async def transaction(self):
        yield self


def document(root, proof):
    return {
        "version": 2,
        "service_unit_id": "svc",
        "nas_root": str(root),
        "workspaces": proof["services"]["svc"]["workspaces"],
        "migration_authority": proof,
    }


async def test_runtime_rejects_incomplete_job(tmp_path):
    proof = receipt(tmp_path)
    with pytest.raises(IntegrityError, match="ready"):
        await authority.validate_authority(
            Database(proof, "importing"),
            document(tmp_path, proof),
        )


async def test_runtime_rejects_missing_receipt_for_existing_job(tmp_path):
    proof = receipt(tmp_path)
    doc = document(tmp_path, proof)
    doc.pop("migration_authority")
    with pytest.raises(IntegrityError, match="authority"):
        await authority.validate_authority(Database(proof), doc)


async def test_runtime_accepts_ready_receipt_without_old_row_fingerprint(
    tmp_path,
):
    proof = receipt(tmp_path)
    assert (
        await authority.validate_authority(
            Database(proof),
            document(tmp_path, proof),
        )
        == proof
    )


async def test_absent_controls_preserve_existing_runtime(tmp_path):
    proof = receipt(tmp_path)
    doc = document(tmp_path, proof)
    doc.pop("migration_authority")
    assert (
        await authority.validate_authority(Database(tables=False), doc) is None
    )


async def test_partial_controls_block_runtime(tmp_path):
    proof = receipt(tmp_path)
    doc = document(tmp_path, proof)
    doc.pop("migration_authority")
    db = Database(tables=False)
    db.fetch_one = AsyncMock(return_value={"n": 1})
    with pytest.raises(IntegrityError, match="control"):
        await authority.validate_authority(db, doc)


async def test_backup_authority_rejects_other_server(tmp_path):
    db = Database(receipt(tmp_path))
    original = db.fetch_one

    async def identity(query, params=None):
        if "@@server_uuid" in query:
            return {"server_uuid": "other-server", "schema_name": "target"}
        return await original(query, params)

    db.fetch_one = identity
    with pytest.raises(IntegrityError, match="binding"):
        await authority.backup_authority(
            db,
            WorkspaceScope("tenant", "source", "agent", service_unit_id="svc"),
        )


async def test_runtime_rejects_other_service_nas_binding(tmp_path):
    proof = receipt(tmp_path)
    proof["services"]["other"] = {"nas_root": "/other", "workspaces": []}
    db = Database(proof)
    original = db.fetch_all

    async def altered(query, params=None):
        rows = await original(query, params)
        binding = json.loads(rows[0]["target_binding_text"])
        binding["nas_roots"]["other"] = "/wrong"
        rows[0]["target_binding_text"] = json.dumps(binding)
        return rows

    db.fetch_all = altered
    with pytest.raises(IntegrityError, match="binding"):
        await authority.validate_authority(db, document(tmp_path, proof))


async def test_backup_receipt_is_scoped_to_actual_capture(tmp_path):
    proof = receipt(tmp_path)
    proof["services"]["other"] = {"nas_root": "/other", "workspaces": []}
    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="svc")
    captured = await authority.backup_authority(Database(proof), scope)
    assert set(captured["services"]) == {"svc"}
    assert (
        captured["services"]["svc"]["workspaces"]
        == proof["services"]["svc"]["workspaces"]
    )


async def test_restore_installs_new_identity_for_actual_scopes(tmp_path):
    (tmp_path / "existing-workspace").mkdir()
    proof = receipt(tmp_path / "old")
    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="svc")
    db = Database()
    new = await authority.restore_authorities(
        db,
        [proof],
        [scope],
        {"svc": str(tmp_path / "new")},
        workspace_roots={scope.values: str(tmp_path / "existing-workspace")},
    )
    assert new["migration_id"] != JOB
    assert new["services"]["svc"]["nas_root"] == str(tmp_path / "new")
    assert new["services"]["svc"]["workspaces"][0]["root"] == str(
        tmp_path / "existing-workspace",
    )
    assert db.insert.await_count == 1
    args = db.insert.call_args.args[1]
    assert '"restored": true' in args[5]


async def test_restore_rejects_scope_outside_receipt(tmp_path):
    scope = WorkspaceScope("other", "source", "agent", service_unit_id="svc")
    with pytest.raises(IntegrityError, match="scope"):
        await authority.restore_authorities(
            Database(),
            [receipt(tmp_path)],
            [scope],
            {"svc": str(tmp_path / "target")},
        )


async def test_restore_requires_explicit_workspace_roots(tmp_path):
    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="svc")
    with pytest.raises(IntegrityError, match="workspace"):
        await authority.restore_authorities(
            Database(),
            [receipt(tmp_path)],
            [scope],
            {"svc": str(tmp_path / "target")},
        )


def test_runtime_accepts_share_only_with_validated_authority(tmp_path):
    from swe.app.conversation_storage.runtime import ConversationRuntime

    proof = receipt(tmp_path)
    proof["services"]["svc"]["workspaces"] = []
    doc = document(tmp_path, proof)
    runtime = ConversationRuntime(
        Database(),
        doc,
        b"secret",
        service_unit_id="svc",
        authority_validated=True,
    )
    assert runtime.mappings == {}


async def test_runtime_checks_authority_before_schema(tmp_path, monkeypatch):
    from swe.app.conversation_storage import runtime

    proof = receipt(tmp_path)
    (tmp_path / "workspace").mkdir()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document(tmp_path, proof)))
    monkeypatch.setenv(runtime.AUTHORITY_ENV, "mysql")
    monkeypatch.setenv(runtime.MAPPING_ENV, str(path))
    monkeypatch.setenv(runtime.CURSOR_ENV, "secret")
    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "svc")
    db = Database(proof, "importing")
    db.is_connected = True
    monkeypatch.setattr(runtime.ConversationRuntime, "verify", AsyncMock())
    with pytest.raises(IntegrityError, match="ready"):
        await runtime.configure_runtime(db)
    runtime.ConversationRuntime.verify.assert_not_awaited()


def test_backup_v3_requires_scoped_authority(tmp_path):
    from swe.app.conversation_storage import backup

    root = tmp_path / "backup"
    root.mkdir()
    backup._atomic_json(
        root / "metadata.json",
        {"version": 3, "backup_id": "x"},
    )
    with pytest.raises(IntegrityError, match="authority"):
        backup.load_backup(root)


@pytest.mark.parametrize("kind", ["missing", "symlink", "parent-symlink"])
async def test_restore_rejects_unusable_workspace_roots(tmp_path, kind):
    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="svc")
    target = tmp_path / "workspace"
    if kind != "missing":
        real = tmp_path / "real"
        real.mkdir()
        target.symlink_to(real, target_is_directory=True)
        if kind == "parent-symlink":
            (real / "child").mkdir()
            target = target / "child"
    db = Database()
    with pytest.raises(IntegrityError, match="workspace"):
        await authority.restore_authorities(
            db,
            [receipt(tmp_path)],
            [scope],
            {"svc": str(tmp_path)},
            workspace_roots={scope.values: str(target)},
        )
    db.insert.assert_not_awaited()
