# -*- coding: utf-8 -*-
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
            "version": 1,
            "backup_id": path.name,
            "scope": [tenant.encode(), source.encode(), b"a"],
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
        ["t", "s1", "a"],
        ["t", "s2", "a"],
        ["other", "s", "a"],
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
