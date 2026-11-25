# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from swe.app.conversation_storage import runtime
from swe.app.conversation_storage.errors import ConflictError
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.metadata_repository import (
    MySQLMetadataRepository,
    session_filename,
)
from swe.app.conversation_storage.migration import metadata
from swe.config.context import encode_scope_id


@pytest.mark.asyncio
async def test_new_tenant_can_open_empty_catalog_and_is_in_cutover_export(
    tmp_path,
    monkeypatch,
):
    scope = WorkspaceScope("new", "source", "default", service_unit_id="svc")
    root = tmp_path / "workspace"
    root.mkdir()
    binding = {**scope.fields, "workspace_root": str(root).encode()}

    @asynccontextmanager
    async def transaction():
        yield

    db = SimpleNamespace(
        is_connected=True,
        fetch_one=AsyncMock(
            side_effect=[None, binding, binding, None, {"1": 1}],
        ),
        fetch_all=AsyncMock(side_effect=[[], [], [], [binding], []]),
        execute=AsyncMock(),
        transaction=transaction,
    )
    monkeypatch.setattr(runtime, "_metadata_database", None)
    monkeypatch.setenv(runtime.AUTHORITY_ENV, runtime.METADATA_AUTHORITY)
    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", scope.service_unit_id)
    await runtime.configure_runtime(db)
    workspace = SimpleNamespace(
        workspace_dir=root,
        tenant_id=encode_scope_id(scope.tenant_id, scope.source_id),
        agent_id=scope.agent_id,
    )
    repo = await runtime.workspace_metadata_repository(workspace)
    await repo._assert_writes_allowed()
    destination = tmp_path / "export"
    [exported] = await metadata.export(db, None, None, destination)

    assert exported["scope"] == scope.values
    assert json.loads((destination / exported["registry"]).read_text()) == {
        "version": 1,
        "chats": [],
    }
    assert all(
        metadata.SOURCES not in call.args[0]
        for call in db.fetch_one.await_args_list[:3]
    )
    rejected = tmp_path / "partial-export"
    with pytest.raises(ConflictError, match="finish migration"):
        await metadata.export(db, None, None, rejected)
    assert not rejected.exists()


@pytest.mark.asyncio
async def test_session_candidate_lookup_treats_oversized_id_as_missing():
    db = SimpleNamespace(fetch_all=AsyncMock())
    repo = MySQLMetadataRepository(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="svc"),
    )
    assert (
        await repo.filter_chats_by_session("会" * 342, "user", "console") == []
    )
    db.fetch_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_session_path_uses_one_lookup_without_migration_state():
    filename = session_filename("session", "user").encode()
    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"nas_filename": filename}),
    )
    repo = MySQLMetadataRepository(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="svc"),
    )
    repo.claim_session = AsyncMock()
    await repo.ensure_session_path("session", "user", "console")
    db.fetch_one.assert_awaited_once()
    statement, _ = db.fetch_one.await_args.args
    assert "swe_conversation_metadata_sources" not in statement
    assert "FOR UPDATE" not in statement
    repo.claim_session.assert_not_awaited()

    db.fetch_one.return_value = {"nas_filename": b"other.json"}
    with pytest.raises(ConflictError, match="filename"):
        await repo.ensure_session_path("session", "user", "console")
    repo.claim_session.assert_not_awaited()

    db.fetch_one.return_value = None
    await repo.ensure_session_path("session", "user", "console")
    repo.claim_session.assert_awaited_once_with("session", "user", "console")


@pytest.mark.asyncio
async def test_delete_checks_locked_chat_and_clears_draft():
    @asynccontextmanager
    async def transaction():
        yield

    db = SimpleNamespace(
        transaction=transaction,
        execute=AsyncMock(return_value=1),
    )
    repo = MySQLMetadataRepository(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="svc"),
    )
    repo._assert_writes_allowed = AsyncMock()
    repo._locked_chat = AsyncMock(
        return_value=({}, {"id": 8, "session_id": 4}),
    )
    repo._spec = lambda row: "authorized"
    assert await repo.delete_chats(
        ["chat"],
        expected_chats={"chat": "authorized"},
    )
    assert db.execute.await_count == 2
    assert db.execute.await_args_list[1].args[1] == (4, 8)

    db.execute.reset_mock()
    with pytest.raises(ConflictError, match="changed since authorization"):
        await repo.delete_chats(["chat"], expected_chats={"chat": "stale"})
    db.execute.assert_not_awaited()
