# -*- coding: utf-8 -*-
import json
from types import SimpleNamespace

import pytest

from swe.app.conversation_storage.chat_mutations import (
    ChatMetadataInstall,
    install_chat_metadata,
    split_metadata,
    validate_light_metadata,
)
from swe.app.conversation_storage.bundle import BundleIndex
from swe.app.conversation_storage.errors import IntegrityError
from swe.app.conversation_storage.identity import WorkspaceScope


def test_metadata_routes_unknown_and_content_fields_to_nas_and_binding_to_typed_columns():
    source = {
        "plan_mode_enabled": False,
        "session_kind": "task",
        "scenario_snapshot": {"prompt": "private instructions"},
        "unknown_future_module": {"body": "must survive"},
        "expert_main_binding": {"revision": 3, "expert_id": "expert"},
    }
    light, content = split_metadata(source)
    assert light == {"plan_mode_enabled": False, "session_kind": "task"}
    assert content == {
        "scenario_snapshot": {"prompt": "private instructions"},
        "unknown_future_module": {"body": "must survive"},
    }
    assert source["expert_main_binding"]["revision"] == 3


def test_metadata_install_roundtrips_body_free_structural_plan():
    plan = ChatMetadataInstall(
        external_chat_key="chat",
        user_id="",
        channel="console",
        title="title",
        metadata_text="{}",
        content_meta_block="content_meta",
        created_at="2026-10-30T00:00:00Z",
        updated_at="2026-10-30T00:00:00Z",
    )
    assert ChatMetadataInstall.from_dict(plan.to_dict()) == plan
    assert plan.expected_metadata_revision is None
    assert "body" not in json.dumps(plan.to_dict())


@pytest.mark.parametrize(
    "metadata",
    [
        {"scenario_snapshot": "private body"},
        {"unknown": "body"},
        {"plan_mode_enabled": "yes"},
        {"session_kind": {"body": "private"}},
    ],
)
def test_structural_metadata_install_refuses_non_whitelisted_content(metadata):
    with pytest.raises(ValueError, match="metadata"):
        ChatMetadataInstall(
            external_chat_key="chat",
            user_id="",
            channel="console",
            title="title",
            metadata_text=json.dumps(metadata),
            created_at="2026-10-30T00:00:00Z",
            updated_at="2026-10-30T00:00:00Z",
        )


def test_structural_metadata_install_rejects_unknown_fields_and_identity_overflow():
    with pytest.raises(ValueError):
        ChatMetadataInstall.from_dict({"action": "run arbitrary SQL"})
    with pytest.raises(ValueError, match="byte limit"):
        ChatMetadataInstall(
            external_chat_key="中" * 43,
            user_id="",
            channel="console",
            title="title",
            metadata_text="{}",
            created_at="2026-10-30T00:00:00Z",
            updated_at="2026-10-30T00:00:00Z",
        )


async def test_create_plan_rejects_target_session_override_before_database_write():
    plan = ChatMetadataInstall(
        external_chat_key="chat",
        user_id="other",
        channel="console",
        title="title",
        metadata_text="{}",
        created_at="2026-10-30T00:00:00Z",
        updated_at="2026-10-30T00:00:00Z",
        target_session_id=2,
    )
    request = SimpleNamespace(
        session_id=1,
        chat_id=None,
        turn_id=None,
        scope=WorkspaceScope("t", "s", "a"),
    )
    source = {"id": 1, "user_key": b"original", "channel_key": b"console"}
    target = {"id": 2, "user_key": b"other", "channel_key": b"console"}
    with pytest.raises(IntegrityError, match="target"):
        await install_chat_metadata(
            None,
            request,
            plan,
            {"id": 3},
            BundleIndex("p", 0, {}, {}),
            (source, None, None),
            target_session=target,
        )


def test_light_metadata_rejects_duplicate_authority_fields():
    with pytest.raises(ValueError, match="duplicate"):
        validate_light_metadata(
            '{"plan_mode_enabled":false,"plan_mode_enabled":true}',
        )
