# -*- coding: utf-8 -*-
# pylint: disable=use-dict-literal
"""Several scope backups restore together, never by weakening empty-target checks."""

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from swe.app.conversation_storage import backup
from swe.app.conversation_storage.bundle import BundleStore


def make_backup(path, tenant, source, identity, *, shared_state=None):
    path.mkdir()
    (path / "nas").mkdir()
    snapshot = {name: [] for name in backup.TABLES}
    snapshot["retentions"] = [
        {
            "id": identity,
            "scope_kind": "workspace",
            "service_unit_key": b"schema-test-service",
            "tenant_key": tenant.encode(),
            "source_key": source.encode(),
            "agent_key": b"a",
            "kind": "gc_guard",
            "state": "active",
            "publication_id": None,
            "released_at": None,
        },
    ]
    if shared_state:
        snapshot["retentions"].append(
            {
                "id": 99,
                "scope_kind": "legacy_share",
                "service_unit_key": b"schema-test-service",
                "tenant_key": tenant.encode(),
                "source_key": b"",
                "agent_key": b"",
                "kind": "backup_fence",
                "state": shared_state,
                "released_at": None,
                "publication_id": None,
            },
        )
    backup._atomic_json(
        path / "metadata.json",
        {
            "version": 2,
            "service_unit_id": "schema-test-service",
            "backup_id": path.name,
            "scope": [
                b"schema-test-service",
                tenant.encode(),
                source.encode(),
                b"a",
            ],
            "snapshot": snapshot,
            "pin_keys": [],
            "snapshot_ref": str(path),
            "source_schema": "same-source",
        },
    )
    return path


class MemoryTarget:
    config = SimpleNamespace(database="test_conversation_restore_" + "b" * 32)

    def __init__(self):
        self.rows = {table: [] for table in backup.TABLES.values()}
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        before = deepcopy(self.rows)
        try:
            yield self
        except BaseException:
            self.rows = before
            raise

    async def fetch_one(self, query, params=None):
        if "information_schema.TABLES" in query:
            return {"n": 0}
        if query == "SELECT DATABASE() AS name":
            return {"name": self.config.database}
        return {"n": len(self.rows[query.split()[-1]])}

    async def fetch_all(self, query, params=None):
        if query.startswith("SELECT COLUMN_NAME"):
            return [
                {"COLUMN_NAME": key}
                for key in (
                    "id",
                    "scope_kind",
                    "service_unit_key",
                    "tenant_key",
                    "source_key",
                    "agent_key",
                    "kind",
                    "state",
                    "publication_id",
                    "released_at",
                )
            ]
        return deepcopy(self.rows[query.split()[3]])

    async def insert(self, query, params):
        table = query.split()[2]
        columns = query.split("(", 1)[1].split(")", 1)[0].split(",")
        self.rows[table].append(dict(zip(columns, params)))


async def test_restore_all_scopes_to_one_empty_target(tmp_path, monkeypatch):
    paths = [
        make_backup(tmp_path / "a", "t", "s1", 1),
        make_backup(tmp_path / "b", "t", "s2", 2),
        make_backup(tmp_path / "c", "other", "s", 3),
    ]
    target = MemoryTarget()
    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    result = await backup.restore_backup(
        target,
        BundleStore(tmp_path / "nas"),
        paths,
        evidence=backup.RestoreEvidence("stopped-all", "isolated"),
    )
    assert result["passed"] and result["restore_ready"]
    assert result["scopes"] == [
        ["schema-test-service", "t", "s1", "a"],
        ["schema-test-service", "t", "s2", "a"],
        ["schema-test-service", "other", "s", "a"],
    ]
    assert result["backup_ids"] == ["a", "b", "c"]
    assert [row["id"] for row in target.rows[backup.TABLES["retentions"]]] == [
        1,
        2,
        3,
    ]
    assert target.transactions == 1
    with pytest.raises(backup.ConflictError, match="empty metadata"):
        await backup.restore_backup(
            target,
            BundleStore(tmp_path / "second-nas"),
            paths,
            evidence=backup.RestoreEvidence("stopped-all", "isolated"),
        )


@pytest.mark.parametrize("verification_fails", [False, True])
async def test_restore_receipt_commits_only_after_complete_verification(
    tmp_path,
    monkeypatch,
    verification_fails,
):
    from swe.app.conversation_storage.migration import authority

    path = make_backup(tmp_path / "backup", "t", "s", 1)
    document = backup.decode_metadata((path / "metadata.json").read_text())
    scope = ["schema-test-service", "t", "s", "a"]
    proof = {
        "version": 1,
        "migration_id": "00000000-0000-4000-8000-000000000001",
        "services": {
            scope[0]: {
                "nas_root": str(tmp_path / "source"),
                "workspaces": [
                    {
                        "root": str(tmp_path / "source" / "workspace"),
                        "scope": scope,
                    },
                ],
            },
        },
    }
    document.update(version=3, migration_authority=proof)
    backup._atomic_json(path / "metadata.json", document)

    class AuthorityTarget(MemoryTarget):
        def __init__(self):
            super().__init__()
            self.rows["swe_conversation_migration_jobs"] = []

        async def fetch_one(self, query, params=None):
            if "information_schema.TABLES" in query:
                return {"n": 2}
            if "@@server_uuid" in query:
                return {
                    "server_uuid": "restored-server",
                    "schema_name": self.config.database,
                }
            return await super().fetch_one(query, params)

    (tmp_path / "existing-workspace").mkdir()
    target = AuthorityTarget()
    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    original = backup._verify_restored_readers

    async def checked(*args):
        assert target.transactions == 1
        assert not target.rows["swe_conversation_migration_jobs"]
        if verification_fails:
            raise backup.IntegrityError("reader verification failed")
        await original(*args)

    monkeypatch.setattr(backup, "_verify_restored_readers", checked)
    kwargs = dict(
        evidence=backup.RestoreEvidence("stopped", "isolated"),
        workspace_roots={tuple(scope): str(tmp_path / "existing-workspace")},
    )
    if verification_fails:
        with pytest.raises(backup.IntegrityError, match="reader verification"):
            await backup.restore_backup(
                target,
                BundleStore(tmp_path / "new-nas"),
                path,
                **kwargs,
            )
        assert all(not rows for rows in target.rows.values())
    else:
        result = await backup.restore_backup(
            target,
            BundleStore(tmp_path / "new-nas"),
            path,
            **kwargs,
        )
        restored = authority.validate_receipt(result["migration_authority"])
        assert restored["migration_id"] != proof["migration_id"]
        assert (
            target.rows["swe_conversation_migration_jobs"][0]["state"]
            == "ready"
        )
        assert result["runtime_manifests"][scope[0]]["workspaces"][0][
            "root"
        ] == str(tmp_path / "existing-workspace")


@pytest.mark.parametrize("conflict", ["scope", "schema", "id"])
async def test_restore_set_conflicts_fail_before_target_mutation(
    tmp_path,
    monkeypatch,
    conflict,
):
    first = make_backup(tmp_path / "a", "t", "s1", 1)
    second = make_backup(
        tmp_path / "b",
        "t",
        "s1" if conflict == "scope" else "s2",
        1 if conflict == "id" else 2,
    )
    if conflict == "schema":
        value = backup.load_backup(second)
        value["source_schema"] = "another-source"
        backup._atomic_json(second / "metadata.json", value)
    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    target = MemoryTarget()
    with pytest.raises(backup.IntegrityError):
        await backup.restore_backup(
            target,
            BundleStore(tmp_path / "nas"),
            [first, second],
            evidence=backup.RestoreEvidence("stopped-all", "isolated"),
        )
    assert not any(target.rows.values())
    assert not (tmp_path / "nas").exists()


async def test_shared_fence_transition_deduplicates_only_stopped_owner_state(
    tmp_path,
    monkeypatch,
):
    paths = [
        make_backup(tmp_path / "a", "t", "s1", 1, shared_state="active"),
        make_backup(tmp_path / "b", "t", "s2", 2, shared_state="released"),
    ]
    target = MemoryTarget()
    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    report = await backup.restore_backup(
        target,
        BundleStore(tmp_path / "nas"),
        paths,
        evidence=backup.RestoreEvidence("stopped-all", "isolated"),
    )
    assert report["passed"]
    rows = target.rows[backup.TABLES["retentions"]]
    assert len(rows) == 3
    assert rows[-1]["id"] == 99 and rows[-1]["state"] == "released"


async def test_multi_scope_install_failure_rolls_back_whole_set(
    tmp_path,
    monkeypatch,
):
    paths = [
        make_backup(tmp_path / "a", "t", "s1", 1),
        make_backup(tmp_path / "b", "t", "s2", 2),
    ]
    target = MemoryTarget()
    original = target.insert

    async def fail_second(query, params):
        if params[0] == 2:
            raise RuntimeError("second scope insert failed")
        await original(query, params)

    target.insert = fail_second
    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    with pytest.raises(RuntimeError, match="second scope"):
        await backup.restore_backup(
            target,
            BundleStore(tmp_path / "nas"),
            paths,
            evidence=backup.RestoreEvidence("stopped-all", "isolated"),
        )
    assert not any(target.rows.values())


@pytest.mark.parametrize("different", [False, True])
def test_shared_bundle_comparison_checks_bytes(tmp_path, different):
    from swe.app.conversation_storage.backup_set import _same_bundle
    from swe.app.conversation_storage.bundle import (
        BundleBlock,
        new_operation_key,
    )

    operation = new_operation_key()
    left, right = BundleStore(tmp_path / "left"), BundleStore(
        tmp_path / "right",
    )
    left.root.mkdir()
    right.root.mkdir()
    index = left.publish(
        operation,
        [BundleBlock("share", "share", b'{"value":1}')],
        metadata={"operation_key": operation},
    )
    right.publish(
        operation,
        [
            BundleBlock(
                "share",
                "share",
                b'{"value":2}' if different else b'{"value":1}',
            ),
        ],
        metadata={"operation_key": operation},
    )
    row = {"relative_path": index.relative_path.encode()}
    if different:
        with pytest.raises(backup.IntegrityError, match="bundle bytes differ"):
            _same_bundle(left, right, row)
    else:
        _same_bundle(left, right, row)


async def test_duplicate_row_inside_one_scope_is_not_silently_deduplicated(
    tmp_path,
    monkeypatch,
):
    path = make_backup(tmp_path / "a", "t", "s", 1)
    document = backup.load_backup(path)
    document["snapshot"]["retentions"] *= 2
    backup._atomic_json(path / "metadata.json", document)
    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    with pytest.raises(backup.IntegrityError, match="duplicate metadata"):
        await backup.restore_backup(
            MemoryTarget(),
            BundleStore(tmp_path / "nas"),
            path,
            evidence=backup.RestoreEvidence("stopped", "isolated"),
        )
    assert not (tmp_path / "nas").exists()


def change_service(path, service):
    document = backup.load_backup(path)
    document["service_unit_id"] = service
    document["scope"][0] = service.encode()
    for rows in document["snapshot"].values():
        for row in rows:
            row["service_unit_key"] = service.encode()
    backup._atomic_json(path / "metadata.json", document)


async def test_mixed_service_restore_rejects_single_root_before_mutation(
    tmp_path,
):
    paths = [
        make_backup(tmp_path / "a", "t", "s", 1),
        make_backup(tmp_path / "b", "t", "s", 2),
    ]
    change_service(paths[1], "service-B")
    target = MemoryTarget()
    with pytest.raises(backup.IntegrityError, match="NAS mapping"):
        await backup.restore_backup(
            target,
            BundleStore(tmp_path / "target"),
            paths,
            evidence=backup.RestoreEvidence("all-stopped", "isolated"),
        )
    assert target.transactions == 0
    assert not (tmp_path / "target").exists()


async def test_mixed_service_restore_installs_one_transaction_with_disjoint_roots(
    tmp_path,
    monkeypatch,
):
    paths = [
        make_backup(tmp_path / "a", "t", "s", 1),
        make_backup(tmp_path / "b", "t", "s", 2),
    ]
    change_service(paths[1], "service-B")
    target = MemoryTarget()
    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    from unittest.mock import AsyncMock

    monkeypatch.setattr(backup, "_verify_restored_readers", AsyncMock())
    stores = {
        "schema-test-service": BundleStore(tmp_path / "target-A"),
        "service-B": BundleStore(tmp_path / "target-B"),
    }
    report = await backup.restore_backup(
        target,
        stores,
        paths,
        evidence=backup.RestoreEvidence("all-stopped", "isolated"),
    )
    assert report["passed"] and target.transactions == 1
    assert set(report["target_nas_roots"]) == set(stores)


@pytest.mark.parametrize("mapping", ["missing", "overlap"])
async def test_restore_validates_entire_service_root_mapping_before_mutation(
    tmp_path,
    mapping,
):
    paths = [
        make_backup(tmp_path / "a", "t", "s", 1),
        make_backup(tmp_path / "b", "t", "s", 2),
    ]
    change_service(paths[1], "service-B")
    stores = {"schema-test-service": BundleStore(tmp_path / "target")}
    if mapping == "overlap":
        stores["service-B"] = BundleStore(tmp_path / "target" / "nested")
    with pytest.raises(backup.IntegrityError):
        await backup.restore_backup(
            MemoryTarget(),
            stores,
            paths,
            evidence=backup.RestoreEvidence("all-stopped", "isolated"),
        )
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize(
    "change",
    ["old-version", "foreign-service", "old-scope"],
)
def test_backup_requires_v2_exact_service_scope(tmp_path, change):
    path = make_backup(tmp_path / "a", "t", "s", 1)
    document = backup.load_backup(path)
    if change == "old-version":
        document["version"] = 1
    elif change == "foreign-service":
        document["service_unit_id"] = "another-service"
    else:
        document["scope"] = document["scope"][1:]
    backup._atomic_json(path / "metadata.json", document)
    with pytest.raises((ValueError, backup.IntegrityError)):
        backup.load_backup(path)


async def test_collation_equivalent_tokens_fail_before_restore_nas_mutation(
    tmp_path,
    monkeypatch,
):
    from unittest.mock import Mock
    from swe.app.conversation_storage import backup_set

    document = {"scope": [b"A", b"t", b"s", b"a"], "source_schema": "source"}
    captured = {name: [] for name in backup.TABLES}
    captured["shares"] = [{"token": "ShareToken"}, {"token": "sharetoken"}]
    monkeypatch.setattr(
        backup_set,
        "load_restore_set",
        lambda *_: ([document], captured, {}),
    )
    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    copy = Mock(side_effect=AssertionError("copy started"))
    monkeypatch.setattr(backup, "_copy_bundle", copy)

    class CollatedTarget(MemoryTarget):
        async def fetch_one(self, query, params=None):
            if "COLLATION_NAME" in query:
                return {
                    "CHARACTER_SET_NAME": "utf8mb4",
                    "COLLATION_NAME": "utf8mb4_general_ci",
                }
            if "HAVING COUNT(*)>1" in query:
                assert "COLLATE utf8mb4_general_ci" in query
                assert params == ("ShareToken", "sharetoken")
                return {"token": "ShareToken"}
            return await super().fetch_one(query, params)

    target = tmp_path / "restore-nas"
    with pytest.raises(backup.IntegrityError, match="token.*collation"):
        await backup.restore_backup(
            CollatedTarget(),
            BundleStore(target),
            tmp_path / "backup",
            evidence=backup.RestoreEvidence("stopped", "isolated"),
        )
    assert not target.exists()
    copy.assert_not_called()


@pytest.mark.parametrize("collation", ["utf8mb4_bin", "utf8mb4_general_ci"])
async def test_restore_token_preflight_uses_server_equality(collation):
    from swe.app.conversation_storage.backup_set import validate_restore_tokens
    from unittest.mock import AsyncMock

    database = SimpleNamespace(
        fetch_one=AsyncMock(
            side_effect=[
                {"CHARACTER_SET_NAME": "utf8mb4", "COLLATION_NAME": collation},
                None,
            ],
        ),
    )
    await validate_restore_tokens(
        database,
        {"shares": [{"token": "One"}, {"token": "Two"}]},
    )
    query, values = database.fetch_one.await_args.args
    assert f"COLLATE {collation}" in query
    assert "GROUP BY token HAVING COUNT(*)>1" in query
    assert values == ("One", "Two")


async def test_restore_token_preflight_bounds_queries_and_checks_cross_batch():
    from unittest.mock import AsyncMock
    from swe.app.conversation_storage.backup_set import validate_restore_tokens

    database = SimpleNamespace(
        fetch_one=AsyncMock(
            side_effect=[
                {
                    "CHARACTER_SET_NAME": "utf8mb4",
                    "COLLATION_NAME": "utf8mb4_general_ci",
                },
                None,
                None,
                {"token": "collision"},
            ],
        ),
    )
    with pytest.raises(backup.IntegrityError, match="token.*collation"):
        await validate_restore_tokens(
            database,
            {"shares": [{"token": str(index)} for index in range(257)]},
        )
    queries = database.fetch_one.await_args_list[1:]
    assert max(len(call.args[1]) for call in queries) <= 512
    assert len(queries[-1].args[1]) == 257


@pytest.mark.parametrize("occupied", [None, "jobs", "entries"])
async def test_old_backup_restore_with_precreated_controls(
    tmp_path,
    monkeypatch,
    occupied,
):
    path = make_backup(tmp_path / "old", "t", "s", 1)

    class Target(MemoryTarget):
        async def fetch_one(self, query, params=None):
            if "information_schema.TABLES" in query:
                return {"n": 2}
            if "swe_conversation_migration_" in query:
                return {"n": int(occupied in query)} if occupied else {"n": 0}
            return await super().fetch_one(query, params)

        async def fetch_all(self, query, params=None):
            if "swe_conversation_migration_" in query:
                return [{"id": 1}] if occupied and occupied in query else []
            return await super().fetch_all(query, params)

    target = Target()
    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    if occupied:
        with pytest.raises(backup.IntegrityError, match="migration authority"):
            await backup.restore_backup(
                target,
                BundleStore(tmp_path / "nas"),
                path,
                evidence=backup.RestoreEvidence("stopped", "isolated"),
            )
        assert not (tmp_path / "nas").exists()
    else:
        result = await backup.restore_backup(
            target,
            BundleStore(tmp_path / "nas"),
            path,
            evidence=backup.RestoreEvidence("stopped", "isolated"),
        )
        assert result["passed"]


async def test_old_backup_rejects_partial_controls(tmp_path, monkeypatch):
    path = make_backup(tmp_path / "old", "t", "s", 1)

    class Target(MemoryTarget):
        async def fetch_one(self, query, params=None):
            if "information_schema.TABLES" in query:
                return {"n": 1}
            return await super().fetch_one(query, params)

    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    with pytest.raises(
        backup.IntegrityError,
        match="both migration control tables",
    ):
        await backup.restore_backup(
            Target(),
            BundleStore(tmp_path / "nas"),
            path,
            evidence=backup.RestoreEvidence("stopped", "isolated"),
        )
    assert not (tmp_path / "nas").exists()


async def test_old_backup_rechecks_controls_in_transaction(
    tmp_path,
    monkeypatch,
):
    path = make_backup(tmp_path / "old", "t", "s", 1)

    class Target(MemoryTarget):
        async def fetch_one(self, query, params=None):
            if "information_schema.TABLES" in query:
                return {"n": 2}
            return await super().fetch_one(query, params)

        async def fetch_all(self, query, params=None):
            if "swe_conversation_migration_" in query:
                return [{"id": 1}] if "FOR UPDATE" in query else []
            return await super().fetch_all(query, params)

    target = Target()
    monkeypatch.setattr(backup, "_restore_target", lambda *_: None)
    with pytest.raises(backup.IntegrityError, match="migration authority"):
        await backup.restore_backup(
            target,
            BundleStore(tmp_path / "nas"),
            path,
            evidence=backup.RestoreEvidence("stopped", "isolated"),
        )
    assert all(not rows for rows in target.rows.values())
