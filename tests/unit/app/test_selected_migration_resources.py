# -*- coding: utf-8 -*-
"""迁入原会话可复用，但不能新建替代ID或删除共享资源。"""

from types import SimpleNamespace
import pytest
from tests.unit.app.test_managed_cron_resources import Chats, spec, valid_model


def migrated():
    value = spec()
    value["meta"]["task_chat_id"] = "original-chat"
    value["meta"]["task_session_id"] = "original-session"
    return value, {
        "task_chat_id": "original-chat",
        "task_session_id": "original-session",
        "_migration": True,
    }


@pytest.mark.asyncio
async def test_migrated_prepare_reuses_legacy_binding_without_relabeling_chat():
    from swe.app.crons.managed_resources import ResourcePreparer

    chats = Chats()
    value, binding = migrated()
    service = ResourcePreparer(
        chats,
        agent_id="default",
        validate_model=valid_model,
    )
    meta = service._route(
        SimpleNamespace(
            id=value["id"],
            tenant_id="alice",
            source_id="bank",
            scope_id=value["scope_id"],
        ),
        "alice",
    )
    meta.pop("management_owner")
    meta.pop("task_agent_id")
    chats.rows["original-chat"] = SimpleNamespace(
        id="original-chat",
        user_id="alice",
        session_id="original-session",
        name="old",
        meta=meta,
    )
    result = await service.prepare(value, binding)
    assert (
        result["chat_id"] == "original-chat"
        and result["session_id"] == "original-session"
    )
    assert chats.created == 0
    assert chats.rows["original-chat"].meta == meta


@pytest.mark.asyncio
async def test_migrated_cleanup_preserves_chat_even_for_direct_resource_call():
    from swe.app.crons.managed_resources import ResourcePreparer

    chats = Chats()
    value, binding = migrated()
    chats.rows["original-chat"] = object()
    result = await ResourcePreparer(chats, agent_id="default").cleanup(
        value,
        binding,
    )
    assert result == {"cleaned": True, "original_resources_preserved": True}
    assert "original-chat" in chats.rows and chats.deleted == []


@pytest.mark.asyncio
async def test_migrated_prepare_does_not_recreate_missing_chat():
    from swe.app.crons.managed_resources import ResourcePreparer

    chats = Chats()
    value, binding = migrated()
    with pytest.raises(ValueError, match="cron_migration_chat_missing"):
        await ResourcePreparer(
            chats,
            agent_id="default",
            validate_model=valid_model,
        ).prepare(value, binding)
    assert chats.created == 0
