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
