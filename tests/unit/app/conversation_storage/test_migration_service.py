# -*- coding: utf-8 -*-
import pytest


async def test_offline_reference_preflight_works_before_plan_body_exists(
    tmp_path,
):
    from types import SimpleNamespace
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.commit_plan import CommitPlan
    from swe.app.conversation_storage.migration.install_records import (
        OfflineImportStore,
    )
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
        request,
    )

    store = OfflineImportStore(
        PublicationDatabase(),
        BundleStore(tmp_path),
        SimpleNamespace(),
        service_unit_id="schema-test-service",
    )
    await store._validate_reference_ownership(request(), CommitPlan())


async def test_offline_payload_rejects_foreign_publication_before_body():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from swe.app.conversation_storage.migration import verify

    database = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"service_unit_key": b"B"}),
    )
    bundles = SimpleNamespace(
        read_extent=Mock(side_effect=AssertionError("foreign body read")),
    )
    with pytest.raises(ValueError, match="service"):
        await verify._payload(
            database,
            bundles,
            {"publication_id": 1, "service_unit_key": b"A"},
            "byte_offset",
            "byte_length",
            service_unit_id="A",
        )
    bundles.read_extent.assert_not_called()


async def test_offline_artifact_rejects_foreign_publication_before_body():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from swe.app.conversation_storage.migration import verify

    database = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"service_unit_key": b"B"}),
    )
    bundles = SimpleNamespace(
        inspect=Mock(side_effect=AssertionError("foreign artifact read")),
    )
    inventory = {"workspaces": [{"scope": ["A", "t", "s", "a"]}]}
    manifest = SimpleNamespace(
        data={
            "assignments": {
                "publication": {
                    "service:A/source-artifact:0/file": {"target_id": 1},
                },
            },
        },
    )
    with pytest.raises(ValueError, match="publication identity"):
        await verify._verify_artifact(
            database,
            bundles,
            manifest,
            inventory,
            {"workspace": 0, "path": "file"},
        )
    bundles.inspect.assert_not_called()


async def test_offline_epoch_rejects_foreign_service_before_nas():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.conversation_storage.migration import verify

    scope = WorkspaceScope("t", "s", "a", service_unit_id="A")
    database = SimpleNamespace(
        fetch_one=AsyncMock(
            return_value={**scope.fields, "service_unit_key": b"B"},
        ),
    )
    bundles = SimpleNamespace(
        inspect=Mock(side_effect=AssertionError("foreign epoch read")),
    )
    manifest = SimpleNamespace(
        data={
            "assignments": {"publication": {"chat/epoch:1": {"target_id": 1}}},
        },
    )
    chat = {"position": "chat", "archive": {"epochs": [{"epoch": 1}]}}
    with pytest.raises(ValueError, match="checkpoint"):
        await verify._verify_epochs(database, bundles, manifest, chat, scope)
    bundles.inspect.assert_not_called()


async def test_offline_archive_rows_receive_explicit_service(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.conversation_storage.migration import verify

    row = {"publication_id": 1}
    message = {"id": "archived"}
    payload = AsyncMock(return_value=message)
    monkeypatch.setattr(verify, "_payload", payload)
    monkeypatch.setattr(
        verify,
        "archive_batches",
        lambda _: [{"messages": [message]}],
    )
    database = SimpleNamespace(fetch_all=AsyncMock(return_value=[row]))
    manifest = SimpleNamespace(
        data={"assignments": {"chat": {"chat": {"target_id": 2}}}},
    )
    scope = WorkspaceScope("t", "s", "a", service_unit_id="A")
    await verify._verify_archive_rows(
        database,
        None,
        manifest,
        {"position": "chat", "archive": {}},
        scope,
    )
    assert payload.await_args.kwargs == {"service_unit_id": "A"}


from swe.app.conversation_storage.migration.manifest import (
    MigrationManifest,
    SourceBoundary,
)
from swe.app.conversation_storage.migration.importer import validate_inventory


def boundary():
    return SourceBoundary(
        "snapshot",
        "2026-10-30T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("stopped",),
    )


def test_manifest_requires_explicit_service(tmp_path):
    with pytest.raises(ValueError, match="service"):
        MigrationManifest.create(
            tmp_path / "manifest.json",
            boundary(),
            [
                {
                    "tenant_id": "t",
                    "source_id": "s",
                    "agent_id": "a",
                    "root": str(tmp_path),
                },
            ],
        )


def test_inventory_v1_is_rejected():
    with pytest.raises(ValueError):
        validate_inventory(
            {
                "version": 1,
                "ready": True,
                "blockers": [],
                "workspaces": [],
                "shares": [],
            },
        )


def test_old_scope_inventory_is_rejected():
    with pytest.raises(ValueError):
        validate_inventory(
            {
                "version": 2,
                "ready": True,
                "blockers": [],
                "workspaces": [{"scope": ["t", "s", "a"]}],
                "shares": [],
            },
        )


def test_manifest_v1_is_rejected(tmp_path):
    from uuid import uuid4
    from dataclasses import asdict

    with pytest.raises(ValueError, match="version"):
        MigrationManifest(
            tmp_path / "old.json",
            {
                "version": 1,
                "migration_id": str(uuid4()),
                "source_boundary": asdict(boundary()),
                "workspaces": [],
                "assignments": {},
                "batches": {},
            },
        )


def test_global_allocations_and_isolated_service_nas_bindings(tmp_path):
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.migration.importer import bundle_stores

    workspaces = [
        {
            "service_unit_id": service,
            "tenant_id": "same",
            "source_id": "same",
            "agent_id": "same",
            "root": str(tmp_path / service),
        }
        for service in ("one", "two")
    ]
    manifest = MigrationManifest.create(
        tmp_path / "manifest.json",
        boundary(),
        workspaces,
    )
    first = manifest.allocate("session", "service:one/workspace:0/session:0")
    second = manifest.allocate("session", "service:two/workspace:1/session:0")
    assert first["target_id"] != second["target_id"]
    assert first == manifest.allocate(
        "session",
        "service:one/workspace:0/session:0",
    )
    one = tmp_path / "nas-one"
    two = tmp_path / "nas-two"
    one.mkdir()
    two.mkdir()
    stores = {"one": BundleStore(one), "two": BundleStore(two)}
    assert bundle_stores(stores, manifest) == stores
    for invalid in (
        stores["one"],
        {"one": stores["one"]},
        {"one": stores["one"], "two": stores["one"]},
    ):
        with pytest.raises(ValueError):
            bundle_stores(invalid, manifest)
    manifest.bind_target(
        {
            "schema": "test_conversation_multi",
            "migration_id": manifest.migration_id,
            "nas_roots": {"one": str(one), "two": str(two)},
        },
    )


def test_same_tenant_orphan_shares_remain_separate_and_global_tokens_collide(
    tmp_path,
):
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.conversation_storage.migration.inventory import (
        WorkspaceMapping,
        inventory,
    )

    mappings = []
    shares = []
    for index, service in enumerate(("one", "two")):
        root = tmp_path / service
        root.mkdir()
        (root / "chats.json").write_text('{"chats": []}')
        (root / "share.json").write_text('{"messages": []}')
        for path in root.iterdir():
            path.chmod(0o444)
        root.chmod(0o555)
        mappings.append(
            WorkspaceMapping(
                WorkspaceScope(
                    "same",
                    "same",
                    "same",
                    service_unit_id=service,
                ),
                root,
            ),
        )
        shares.append(
            {
                "service_unit_id": service,
                "tenant_id": "same",
                "token": service,
                "chat_id": "deleted",
                "creator_id": "same",
                "snapshot_key": "share.json",
                "created_at": "2026-10-30T00:00:00Z",
                "access_count": 1,
                "last_access_at": None,
                "snapshot_path": "share.json",
                "snapshot_workspace_index": index,
            },
        )
    report = inventory(mappings, boundary(), shares=shares)
    assert report["ready"], report["blockers"]
    assert {item["service_unit_id"] for item in report["shares"]} == {
        "one",
        "two",
    }
    assert all(
        item["scope_kind"] == "legacy_share" for item in report["shares"]
    )
    shares[1]["token"] = shares[0]["token"]
    rejected = inventory(mappings, boundary(), shares=shares)
    assert not rejected["ready"]
    assert (
        "duplicate permanent share token" in rejected["blockers"][0]["detail"]
    )


async def test_runtime_chat_verification_matches_seven_value_session_identity(
    monkeypatch,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from swe.app.conversation_storage.migration import verify
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.runner import api

    history = SimpleNamespace(
        current=AsyncMock(return_value=SimpleNamespace(messages=[])),
    )
    monkeypatch.setattr(
        verify,
        "HistoryStore",
        lambda *args, **kwargs: history,
    )
    monkeypatch.setattr(verify, "read_only_adapter", lambda adapter: adapter)
    monkeypatch.setattr(
        api,
        "_build_chat_history",
        AsyncMock(return_value=SimpleNamespace(messages=[])),
    )
    for name in (
        "_verify_answer_turns",
        "_verify_archived_questions",
        "_verify_archive_page",
        "_verify_checkpoint",
    ):
        monkeypatch.setattr(verify, name, AsyncMock())
    scope = WorkspaceScope(
        "tenant",
        "source",
        "agent",
        service_unit_id="service",
    )
    chat = {
        "spec": {
            "id": "chat",
            "user_id": "user",
            "channel": "console",
            "session_id": "session",
        },
    }
    workspace = {
        "sessions": [
            {
                "identity": [
                    "service",
                    "tenant",
                    "source",
                    "agent",
                    "user",
                    "console",
                    "session",
                ],
                "legacy": {"state": {}},
            },
        ],
    }
    await verify._verify_runtime_chat(None, None, scope, chat, workspace)
    api._build_chat_history.assert_awaited_once()


async def test_imported_turn_and_message_rows_carry_immutable_service():
    from unittest.mock import AsyncMock
    from swe.app.conversation_storage.migration.importer import (
        ConversationImporter,
        _message_row,
        _entry,
    )
    from swe.app.conversation_storage.migration.install_records import (
        COLUMNS,
        IMMUTABLE,
    )
    from swe.app.conversation_storage.identity import WorkspaceScope

    scope = WorkspaceScope(
        "tenant",
        "source",
        "agent",
        service_unit_id="service",
    )
    importer = ConversationImporter.__new__(ConversationImporter)
    importer.session_ids = {"session": 1}
    importer.turn_ids = {(1, "question"): 2}
    importer._publish = AsyncMock()
    session = {
        "position": "session",
        "legacy": {
            "state": {
                "turn_states": {
                    "question": {"chat_id": "chat", "status": "completed"},
                },
            },
        },
    }
    await importer._turn_identities(session, scope, {"chat": 3})
    turn = importer._publish.call_args.args[2][0]["values"]
    assert turn["service_unit_key"] == "service"
    item = _entry({"role": "user", "content": "body"}, "memory", 0, 0)
    message = _message_row(
        4,
        1,
        item,
        1,
        "body",
        2,
        3,
        service_unit_id="service",
    )
    assert message["service_unit_key"] == "service"
    for table in ("turns", "messages"):
        assert "service_unit_key" in COLUMNS[table]
        assert "service_unit_key" in IMMUTABLE[table]


@pytest.mark.parametrize("table", ["turns", "messages"])
def test_import_record_rejects_foreign_service_even_with_same_parent_ids(
    table,
):
    from swe.app.conversation_storage.migration.install_records import (
        OfflineImportStore,
        _LOCKED,
    )
    from swe.app.conversation_storage.errors import IntegrityError

    scope = {
        "service_unit_key": b"service",
        "tenant_key": b"tenant",
        "source_key": b"source",
        "agent_key": b"agent",
    }
    publication = {"id": 9, **scope}
    importer = OfflineImportStore.__new__(OfflineImportStore)
    token = _LOCKED.set(
        {"sessions": {1: {"id": 1, **scope}}, "chats": {}, "turns": {}},
    )
    try:
        with pytest.raises(IntegrityError, match="service|scope"):
            importer._validate_relation(
                table,
                {"id": 2, "session_id": 1, "service_unit_key": b"foreign"},
                publication,
            )
    finally:
        _LOCKED.reset(token)


def test_import_message_rejects_foreign_service_turn_reference():
    from swe.app.conversation_storage.migration.install_records import (
        OfflineImportStore,
        _LOCKED,
    )
    from swe.app.conversation_storage.errors import IntegrityError

    scope = {
        "service_unit_key": b"service",
        "tenant_key": b"tenant",
        "source_key": b"source",
        "agent_key": b"agent",
    }
    publication = {"id": 9, **scope}
    importer = OfflineImportStore.__new__(OfflineImportStore)
    token = _LOCKED.set(
        {
            "sessions": {1: {"id": 1, **scope}},
            "chats": {},
            "turns": {
                2: {
                    "id": 2,
                    "session_id": 1,
                    "chat_id": 3,
                    "service_unit_key": b"foreign",
                },
            },
        },
    )
    try:
        with pytest.raises(IntegrityError, match="Turn relation"):
            importer._validate_relation(
                "messages",
                {
                    "id": 4,
                    "session_id": 1,
                    "turn_id": 2,
                    "service_unit_key": b"service",
                },
                publication,
            )
    finally:
        _LOCKED.reset(token)
