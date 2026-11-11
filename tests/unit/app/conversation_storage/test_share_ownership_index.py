# -*- coding: utf-8 -*-
import pytest

from swe.app.conversation_storage.migration.discovery import SHARE_FIELDS
from swe.app.conversation_storage.migration.inventory_v3 import (
    _inventory_shares,
)


class CountedChats(list):
    def __init__(self, values):
        super().__init__(values)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


@pytest.mark.parametrize("matches", [0, 1, 2])
def test_share_ownership_index_preserves_zero_unique_and_ambiguous_matches(
    tmp_path,
    matches,
):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text('{"messages": []}')
    sources = []
    for index in range(100):
        source = {key: None for key in SHARE_FIELDS}
        source.update(
            position="share:" + str(index),
            snapshot_path=str(snapshot),
            service_unit_id="service",
            tenant_id="tenant",
            chat_id="same-id",
        )
        sources.append(source)
    lists = []
    workspaces = []
    for index in range(matches):
        chats = CountedChats(
            [{"spec": {"id": "same-id"}, "position": "chat:" + str(index)}],
        )
        lists.append(chats)
        workspaces.append(
            {
                "scope": ["service", "tenant", "source", str(index)],
                "chats": chats,
            },
        )
    # Identical external IDs in another service and tenant must not count.
    for service, tenant in (("other", "tenant"), ("service", "other")):
        chats = CountedChats(
            [{"spec": {"id": "same-id"}, "position": "foreign"}],
        )
        lists.append(chats)
        workspaces.append(
            {"scope": [service, tenant, "source", "agent"], "chats": chats},
        )
    report = {"workspaces": workspaces, "shares": [], "blockers": []}
    _inventory_shares({"shares": sources}, report)
    assert all(chats.iterations == 1 for chats in lists)
    if matches == 2:
        assert report["shares"] == []
        assert len(report["blockers"]) == 100
        assert {item["code"] for item in report["blockers"]} == {
            "share_invalid",
        }
    else:
        assert report["blockers"] == []
        assert len(report["shares"]) == 100
        assert {item["scope_kind"] for item in report["shares"]} == {
            "workspace" if matches else "legacy_share",
        }
        assert {item["chat_position"] for item in report["shares"]} == {
            "chat:0" if matches else None,
        }
