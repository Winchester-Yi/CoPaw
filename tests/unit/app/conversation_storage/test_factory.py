# -*- coding: utf-8 -*-
"""Existing workspace injection never initializes another filesystem tree."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.identity import WorkspaceScope


def test_factory_uses_existing_workspace_mount_and_exact_scope(tmp_path):
    from swe.app.conversation_storage.factory import (
        WorkspaceConversationStorage,
    )

    scope = WorkspaceScope(
        "Tenant",
        "Source",
        "Agent",
        service_unit_id="schema-test-service",
    )
    database = object()
    storage = WorkspaceConversationStorage(
        database,
        scope,
        tmp_path,
        cursor_secret=b"stable-secret",
    )
    assert storage.scope == scope
    assert storage.db is database
    assert storage.bundles.root == tmp_path
    assert not list(tmp_path.iterdir())
    console = storage.session_for("console")
    assert console is storage.session_for("console")
    assert console is not storage.session_for("cron")
    assert console.identity("session", "").sql_values == (
        b"schema-test-service",
        b"Tenant",
        b"Source",
        b"Agent",
        b"",
        b"console",
        b"session",
    )
    assert storage.history.scope == storage.shares.scope == scope
    assert storage.archive.scope == scope


def test_factory_refuses_missing_or_symlink_workspace_without_creating_it(
    tmp_path,
):
    from swe.app.conversation_storage.factory import (
        WorkspaceConversationStorage,
    )

    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="existing"):
        WorkspaceConversationStorage(
            object(),
            WorkspaceScope(
                "t",
                "s",
                "a",
                service_unit_id="schema-test-service",
            ),
            missing,
            cursor_secret=b"secret",
        )
    assert not missing.exists()
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        WorkspaceConversationStorage(
            object(),
            WorkspaceScope(
                "t",
                "s",
                "a",
                service_unit_id="schema-test-service",
            ),
            link,
            cursor_secret=b"secret",
        )


async def test_readiness_is_read_only_and_missing_table_is_an_error(tmp_path):
    from swe.app.conversation_storage.factory import (
        WorkspaceConversationStorage,
    )

    db = SimpleNamespace(fetch_all=AsyncMock(return_value=[]))
    storage = WorkspaceConversationStorage(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        tmp_path,
        cursor_secret=b"secret",
    )
    with pytest.raises(RuntimeError, match="schema"):
        await storage.verify_schema()
    assert all(
        call.args[0].lstrip().startswith("SELECT")
        for call in db.fetch_all.await_args_list
    )
    assert not list(tmp_path.iterdir())


def installed_schema_rows():
    from swe.app.conversation_storage.schema_contract import (
        COLUMNS,
        INDEXES,
        DEFAULTS,
        EXTRAS,
        CHARSETS,
    )

    columns = [
        {
            "TABLE_NAME": table,
            "COLUMN_NAME": name,
            "COLUMN_TYPE": kind,
            "IS_NULLABLE": nullable,
            "COLUMN_DEFAULT": DEFAULTS[table].get(name),
            "EXTRA": EXTRAS[table].get(name, ""),
            "CHARACTER_SET_NAME": CHARSETS[table][name][0],
            "COLLATION_NAME": (
                "utf8mb4_0900_ai_ci"
                if CHARSETS[table][name][0] == "utf8mb4"
                else CHARSETS[table][name][1]
            ),
        }
        for table, fields in COLUMNS.items()
        for name, (kind, nullable) in fields.items()
    ]
    indexes = [
        {
            "TABLE_NAME": table,
            "INDEX_NAME": name,
            "NON_UNIQUE": unique,
            "SEQ_IN_INDEX": position,
            "COLUMN_NAME": column,
            "SUB_PART": None,
        }
        for table, fields in INDEXES.items()
        for name, (unique, names) in fields.items()
        for position, column in enumerate(names, 1)
    ]
    tables = [
        {"TABLE_NAME": table, "ENGINE": "InnoDB", "TABLE_TYPE": "BASE TABLE"}
        for table in (*COLUMNS, "swe_chat_shares")
    ]
    return columns, indexes, tables


@pytest.mark.parametrize(
    "corruption",
    [
        "type",
        "nullable",
        "column",
        "index",
        "prefix",
        "default",
        "auto_increment",
    ],
)
async def test_readiness_refuses_same_size_but_incompatible_schema(
    tmp_path,
    corruption,
):
    from swe.app.conversation_storage.factory import (
        WorkspaceConversationStorage,
    )

    columns, indexes, tables = installed_schema_rows()
    if corruption == "type":
        columns[0]["COLUMN_TYPE"] = "varchar(20)"
    elif corruption == "nullable":
        columns[0]["IS_NULLABLE"] = "YES"
    elif corruption == "column":
        columns[0]["COLUMN_NAME"] = "unexpected"
    elif corruption == "prefix":
        indexes[0]["SUB_PART"] = 2
    elif corruption == "default":
        columns[0]["COLUMN_DEFAULT"] = "123"
    elif corruption == "auto_increment":
        columns[0]["EXTRA"] = ""
    else:
        indexes[0]["NON_UNIQUE"] = 1
    db = SimpleNamespace(
        fetch_all=AsyncMock(side_effect=[columns, indexes, tables]),
    )
    storage = WorkspaceConversationStorage(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        tmp_path,
        cursor_secret=b"secret",
    )
    storage.shares.store.verify_locator_schema = AsyncMock()
    with pytest.raises(RuntimeError, match="schema"):
        await storage.verify_schema()
    storage.shares.store.verify_locator_schema.assert_not_awaited()


async def test_readiness_accepts_exact_columns_and_indexes_without_ddl(
    tmp_path,
):
    from swe.app.conversation_storage.factory import (
        WorkspaceConversationStorage,
    )

    db = SimpleNamespace(
        fetch_all=AsyncMock(side_effect=installed_schema_rows()),
    )
    storage = WorkspaceConversationStorage(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        tmp_path,
        cursor_secret=b"secret",
    )
    storage.shares.store.verify_locator_schema = AsyncMock()
    await storage.verify_schema()
    storage.shares.store.verify_locator_schema.assert_awaited_once()
    assert all(
        call.args[0].lstrip().startswith("SELECT")
        for call in db.fetch_all.await_args_list
    )


async def test_readiness_refuses_ascii_text_that_cannot_store_chinese(
    tmp_path,
):
    from swe.app.conversation_storage.factory import (
        WorkspaceConversationStorage,
    )

    columns, indexes, tables = installed_schema_rows()
    title = next(
        row
        for row in columns
        if row["TABLE_NAME"] == "swe_conversation_chats"
        and row["COLUMN_NAME"] == "title"
    )
    title.update(CHARACTER_SET_NAME="ascii", COLLATION_NAME="ascii_bin")
    db = SimpleNamespace(
        fetch_all=AsyncMock(side_effect=[columns, indexes, tables]),
    )
    storage = WorkspaceConversationStorage(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        tmp_path,
        cursor_secret=b"secret",
    )
    storage.shares.store.verify_locator_schema = AsyncMock()
    with pytest.raises(RuntimeError, match="schema.*charset"):
        await storage.verify_schema()


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("swe_conversation_turns", "ENGINE", "MyISAM"),
        ("swe_chat_shares", "ENGINE", "MyISAM"),
        ("swe_conversation_sessions", "TABLE_TYPE", "VIEW"),
    ],
)
async def test_readiness_requires_transactional_base_tables(
    tmp_path,
    table,
    field,
    value,
):
    from swe.app.conversation_storage.factory import (
        WorkspaceConversationStorage,
    )

    columns, indexes, tables = installed_schema_rows()
    next(row for row in tables if row["TABLE_NAME"] == table)[field] = value
    db = SimpleNamespace(
        fetch_all=AsyncMock(side_effect=[columns, indexes, tables]),
    )
    storage = WorkspaceConversationStorage(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        tmp_path,
        cursor_secret=b"secret",
    )
    storage.shares.store.verify_locator_schema = AsyncMock()
    with pytest.raises(RuntimeError, match="schema.*InnoDB"):
        await storage.verify_schema()


async def test_bind_existing_workspace_reuses_services_without_legacy_files(
    tmp_path,
):
    from swe.app.conversation_storage.factory import (
        WorkspaceConversationStorage,
    )

    storage = WorkspaceConversationStorage(
        object(),
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        tmp_path,
        cursor_secret=b"secret",
    )
    storage.verify_schema = AsyncMock()
    runner = SimpleNamespace(
        set_chat_manager=lambda manager: setattr(
            runner,
            "chat_manager",
            manager,
        ),
    )
    memory = SimpleNamespace(
        bind_conversation_archive=lambda archive: setattr(
            memory,
            "archive",
            archive,
        ),
    )
    services = {"runner": runner, "memory_manager": memory}
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        _service_manager=SimpleNamespace(services=services),
    )
    await storage.bind_workspace(workspace)
    assert workspace.conversation_storage is storage
    assert runner.session is storage.session_for("console")
    assert services["chat_manager"]._repo is storage.chats
    assert memory.archive is storage.archive
    assert not (tmp_path / "chats.json").exists()
    assert not (tmp_path / "sessions").exists()


async def test_runner_initialization_keeps_indexed_session_without_file_store(
    tmp_path,
):
    from swe.app.runner.runner import AgentRunner

    runner = AgentRunner(workspace_dir=tmp_path)
    adapter = object()
    storage = SimpleNamespace(session_for=lambda channel: adapter)
    runner.set_workspace(SimpleNamespace(conversation_storage=storage))
    await runner.init_handler()
    assert runner.session is adapter
    assert not (tmp_path / "sessions").exists()


async def test_sql_chat_service_reuses_existing_storage_without_legacy_repo(
    tmp_path,
):
    from swe.app.workspace.service_factories import create_chat_service

    storage = SimpleNamespace(
        bind_workspace=AsyncMock(return_value="sql-manager"),
    )
    workspace = SimpleNamespace(
        conversation_storage=storage,
        workspace_dir=tmp_path,
    )
    assert await create_chat_service(workspace, None) == "sql-manager"
    storage.bind_workspace.assert_awaited_once_with(workspace)
    assert not (tmp_path / "chats.json").exists()


def test_runtime_maps_original_workspace_to_shared_existing_nas(tmp_path):
    from swe.app.conversation_storage.runtime import ConversationRuntime

    workspace_root, nas = tmp_path / "workspace", tmp_path / "nas"
    workspace_root.mkdir()
    nas.mkdir()
    runtime = ConversationRuntime(
        object(),
        {
            "version": 2,
            "service_unit_id": "schema-test-service",
            "nas_root": str(nas),
            "workspaces": [
                {
                    "root": str(workspace_root),
                    "scope": ["schema-test-service", "t", "s", "a"],
                },
            ],
        },
        b"secret",
        service_unit_id="schema-test-service",
    )
    workspace = SimpleNamespace(
        workspace_dir=workspace_root,
        agent_id="a",
        conversation_scope=WorkspaceScope(
            "t",
            "s",
            "a",
            service_unit_id="schema-test-service",
        ),
    )
    storage = runtime.for_workspace(workspace)
    assert runtime.for_workspace(workspace) is storage
    assert storage.workspace_root == workspace_root
    assert storage.bundles.root == nas
    assert not list(nas.iterdir())
    workspace.agent_id = "other"
    with pytest.raises(ValueError, match="scope"):
        runtime.for_workspace(workspace)


def test_runtime_rejects_other_tenant_config_pointing_at_installed_root(
    tmp_path,
):
    from swe.app.conversation_storage.runtime import ConversationRuntime

    runtime = ConversationRuntime(
        object(),
        {
            "version": 2,
            "service_unit_id": "schema-test-service",
            "nas_root": str(tmp_path),
            "workspaces": [
                {
                    "root": str(tmp_path),
                    "scope": ["schema-test-service", "t", "s", "a"],
                },
            ],
        },
        b"secret",
        service_unit_id="schema-test-service",
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="a",
        conversation_scope=WorkspaceScope(
            "other",
            "s",
            "a",
            service_unit_id="schema-test-service",
        ),
    )
    with pytest.raises(ValueError, match="scope"):
        runtime.for_workspace(workspace)


def test_runtime_binds_normally_provisioned_new_scope_without_new_tree(
    tmp_path,
):
    from swe.app.conversation_storage.runtime import ConversationRuntime
    from swe.config.context import encode_scope_id

    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    runtime = ConversationRuntime(
        object(),
        {
            "version": 2,
            "service_unit_id": "schema-test-service",
            "nas_root": str(tmp_path),
            "workspaces": [
                {
                    "root": str(old),
                    "scope": ["schema-test-service", "t", "s", "a"],
                },
            ],
        },
        b"secret",
        service_unit_id="schema-test-service",
    )
    workspace = SimpleNamespace(
        workspace_dir=new,
        agent_id="a",
        tenant_id=encode_scope_id("new-tenant", "source"),
        source_id="source",
    )
    storage = runtime.for_workspace(workspace)
    assert storage.scope == WorkspaceScope(
        "new-tenant",
        "source",
        "a",
        service_unit_id="schema-test-service",
    )
    assert storage.workspace_root == new
    assert not list(new.iterdir())


def test_runtime_cannot_silently_adopt_unmapped_legacy_conversation(tmp_path):
    from swe.app.conversation_storage.runtime import ConversationRuntime

    old, missing = tmp_path / "old", tmp_path / "not-migrated"
    old.mkdir()
    missing.mkdir()
    (missing / "chats.json").write_text('{"version":1,"chats":[{"id":"old"}]}')
    runtime = ConversationRuntime(
        object(),
        {
            "version": 2,
            "service_unit_id": "schema-test-service",
            "nas_root": str(tmp_path),
            "workspaces": [
                {
                    "root": str(old),
                    "scope": ["schema-test-service", "t", "s", "a"],
                },
            ],
        },
        b"secret",
        service_unit_id="schema-test-service",
    )
    with pytest.raises(ValueError, match="migration"):
        runtime.for_workspace(
            SimpleNamespace(
                workspace_dir=missing,
                agent_id="b",
                conversation_scope=WorkspaceScope(
                    "t",
                    "s",
                    "b",
                    service_unit_id="schema-test-service",
                ),
            ),
        )


def test_runtime_default_single_user_uses_installed_default_identity(tmp_path):
    from swe.app.conversation_storage.runtime import ConversationRuntime

    runtime = ConversationRuntime(
        object(),
        {
            "version": 2,
            "service_unit_id": "schema-test-service",
            "nas_root": str(tmp_path),
            "workspaces": [
                {
                    "root": str(tmp_path),
                    "scope": [
                        "schema-test-service",
                        "default",
                        "console",
                        "a",
                    ],
                },
            ],
        },
        b"secret",
        service_unit_id="schema-test-service",
    )
    storage = runtime.for_workspace(
        SimpleNamespace(
            workspace_dir=tmp_path,
            agent_id="a",
            tenant_id=None,
            source_id=None,
        ),
    )
    assert storage.scope == WorkspaceScope(
        "default",
        "console",
        "a",
        service_unit_id="schema-test-service",
    )


async def test_mysql_runtime_refuses_unready_database_and_missing_mapping(
    monkeypatch,
):
    from swe.app.conversation_storage.runtime import (
        configure_runtime,
        workspace_storage,
    )

    monkeypatch.setenv("SWE_CONVERSATION_STORAGE_AUTHORITY", "mysql")
    with pytest.raises(RuntimeError, match="connected"):
        await configure_runtime(None)
    monkeypatch.delenv("SWE_CONVERSATION_SCOPE_MANIFEST", raising=False)
    with pytest.raises(RuntimeError, match="installed scopes"):
        await configure_runtime(SimpleNamespace(is_connected=True))
    with pytest.raises(RuntimeError, match="not ready"):
        workspace_storage(SimpleNamespace())


def test_mysql_workspace_never_initializes_missing_root(monkeypatch, tmp_path):
    from swe.app.workspace.workspace import Workspace

    monkeypatch.setenv("SWE_CONVERSATION_STORAGE_AUTHORITY", "mysql")
    root = tmp_path / "missing"
    with pytest.raises(ValueError, match="existing workspace"):
        Workspace("a", str(root))
    assert not root.exists()


def test_runtime_reset_discards_ready_process_mapping(monkeypatch):
    from swe.app.conversation_storage import runtime

    monkeypatch.setenv(runtime.AUTHORITY_ENV, "mysql")
    monkeypatch.setattr(runtime, "_runtime", SimpleNamespace(shares=object()))
    runtime.reset_runtime()
    with pytest.raises(RuntimeError, match="not ready"):
        runtime.runtime_shares()


def test_share_module_uses_indexed_public_reader(monkeypatch):
    from swe.app.chat_sharing import router

    shares = object()
    monkeypatch.setattr(
        "swe.app.conversation_storage.runtime.runtime_shares",
        lambda: shares,
    )
    monkeypatch.setattr(router, "_service", None)
    router.init_chat_sharing_module(SimpleNamespace(is_connected=True))
    assert router.get_service().conversation_shares is shares


async def test_share_creation_uses_trusted_workspace_storage(
    monkeypatch,
    tmp_path,
):
    from swe.app.chat_sharing import router
    from swe.app.chat_sharing.models import ChatShareRecord

    shares = SimpleNamespace(
        store=object(),
        selection_snapshot=AsyncMock(return_value={"state_revision": 1}),
        prove_selection=AsyncMock(
            return_value={"snapshot": {"state_revision": 1}},
        ),
        create_snapshot=AsyncMock(
            return_value=ChatShareRecord(
                token="token",
                chat_id="chat",
                creator_id="user",
                tenant_id="t",
                snapshot_key="key",
                created_at="2026-10-31T00:00:00Z",
            ),
        ),
    )
    storage = SimpleNamespace(
        shares=shares,
        workspace_root=tmp_path,
        scope=WorkspaceScope(
            "t",
            "s",
            "a",
            service_unit_id="schema-test-service",
        ),
    )
    chat = SimpleNamespace(id="chat", name="Demo")
    workspace = SimpleNamespace(
        conversation_storage=storage,
        chat_manager=SimpleNamespace(get_chat=AsyncMock(return_value=chat)),
    )
    monkeypatch.setattr(router, "_service", SimpleNamespace())
    monkeypatch.setattr(router, "_authorize_chat", lambda *args: None)
    messages = [
        {
            "id": "q",
            "name": "user",
            "role": "user",
            "content": [{"type": "text", "text": "question"}],
            "metadata": {"original_id": "q"},
        },
        {
            "id": "a",
            "name": "assistant",
            "role": "assistant",
            "content": [{"type": "text", "text": "answer"}],
        },
    ]
    monkeypatch.setattr(
        router,
        "_share_context",
        AsyncMock(return_value=(None, messages, {"q": "completed"})),
    )
    result = await router.create_chat_share(
        "chat",
        router.ChatShareCreateRequest(turn_ids=["q"]),
        SimpleNamespace(
            state=SimpleNamespace(
                user_id="user",
                tenant_id="t",
                effective_tenant_id="dA.cw",
            ),
        ),
        workspace,
    )
    assert result.token == "token"
    assert shares.create_snapshot.await_args.kwargs[
        "selected_question_keys"
    ] == ("q",)
    assert shares.create_snapshot.await_args.kwargs["tenant_id"] == "t"
    assert shares.create_snapshot.await_args.kwargs["selection_proof"] == {
        "snapshot": {"state_revision": 1},
    }
    shares.selection_snapshot.assert_awaited_once_with("chat")
    shares.prove_selection.assert_awaited_once()
    assert not list(tmp_path.iterdir())
