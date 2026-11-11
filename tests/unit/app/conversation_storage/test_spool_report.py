# -*- coding: utf-8 -*-
import json
from unittest.mock import Mock

import pytest

from swe.app.conversation_storage.migration.importer import validate_inventory
from swe.app.conversation_storage.migration.inventory_parallel import (
    spool_workspaces,
)
from swe.app.conversation_storage.migration.inventory_v3 import (
    inventory_discovery,
)
from swe.app.conversation_storage.migration.spool import InventorySpool
from swe.app.conversation_storage.migration.spool_report import spool_report


def source_data(tmp_path):
    from swe.app.runner.models import ChatSpec, ChatsFile

    mappings = []
    for index in range(4):
        root = tmp_path / str(index)
        (root / "sessions").mkdir(parents=True)
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
                    "unknown": {"body": ["正文", None, 42]},
                },
            ),
        )
        for path in root.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        root.chmod(0o555)
        mappings.append(
            {
                "root": str(root),
                "position": "workspace:" + str(index),
                "service_unit_id": "service",
                "tenant_id": "tenant",
                "source_id": "source",
                "agent_id": str(index),
            },
        )
    return {"workspaces": mappings, "shares": [], "fingerprints": []}


async def test_spool_report_equals_serial_and_repeats_random_access(tmp_path):
    data = source_data(tmp_path)
    expected = inventory_discovery(data)
    expected["source_boundary"] = {"snapshot_id": "frozen"}
    with InventorySpool(
        tmp_path,
        max_bytes=1024 * 1024,
        record_limit=128 * 1024,
    ) as spool:
        summaries = await spool_workspaces(data, spool)
        report = spool_report(
            spool,
            summaries,
            shares=[],
            source_boundary=expected["source_boundary"],
        )
        validate_inventory(report)
        for _ in range(2):
            actual = {
                **report,
                "workspaces": list(report["workspaces"]),
                "files": list(report["files"]),
            }
            assert actual == expected
        workspaces = report["workspaces"]
        for index in (3, 0, 2, 1, -1):
            assert workspaces[index] == expected["workspaces"][index]
        assert len(workspaces) == 4
        with pytest.raises(IndexError):
            workspaces.__getitem__(4)
        with pytest.raises(TypeError):
            workspaces.__getitem__(slice(None, 2))


async def test_files_share_only_one_cached_record(tmp_path):
    data = source_data(tmp_path)
    with InventorySpool(
        tmp_path,
        max_bytes=1024 * 1024,
        record_limit=128 * 1024,
    ) as spool:
        summaries = await spool_workspaces(data, spool)
        spool.read = Mock(wraps=spool.read)
        report = spool_report(spool, summaries, shares=[], source_boundary={})
        workspaces = report["workspaces"]
        for item in report["files"]:
            workspace = workspaces[item["workspace"]]
            assert workspaces._cached_index == item["workspace"]
            assert workspaces._cached_record["workspaces"][0] is workspace
        assert spool.read.call_count == 4
        assert workspaces[0]["position"] == "workspace:0"
        assert workspaces[3]["position"] == "workspace:3"
        assert spool.read.call_count == 6
        assert workspaces._cached_index == 3
        assert len(workspaces._cached_record["workspaces"]) == 1


async def test_corrupt_record_refused_on_access(tmp_path):
    data = source_data(tmp_path)
    with InventorySpool(
        tmp_path,
        max_bytes=1024 * 1024,
        record_limit=128 * 1024,
    ) as spool:
        summaries = await spool_workspaces(data, spool)
        report = spool_report(spool, summaries, shares=[], source_boundary={})
        (spool.root / "0.json").write_bytes(b"{}")
        with pytest.raises(ValueError, match="changed"):
            validate_inventory(report)
        assert report["workspaces"]._cached_record is None


async def test_blockers_aggregate_and_uncontrolled_sequence_rejected(tmp_path):
    data = source_data(tmp_path)
    from dataclasses import replace

    with InventorySpool(
        tmp_path,
        max_bytes=1024 * 1024,
        record_limit=128 * 1024,
    ) as spool:
        summaries = await spool_workspaces(data, spool)
        blocker = {"code": "source_invalid", "position": "workspace:0"}
        summaries[0] = replace(summaries[0], blockers=(blocker,))
        report = spool_report(
            spool,
            summaries,
            shares=[],
            source_boundary={},
            blockers=[{"code": "share_invalid"}],
        )
        assert report["blockers"] == [blocker, {"code": "share_invalid"}]
        with pytest.raises(ValueError, match="blockers"):
            validate_inventory(report)
    with pytest.raises(ValueError, match="incomplete"):
        validate_inventory(
            {
                "version": 2,
                "ready": True,
                "blockers": [],
                "workspaces": (),
                "shares": [],
            },
        )
