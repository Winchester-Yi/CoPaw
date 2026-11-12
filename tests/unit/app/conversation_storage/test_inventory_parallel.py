# -*- coding: utf-8 -*-
import json
import io
import threading

import pytest

from swe.app.conversation_storage.migration.discovery import source_key
from swe.app.conversation_storage.migration.inventory_parallel import (
    spool_workspaces,
)
from swe.app.conversation_storage.migration.inventory_v3 import (
    inventory_discovery,
)
from swe.app.conversation_storage.migration.spool import InventorySpool
from swe.app.conversation_storage.migration.progress import MigrationProgress


@pytest.mark.parametrize("count", [2, 4])
async def test_preflight_uses_shared_io_and_inventory_counters(
    tmp_path,
    monkeypatch,
    count,
):
    from swe.app.conversation_storage.migration import inventory_parallel

    owner_thread = threading.get_ident()
    barrier, lock = threading.Barrier(2), threading.Lock()
    active = peak = 0
    source_bytes = 0
    mappings = []
    for index in range(count):
        root = tmp_path / str(index)
        root.mkdir()
        content = b"source" * (index + 1)
        (root / "chats.json").write_bytes(content)
        source_bytes += len(content)
        mappings.append(
            {
                "root": str(root),
                "position": str(index),
                "service_unit_id": "svc",
                "tenant_id": "tenant",
            },
        )
    original = inventory_parallel._preflight_workspace

    def preflight(workspace, allowance):
        nonlocal active, peak
        assert threading.get_ident() != owner_thread
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait(timeout=5)
            return original(workspace, allowance)
        finally:
            with lock:
                active -= 1

    def parse(_data, index):
        return {
            "workspaces": [
                {
                    "scope": ["svc", "tenant", "source", str(index)],
                    "position": str(index),
                    "sessions": [{}] * (index + 1),
                    "chats": [{}] * (index + 2),
                },
            ],
            "files": [],
            "blockers": [{"code": "fixture"}] if index == 0 else [],
        }

    monkeypatch.setattr(inventory_parallel, "_preflight_workspace", preflight)
    monkeypatch.setattr(inventory_parallel, "inventory_workspace", parse)
    progress = MigrationProgress(
        "inventory",
        "migration",
        stream=io.StringIO(),
    )
    with progress.activate():
        for _pass in range(2):
            with InventorySpool(
                tmp_path,
                max_bytes=1024 * 1024,
                record_limit=128 * 1024,
            ) as spool:
                await spool_workspaces(
                    {"workspaces": mappings},
                    spool,
                    workers=4,
                    io_workers=2,
                )
            snapshot = progress.snapshot()
            assert snapshot["inventory_completed_workspaces"] == count
            assert snapshot["inventoried_sessions"] == sum(range(1, count + 1))
            assert snapshot["inventoried_chats"] == sum(range(2, count + 2))
            assert snapshot["inventoried_source_bytes"] == source_bytes
            assert snapshot["inventory_blocker_count"] == 1
            assert snapshot["counter_unit"] == "tasks"
    assert peak == 2


async def test_parallel_workspace_spool_matches_serial_inventory(tmp_path):
    mappings = []
    for index in range(12):
        root = tmp_path / str(index)
        root.mkdir()
        (root / "chats.json").write_text(json.dumps({"chats": []}))
        (root / "agent.json").write_text(json.dumps({"id": str(index)}))
        for path in root.iterdir():
            path.chmod(0o444)
        root.chmod(0o555)
        mappings.append(
            {
                "service_unit_id": "service",
                "tenant_id": "tenant",
                "source_id": "source",
                "agent_id": str(index),
                "root": str(root),
                "position": source_key("workspace", str(index)),
            },
        )
    data = {"workspaces": mappings, "shares": [], "fingerprints": []}
    expected = inventory_discovery(data)
    spool_base = tmp_path / "spool-base"
    spool_base.mkdir()
    with InventorySpool(
        spool_base,
        max_bytes=1024 * 1024,
        record_limit=128 * 1024,
    ) as spool:
        summaries = await spool_workspaces(data, spool)
        assert len(summaries) == 12
        workspaces, files, blockers = [], [], []
        for summary in summaries:
            record = spool.read(summary.record)
            workspaces.extend(record["workspaces"])
            files.extend(record["files"])
            blockers.extend(record["blockers"])
        assert workspaces == expected["workspaces"]
        assert files == expected["files"]
        assert blockers == expected["blockers"]
        assert {item["workspace"] for item in files} == set(range(12))


async def test_nonempty_session_roundtrip_and_real_parser_overlap(
    tmp_path,
    monkeypatch,
):
    import threading
    from swe.app.runner.models import ChatSpec, ChatsFile
    from swe.app.conversation_storage.migration import inventory_parallel

    mappings = []
    for index in range(4):
        root = tmp_path / ("source-" + str(index))
        root.mkdir()
        (root / "sessions").mkdir()
        chat = ChatSpec(
            session_id="session",
            user_id="user",
            channel="console",
        )
        (root / "chats.json").write_text(
            ChatsFile(chats=[chat]).model_dump_json(),
        )
        (root / "sessions/user_session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "unknown_module": {"content": ["正文", 123, None]},
                },
            ),
        )
        for path in root.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        root.chmod(0o555)
        mappings.append(
            {
                "service_unit_id": "service",
                "tenant_id": "tenant",
                "source_id": "source",
                "agent_id": str(index),
                "root": str(root),
                "position": source_key("workspace", str(index)),
            },
        )
    data = {"workspaces": mappings, "shares": [], "fingerprints": []}
    expected = inventory_discovery(data)
    barrier = threading.Barrier(2)
    original = inventory_parallel.inventory_workspace

    def overlapping(data, index):
        barrier.wait(timeout=3)
        return original(data, index)

    monkeypatch.setattr(inventory_parallel, "inventory_workspace", overlapping)
    with InventorySpool(
        tmp_path,
        max_bytes=1024 * 1024,
        record_limit=128 * 1024,
    ) as spool:
        summaries = await spool_workspaces(
            data,
            spool,
            workers=2,
            io_workers=2,
        )
        actual = [
            spool.read(item.record)["workspaces"][0] for item in summaries
        ]
        assert actual == expected["workspaces"]
        assert all(len(item["sessions"]) == 1 for item in actual)


async def test_invalid_workers_rejected_before_parsing(tmp_path):
    import pytest

    with InventorySpool(tmp_path, max_bytes=1024, record_limit=1024) as spool:
        with pytest.raises(ValueError, match="workers"):
            await spool_workspaces({"workspaces": []}, spool, workers=0)


async def test_workspace_input_limit_fails_before_parser_allocation(
    tmp_path,
    monkeypatch,
):
    import pytest
    from swe.app.conversation_storage.migration import inventory_parallel
    from swe.app.conversation_storage.migration.spool import (
        MigrationCapacityError,
    )

    root = tmp_path / "large-workspace"
    root.mkdir()
    (root / "chats.json").write_bytes(b"x" * 2048)
    mapping = {
        "service_unit_id": "service",
        "tenant_id": "tenant",
        "source_id": "source",
        "agent_id": "agent",
        "root": str(root),
        "position": source_key("workspace", "large"),
    }
    data = {"workspaces": [mapping], "shares": [], "fingerprints": []}

    def forbidden(*_args):
        pytest.fail("oversized workspace reached the legacy parser")

    monkeypatch.setattr(inventory_parallel, "inventory_workspace", forbidden)
    spool_base = tmp_path / "spool"
    spool_base.mkdir()
    with InventorySpool(
        spool_base,
        max_bytes=1024 * 1024,
        record_limit=1024 * 1024,
    ) as spool:
        with pytest.raises(MigrationCapacityError, match="memory allowance"):
            await spool_workspaces(
                data,
                spool,
                workers=1,
                memory_bytes=16 * 1024,
            )


async def test_spooled_v3_inventory_matches_serial_inventory(tmp_path):
    from swe.app.conversation_storage.migration.inventory_v3 import (
        inventories_equal,
        inventory_v3_spooled,
    )
    from swe.app.conversation_storage.migration.spool_report import (
        spool_report,
    )

    roots = []
    mappings = []
    for index in range(5):
        root = tmp_path / ("workspace-" + str(index))
        root.mkdir()
        (root / "chats.json").write_text(json.dumps({"chats": []}))
        (root / "agent.json").write_text(json.dumps({"id": str(index)}))
        for path in root.iterdir():
            path.chmod(0o444)
        root.chmod(0o555)
        roots.append(root)
        mappings.append(
            {
                "service_unit_id": "service",
                "tenant_id": "tenant",
                "source_id": "source",
                "agent_id": str(index),
                "root": str(root),
                "position": source_key("workspace", str(index)),
            },
        )

    class Manifest:
        data = {
            "workspaces": mappings,
            "services": [],
            "shares": [],
            "fingerprints": [],
            "source_boundary": {
                "snapshot_id": "snapshot",
                "captured_at": "2026-11-12T00:00:00Z",
                "old_image_digest": "old@sha256:" + "a" * 64,
                "freeze_evidence": ["stopped", "readonly"],
            },
        }

    manifest = Manifest()
    expected = inventory_discovery(manifest.data)
    expected["source_boundary"] = manifest.data["source_boundary"]
    spool_base = tmp_path / "inventory-spool"
    spool_base.mkdir()
    with InventorySpool(
        spool_base,
        max_bytes=1024 * 1024,
        record_limit=128 * 1024,
        protected=roots,
    ) as spool:
        summaries = await spool_workspaces(
            manifest.data,
            spool,
            workers=2,
            io_workers=2,
        )
        actual = spool_report(
            spool,
            summaries,
            shares=[],
            source_boundary=manifest.data["source_boundary"],
        )
        assert inventories_equal(actual, expected)


def test_share_snapshot_budget_is_checked_before_json_read(tmp_path):
    import pytest
    from swe.app.conversation_storage.migration import inventory_v3

    snapshot = tmp_path / "share.json"
    snapshot.write_bytes(b"x" * 32)
    data = {
        "shares": [{"snapshot_path": str(snapshot)}],
    }

    with pytest.raises(ValueError, match="memory budget"):
        inventory_v3._preflight_share_budget(data, 1024)


def test_share_count_budget_covers_small_snapshot_metadata(tmp_path):
    import pytest
    from swe.app.conversation_storage.migration import inventory_v3

    snapshot = tmp_path / "share.json"
    snapshot.write_text("{}")
    data = {
        "shares": [{"snapshot_path": str(snapshot)}] * 17,
    }

    with pytest.raises(ValueError, match="memory budget"):
        inventory_v3._preflight_share_budget(data, 256 * 1024)


async def test_spooled_recheck_compares_before_temporary_records_close(
    tmp_path,
):
    from swe.app.conversation_storage.migration.inventory_v3 import (
        inventory_discovery,
    )
    from swe.cli.conversation_storage_v3_cmd import (
        _recheck_spooled_inventory,
    )

    root = tmp_path / "source"
    root.mkdir()
    (root / "chats.json").write_text(json.dumps({"chats": []}))
    (root / "agent.json").write_text(json.dumps({"id": "agent"}))
    for path in root.iterdir():
        path.chmod(0o444)
    root.chmod(0o555)
    mappings = [
        {
            "service_unit_id": "service",
            "tenant_id": "tenant",
            "source_id": "source",
            "agent_id": "agent",
            "root": str(root),
            "position": source_key("workspace", "agent"),
        },
    ]
    boundary = {
        "snapshot_id": "snapshot",
        "captured_at": "2026-11-12T00:00:00Z",
        "old_image_digest": "old@sha256:" + "a" * 64,
        "evidence_refs": ["stopped", "readonly"],
    }

    class Manifest:
        data = {
            "workspaces": mappings,
            "services": [],
            "shares": [],
            "fingerprints": [],
            "source_boundary": boundary,
        }

    expected = inventory_discovery(Manifest.data)
    expected["source_boundary"] = boundary
    base = tmp_path / "verification-spool"
    base.mkdir()
    assert await _recheck_spooled_inventory(
        Manifest(),
        expected,
        base,
        disk_limit=1024 * 1024,
        record_limit=128 * 1024,
        protected=[root],
        workers=1,
        io_workers=1,
        memory_bytes=1024 * 1024,
    )
