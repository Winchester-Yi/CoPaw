# -*- coding: utf-8 -*-
import pytest

from swe.app.conversation_storage.migration.exporter import (
    OldFormatTarget,
    export_session_state,
)


def test_export_target_requires_pinned_image_and_explicit_supported_formats():
    with pytest.raises(ValueError):
        OldFormatTarget("latest", 2, 1)
    with pytest.raises(ValueError):
        OldFormatTarget("old@sha256:" + "a" * 64, 99, 1)


def test_export_session_preserves_all_modules_and_unknown_fields():
    state = {
        "revision": 7,
        "future": {"nested": [1, True]},
        "turn_states": {"q": {"status": "stopped", "extra": 2}},
    }
    actual = export_session_state(state, target_version=2)
    assert actual == {**state, "schema_version": 2}
    assert state.get("schema_version") is None


def test_export_session_does_not_silently_drop_ledger_metadata_for_unsupported_target():
    with pytest.raises(ValueError, match="ledger"):
        export_session_state(
            {"session_generation": "original"},
            target_version=2,
        )


@pytest.mark.parametrize("epoch", [1, 2])
@pytest.mark.parametrize("target_version", [1, 2])
async def test_export_advances_retained_v2_journal_instead_of_reusing_stale_root(
    tmp_path,
    epoch,
    target_version,
):
    import json
    from uuid import uuid4
    from agentscope.message import Msg
    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.app.conversation_storage.bundle import (
        BundleBlock,
        BundleStore,
        new_operation_key,
    )
    from swe.app.conversation_storage.migration.exporter import (
        ConversationExporter,
    )
    from swe.app.conversation_storage.migration.legacy import read_archive
    from datetime import datetime

    chat_id = str(uuid4())
    destination = tmp_path / "export"
    old = ConversationArchiveStore(
        destination / "dialog",
        cursor_secret=b"test",
    )
    old.migrate_checkpoint_v2(chat_id)
    message = Msg("assistant", "newly compacted", "assistant").to_dict()
    operation = new_operation_key()
    nas = tmp_path / "nas"
    nas.mkdir()
    bundles = BundleStore(nas)
    index = bundles.publish(
        operation,
        [
            BundleBlock("archive", "archive", json.dumps([message]).encode()),
            BundleBlock("message", "message", json.dumps(message).encode()),
        ],
    )
    extent = index.blocks["archive"]
    publication = {
        "id": 1,
        "chat_id": 1,
        "state": "committed",
        "gc_state": "live",
        "epoch": epoch,
        "boundary_order": 1,
        "boundary_key": str(uuid4()).encode(),
        "committed_at": datetime(2026, 10, 30),
        "archive_offset": extent.offset,
        "archive_length": extent.length,
        "archive_source_count": 1,
        "archive_display_count": 1,
        "archive_first_source_msg_key": message["id"].encode(),
        "archive_last_source_msg_key": message["id"].encode(),
        "archive_first_timestamp": None,
        "archive_last_timestamp": None,
        "checkpoint_offset": None,
        "event_offset": None,
        "relative_path": index.relative_path.encode(),
        "byte_length": index.byte_length,
    }
    exporter = object.__new__(ConversationExporter)
    exporter.target = OldFormatTarget(
        "old@sha256:" + "a" * 64,
        3,
        target_version,
    )
    exporter.bundles, exporter.publications = bundles, {1: publication}
    exporter.snapshot = {
        "publications": [publication],
        "messages": [
            {
                "id": 1,
                "archive_publication_id": 1,
                "archive_order": 1,
                "publication_id": 1,
                "byte_offset": index.blocks["message"].offset,
                "byte_length": index.blocks["message"].length,
            },
        ],
    }
    chat = {
        "id": 1,
        "external_chat_key": chat_id.encode(),
        "epoch": epoch,
        "created_at": datetime(2026, 10, 30),
        "deleted_at": None,
    }
    if target_version == 1:
        with pytest.raises(ValueError, match="retained journal"):
            exporter._archive(destination, chat)
        return
    exporter._archive(destination, chat)
    restored = read_archive(destination / "dialog", chat_id)
    assert restored["batches"][0]["messages"] == [message]
    assert len(restored["epochs"]) == epoch


def test_export_verification_rejects_missing_archive_occurrence(tmp_path):
    from uuid import uuid4
    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.app.conversation_storage.errors import IntegrityError
    from swe.app.conversation_storage.migration.exporter import (
        ConversationExporter,
    )

    chat = str(uuid4())
    store = ConversationArchiveStore(
        tmp_path / "dialog",
        cursor_secret=b"test",
    )
    store.migrate_checkpoint_v2(chat)
    exporter = object.__new__(ConversationExporter)
    exporter.snapshot = {
        "sessions": [],
        "shares": [],
        "chats": [
            {
                "id": 1,
                "external_chat_key": chat.encode(),
                "deleted_at": None,
            },
        ],
        "publications": [
            {
                "id": 1,
                "chat_id": 1,
                "state": "committed",
                "gc_state": "live",
                "archive_offset": 10,
                "boundary_key": str(uuid4()).encode(),
            },
        ],
    }
    with pytest.raises(IntegrityError, match="archive occurrence"):
        exporter._verify(tmp_path, {})


def test_v3_export_assigns_missing_admission_orders_using_persisted_session_order():
    from swe.app.conversation_storage.migration.exporter import (
        ConversationExporter,
    )

    exporter = object.__new__(ConversationExporter)
    exporter.snapshot = {
        "turns": [
            {
                "id": 90,
                "session_id": 1,
                "question_msg_key": b"later",
                "chat_id": 1,
            },
            {
                "id": 80,
                "session_id": 1,
                "question_msg_key": b"earlier",
                "chat_id": 2,
            },
            {
                "id": 70,
                "session_id": 2,
                "question_msg_key": b"foreign",
                "chat_id": 3,
            },
        ],
    }
    state = {
        "session_generation": "unchanged",
        "next_admission_order": 4,
        "cold_turn_count": 1,
        "turn_states": {
            "old": {"admission_order": 3},
            "later": {},
            "earlier": {},
        },
    }
    exporter._complete_v3_admission_order(state, 1)
    assert state["turn_states"]["old"]["admission_order"] == 3
    assert state["turn_states"]["earlier"]["admission_order"] == 4
    assert state["turn_states"]["later"]["admission_order"] == 5
    assert state["next_admission_order"] == 6
    assert (
        state["session_generation"] == "unchanged"
        and state["cold_turn_count"] == 1
    )


def test_export_keeps_only_deleted_marker_for_legally_reclaimed_archive(
    tmp_path,
):
    import json
    from datetime import datetime
    from uuid import uuid4
    from swe.app.conversation_storage.bundle import (
        BundleBlock,
        BundleStore,
        new_operation_key,
    )
    from swe.app.conversation_storage.migration.exporter import (
        ConversationExporter,
    )
    from swe.app.conversation_storage.migration.legacy import read_archive

    root = tmp_path / "nas"
    root.mkdir()
    bundles = BundleStore(root)
    chat_id = str(uuid4())
    operation = new_operation_key()
    metadata = {
        "version": 2,
        "scope": ["schema-test-service", "tenant", "source", "agent"],
        "relative_path": "dialog/" + chat_id + "/manifest.json",
        "bytes": 8,
    }
    index = bundles.publish(
        operation,
        [
            BundleBlock(
                "legacy_artifact_metadata",
                "legacy_artifact_metadata",
                json.dumps(metadata).encode(),
            ),
            BundleBlock("legacy_artifact", "legacy_artifact", b"obsolete"),
        ],
    )
    exporter = object.__new__(ConversationExporter)
    chat = {
        "id": 1,
        "external_chat_key": chat_id.encode(),
        "deleted_at": datetime(2026, 10, 30),
    }
    exporter.snapshot = {
        "chats": [chat],
        "messages": [],
        "publications": [
            {
                "id": 1,
                "chat_id": None,
                "state": "committed",
                "gc_state": "live",
                "relative_path": index.relative_path.encode(),
                "byte_length": index.byte_length,
            },
            {
                "id": 2,
                "chat_id": 1,
                "state": "committed",
                "gc_state": "reclaimed",
                "archive_offset": 100,
            },
        ],
    }
    exporter.target = OldFormatTarget("old@sha256:" + "a" * 64, 2, 1)
    exporter.document = {
        "scope": [b"schema-test-service", b"tenant", b"source", b"agent"],
    }
    exporter.publications = {
        row["id"]: row for row in exporter.snapshot["publications"]
    }
    exporter.bundles, exporter.artifacts = bundles, {}
    destination = tmp_path / "export"
    destination.mkdir()
    exporter._source_artifacts(destination)
    exporter._archive(destination, chat)
    assert not (destination / "dialog" / chat_id).exists()
    assert read_archive(destination / "dialog", chat_id)["deleted"]
    exporter._verify_archive(destination, chat)


def test_reclaimed_deleted_chat_metadata_alone_does_not_discard_archive_history():
    from swe.app.conversation_storage.migration.exporter import (
        ConversationExporter,
    )

    exporter = object.__new__(ConversationExporter)
    exporter.snapshot = {
        "publications": [
            {
                "id": 1,
                "chat_id": 9,
                "gc_state": "reclaimed",
                "archive_offset": None,
            },
        ],
        "messages": [],
    }
    exporter.publications = {1: exporter.snapshot["publications"][0]}
    assert not exporter._deleted_archive_reclaimed(
        {"id": 9, "deleted_at": "deleted", "content_meta_publication_id": 1},
    )


@pytest.mark.parametrize("history_kind", ["checkpoint", "event"])
def test_deleted_archive_discard_includes_reclaimed_old_history_with_live_checkpoint(
    history_kind,
):
    from swe.app.conversation_storage.migration.exporter import (
        ConversationExporter,
    )

    exporter = object.__new__(ConversationExporter)
    publications = [
        {
            "id": 1,
            "chat_id": 9,
            "gc_state": "reclaimed",
            history_kind + "_offset": 100,
        },
        {"id": 2, "chat_id": 9, "gc_state": "live", "checkpoint_offset": 200},
    ]
    exporter.snapshot = {"publications": publications, "messages": []}
    exporter.publications = {row["id"]: row for row in publications}
    assert exporter._deleted_archive_reclaimed(
        {
            "id": 9,
            "deleted_at": "deleted",
            "active_checkpoint_publication_id": 2,
        },
    )
