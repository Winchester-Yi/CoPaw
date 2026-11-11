# -*- coding: utf-8 -*-
import json

from swe.app.conversation_storage.migration.discovery import source_key
from swe.app.conversation_storage.migration.inventory_parallel import (
    spool_workspaces,
)
from swe.app.conversation_storage.migration.inventory_v3 import (
    inventory_discovery,
)
from swe.app.conversation_storage.migration.spool import InventorySpool


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
    with InventorySpool(
        tmp_path,
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
