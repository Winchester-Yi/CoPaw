# -*- coding: utf-8 -*-
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agentscope.message import Msg
from click.testing import CliRunner
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from swe.app.conversation_storage import runtime
from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.app.conversation_storage.errors import ConflictError
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.migration import metadata
from swe.app.conversation_storage.metadata_repository import (
    MySQLMetadataRepository,
)
from swe.app.approvals import external
from swe.app.channels.console.channel import ConsoleChannel
from swe.app.routers import approvals
from swe.app.runner.manager import ChatManager
from swe.app.runner.models import ChatSpec
from swe.app.runner.session import SafeJSONSession
from swe.app.workspace.service_factories import create_chat_service
from swe.cli.conversation_storage_cmd import conversation_storage_group
from swe.config.context import encode_scope_id
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)


@pytest.mark.asyncio
async def test_default_workspace_scan_imports_registry_changed_after_prepare(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    await apply_sql_script(db, SQL_ROOT / "conversation_mysql_metadata.sql")
    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "svc")
    nas = tmp_path / "nas"
    scope_id = encode_scope_id("tenant", "source")
    root = nas / scope_id / "workspaces" / "default"
    root.mkdir(parents=True)
    registry = root / "chats.json"
    registry.write_text('{"version": 1, "chats": []}')
    path = tmp_path / "scan.json"
    metadata.prepare_scan(nas, db.config.database, path)

    chat = ChatSpec(session_id="session", user_id="tenant", channel="console")
    registry.write_text(
        json.dumps({"version": 1, "chats": [chat.model_dump(mode="json")]}),
    )
    manifest = metadata.read_manifest(path, db.config.database)

    async def fail_first_batch(*_args):
        raise ValueError("first batch failed")

    with monkeypatch.context() as patch:
        patch.setattr(metadata, "_insert_chats", fail_first_batch)
        result = await metadata.migrate(db, *manifest, apply=True)
    assert result["failed"][0]["detail"] == "first batch failed"
    assert not await db.fetch_all(f"SELECT 1 FROM {metadata.WORKSPACES}")

    registry.write_text(registry.read_text() + "\n")
    result = await metadata.migrate(db, *manifest, apply=True)
    assert result["published"] == [("svc", "tenant", "source")]
    row = await db.fetch_one(
        "SELECT external_chat_key FROM swe_conversation_chats",
    )
    assert row["external_chat_key"] == chat.id.encode()


@pytest.mark.asyncio
async def test_migration_filters_users_and_imports_past_session_failures(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await apply_sql_script(db, SQL_ROOT / "conversation_mysql_metadata.sql")
    scope = WorkspaceScope(
        "tenant",
        "source",
        "default",
        service_unit_id="svc",
    )
    root = tmp_path / "workspace"
    root.mkdir()

    def chat(session_id, *, user_id="tenant", **fields):
        return (
            ChatSpec(session_id=session_id, user_id=user_id).model_dump(
                mode="json",
            )
            | fields
        )

    first, last = chat("first"), chat("last")
    malformed = chat("malformed", name=42)
    entries = [
        first,
        chat("foreign:session", user_id="foreign"),
        chat("foreign--session", user_id="foreign"),
        chat("collision:session"),
        chat("collision--session"),
        chat("malformed"),
        malformed,
        last,
    ]
    (root / "chats.json").write_text(
        json.dumps({"version": 1, "chats": entries}),
    )
    path = tmp_path / "migration.json"
    metadata.prepare(
        [{"scope": scope.values, "root": str(root)}],
        db.config.database,
        path,
    )
    manifest = metadata.read_manifest(path, db.config.database)

    preview = await metadata.migrate(None, *manifest, apply=False)
    assert [error["session_id"] for error in preview["failed"]] == [
        "collision--session",
        "malformed",
    ]
    for _ in range(2):
        report = await metadata.migrate(db, *manifest, apply=True)
        assert report["status"] == "partial_success"
        assert report["published"] == []
        assert report["failed"] == preview["failed"]
        assert await db.fetch_all(
            "SELECT external_chat_key,external_session_key "
            "FROM swe_conversation_chats ORDER BY registry_order",
        ) == [
            {
                "external_chat_key": first["id"].encode(),
                "external_session_key": b"first",
            },
            {
                "external_chat_key": last["id"].encode(),
                "external_session_key": b"last",
            },
        ]


@pytest.mark.asyncio
# pylint: disable-next=too-many-statements
async def test_metadata_cutover_keeps_nas_as_the_online_content_store(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    await apply_sql_script(db, SQL_ROOT / "conversation_mysql_metadata.sql")
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_metadata_performance.sql",
    )
    scope = WorkspaceScope(
        "tenant",
        "source",
        "agent",
        service_unit_id="service",
    )
    root = tmp_path / "workspace"
    root.mkdir()
    state_dir = root / "sessions"
    state_dir.mkdir()
    original = state_dir / "tenant_session.json"
    original.write_text('{"schema_version":2,"revision":1,"example":"before"}')
    before = original.read_bytes()
    specs = [
        ChatSpec(
            id=str(uuid4()),
            session_id="session",
            user_id="tenant",
            channel="console",
            meta={
                "task_job_id": "task",
                "scenario_preset_snapshot": {"version": 1, "enabled": False},
            },
        )
        for _ in range(2)
    ]
    registry = root / "chats.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "chats": [spec.model_dump(mode="json") for spec in specs],
            },
        ),
    )
    manifest = tmp_path / "manifest.json"
    metadata.prepare(
        [{"scope": scope.values, "root": str(root)}],
        db.config.database,
        manifest,
    )
    data, digest = metadata.read_manifest(manifest, db.config.database)
    archive = ConversationArchiveStore(root / "dialog")
    await archive.commit(specs[0].id, [Msg("user", "old archive", "user")])
    snapshot = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(metadata, "BATCH_SIZE", 1)
    monkeypatch.setattr(metadata, "VERIFY_PAGE_SIZE", 1)
    batch = metadata._import_batch

    async def interrupt(repo, registry, root_key, registry_digest):
        assert await batch(repo, registry, root_key, registry_digest)
        raise RuntimeError("interrupted after a committed batch")

    monkeypatch.setattr(metadata, "_import_batch", interrupt)
    with pytest.raises(RuntimeError, match="committed batch"):
        await metadata.migrate(db, data, digest, apply=True)
    marker = await db.fetch_one(
        "SELECT imported_count FROM swe_conversation_metadata_workspaces",
    )
    assert marker["imported_count"] == 1

    monkeypatch.setenv(runtime.AUTHORITY_ENV, runtime.METADATA_AUTHORITY)
    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", scope.service_unit_id)
    try:
        runner = SimpleNamespace(
            set_chat_manager=lambda manager: setattr(
                runner,
                "chat_manager",
                manager,
            ),
        )
        workspace = SimpleNamespace(
            workspace_dir=root,
            tenant_id=encode_scope_id(scope.tenant_id, scope.source_id),
            source_id=scope.source_id,
            agent_id=scope.agent_id,
            _service_manager=SimpleNamespace(services={"runner": runner}),
        )
        monkeypatch.setattr(metadata, "_import_batch", batch)
        for _ in range(2):
            assert (
                await metadata.migrate(
                    db,
                    data,
                    digest,
                    apply=True,
                    full_audit=True,
                )
            )["status"] == "ready"
        assert {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        } == snapshot

        await runtime.configure_runtime(db)
        await create_chat_service(workspace, None)
        manager = runner.chat_manager
        repo = manager._repo
        assert await repo.list_chats() == specs
        assert (await repo.list_summaries()).items[0].display_metadata == {
            "task_job_id": "task",
        }
        async with db.transaction():
            await db.fetch_one(
                "SELECT workspace_root FROM swe_conversation_metadata_workspaces "
                "WHERE service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s FOR UPDATE",
                scope.sql_values,
            )
            await asyncio.wait_for(
                db.create_independent_task(
                    repo.ensure_session_path("session", "tenant", "console"),
                ),
                timeout=3,
            )
        with pytest.raises(ConflictError, match="filename"):
            await repo.ensure_session_path("session", "tenant", "zhaohu")
        session = SafeJSONSession(str(state_dir))
        async with session.execution("session", "tenant") as state:
            assert state.state["example"] == "before"
            await state.commit_state({**state.state, "example": "after"})
        assert json.loads(original.read_text())["example"] == "after"

        updated = specs[0].model_copy(update={"name": "renamed"})
        assert (
            await repo.update_chat_if_exists(updated, expected_chat=specs[0])
            == updated
        )
        assert (await manager._archive_store.read_page(specs[0].id)).messages[
            0
        ].get_text_content() == "old archive"
        await manager._archive_store.commit(
            specs[1].id,
            [Msg("user", "delete me", "user")],
        )
        assert await manager.delete_chats(
            [specs[1].id],
            expected_chats={specs[1].id: specs[1]},
        )
        assert not await repo.pending_resource_cleanup()
        assert not (
            await manager._archive_store.read_page(specs[1].id)
        ).messages
        assert original.is_file()
        assert await manager._archive_store.read_page(specs[0].id)
        output = tmp_path / "export"
        await metadata.export(db, data, digest, output)
        rows = json.loads((output / "0" / "chats.json").read_text())["chats"]
        assert [(row["id"], row["name"]) for row in rows] == [
            (updated.id, "renamed"),
        ]
        assert set(root.iterdir()) == {state_dir, registry, root / "dialog"}
    finally:
        runtime.reset_runtime()


@pytest.mark.asyncio
async def test_bulk_import_rolls_back_new_sessions_on_filename_conflict(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    await apply_sql_script(db, SQL_ROOT / "conversation_mysql_metadata.sql")
    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="svc")
    root = tmp_path / "workspace"
    root.mkdir()
    specs = [
        ChatSpec(
            id=str(uuid4()),
            session_id=session_id,
            user_id="tenant",
            channel="console",
        )
        for session_id in ("new", "taken")
    ]
    (root / "chats.json").write_text(
        json.dumps(
            {
                "version": 1,
                "chats": [spec.model_dump(mode="json") for spec in specs],
            },
        ),
    )
    manifest = tmp_path / "manifest.json"
    metadata.prepare(
        [{"scope": scope.values, "root": str(root)}],
        db.config.database,
        manifest,
    )
    data, digest = metadata.read_manifest(manifest, db.config.database)
    batch = metadata._import_batch

    async def seed_conflicting_owner(
        repo,
        registry,
        root_key,
        registry_digest,
    ):
        await repo.claim_session("taken", "tenant", "zhaohu")
        return await batch(repo, registry, root_key, registry_digest)

    monkeypatch.setattr(metadata, "_import_batch", seed_conflicting_owner)
    result = await metadata.migrate(db, data, digest, apply=True)
    assert result["status"] == "partial_success"
    assert (
        "filename belongs to another identity" in result["failed"][0]["detail"]
    )
    assert await db.fetch_all(
        "SELECT external_session_key FROM swe_conversation_sessions",
    ) == [{"external_session_key": b"taken"}]
    assert not await db.fetch_one("SELECT id FROM swe_conversation_chats")
    assert await db.fetch_one(
        "SELECT imported_count FROM swe_conversation_metadata_workspaces",
    ) == {"imported_count": 0}
    assert await db.fetch_one(
        "SELECT published FROM swe_conversation_metadata_sources",
    ) == {"published": 0}


@pytest.mark.asyncio
async def test_metadata_cli_uses_a_published_sql_registry(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    await apply_sql_script(db, SQL_ROOT / "conversation_mysql_metadata.sql")
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_metadata_performance.sql",
    )
    from swe.database import config

    monkeypatch.setattr(config, "get_database_config", lambda: db.config)
    root = tmp_path / "workspace"
    root.mkdir()
    spec = ChatSpec(id=str(uuid4()), session_id="legacy", user_id="tenant")
    original = {"version": 1, "chats": [spec.model_dump(mode="json")]}
    raw = json.dumps(original)
    (root / "chats.json").write_text(raw)
    mapping = tmp_path / "workspaces.json"
    mapping.write_text(
        json.dumps(
            [
                {
                    "root": str(root),
                    "scope": ["service", "tenant", "source", "agent"],
                },
            ],
        ),
    )
    manifest = str(tmp_path / "manifest.json")

    async def invoke(*args):
        result = await asyncio.to_thread(
            CliRunner().invoke,
            conversation_storage_group,
            list(args),
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    assert (
        await invoke(
            "prepare",
            "--mode",
            "mysql-metadata",
            "--workspaces",
            str(mapping),
            "--manifest",
            manifest,
        )
    )["status"] == "prepared"
    assert (await invoke("migrate", "--manifest", manifest))[
        "status"
    ] == "preview"
    assert (
        await invoke(
            "migrate",
            "--manifest",
            manifest,
            "--apply",
            "--full-audit",
        )
    )["status"] == "ready"
    dest = tmp_path / "export"
    assert (
        await invoke(
            "export-metadata",
            "--manifest",
            manifest,
            "--destination",
            str(dest),
        )
    )["status"] == "exported"
    exported = json.loads((dest / "0" / "chats.json").read_text())
    assert ChatSpec.model_validate(exported["chats"][0]) == spec
    assert (root / "chats.json").read_text() == raw


@pytest.mark.asyncio
async def test_cutover_export_includes_new_tenant_without_source_record(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    await apply_sql_script(db, SQL_ROOT / "conversation_mysql_metadata.sql")
    from swe.database import config

    monkeypatch.setattr(config, "get_database_config", lambda: db.config)
    new_root = tmp_path / "new-workspace"
    new_root.mkdir()
    monkeypatch.setenv(runtime.AUTHORITY_ENV, runtime.METADATA_AUTHORITY)
    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "service")
    await runtime.configure_runtime(db)
    try:
        repo = await runtime.workspace_metadata_repository(
            SimpleNamespace(
                workspace_dir=new_root,
                tenant_id=encode_scope_id("new", "source"),
                agent_id="default",
            ),
        )
        assert await repo.list_chats() == []
        new_chat = ChatSpec(session_id="new-session", user_id="new")
        await repo.upsert_chat(new_chat)
        full_dest = tmp_path / "full-export"
        result = await asyncio.to_thread(
            CliRunner().invoke,
            conversation_storage_group,
            ["export-metadata", "--destination", str(full_dest)],
        )
        assert result.exit_code == 0, result.output
        [exported] = json.loads(result.output)["workspaces"]
        assert exported["root"] == str(new_root)
        new_export = full_dest / exported["registry"]
        assert (
            json.loads(new_export.read_text())["chats"][0]["id"] == new_chat.id
        )
    finally:
        runtime.reset_runtime()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restrict_title", "restore_title"),
    [
        (
            "MODIFY title VARCHAR(20) NOT NULL",
            "MODIFY title TEXT NOT NULL",
        ),
        (
            "ADD CONSTRAINT test_title CHECK (CHAR_LENGTH(title) <= 20)",
            "DROP CHECK test_title",
        ),
    ],
    ids=["data-error", "check-constraint"],
)
async def test_workspace_sql_error_does_not_stop_later_imports(
    conversation_test_database,
    tmp_path,
    restrict_title,
    restore_title,
    monkeypatch,
):
    db = conversation_test_database
    await apply_sql_script(db, SQL_ROOT / "conversation_mysql_metadata.sql")
    workspaces = []
    specs = {}
    for source, agent in (
        ("source-a", "bad"),
        ("source-a", "healthy"),
        ("source-b", "empty"),
    ):
        scope = WorkspaceScope("tenant", source, agent, service_unit_id="svc")
        root = tmp_path / agent
        root.mkdir()
        workspaces.append({"scope": scope.values, "root": str(root)})
        if agent == "empty":
            continue
        spec = ChatSpec(
            name=(
                "title is longer than twenty bytes"
                if agent == "bad"
                else "ordinary"
            ),
            session_id=agent,
            user_id="tenant",
            channel="zhaohu",
            meta={"multi_agent_mode_enabled": True},
        )
        specs[agent] = spec
        (root / "chats.json").write_text(
            json.dumps(
                {"version": 1, "chats": [spec.model_dump(mode="json")]},
            ),
        )

    await db.execute(f"ALTER TABLE swe_conversation_chats {restrict_title}")
    manifest = tmp_path / "manifest.json"
    metadata.prepare(workspaces, db.config.database, manifest)
    data, digest = metadata.read_manifest(manifest, db.config.database)
    report = await metadata.migrate(db, data, digest, apply=True)
    assert report["status"] == "partial_success"
    assert report["published"] == [("svc", "tenant", "source-b")]
    assert [failure["scope"][-1] for failure in report["failed"]] == ["bad"]
    assert report["failed"][0]["root"] == str(tmp_path / "bad")
    assert report["failed"][0]["phase"] == "import"
    assert await db.fetch_all(
        "SELECT agent_key,imported_count FROM swe_conversation_metadata_workspaces "
        "ORDER BY agent_key",
    ) == [
        {"agent_key": b"bad", "imported_count": 0},
        {"agent_key": b"empty", "imported_count": 0},
        {"agent_key": b"healthy", "imported_count": 1},
    ]
    await db.execute(f"ALTER TABLE swe_conversation_chats {restore_title}")
    retry = await metadata.migrate(db, data, digest, apply=True)
    assert retry["published"] == [("svc", "tenant", "source-a")]
    assert retry["already_published"] == [("svc", "tenant", "source-b")]

    scope = WorkspaceScope.from_values(workspaces[1]["scope"])
    repo = MySQLMetadataRepository(db, scope)
    spec = specs["healthy"]
    binding = await repo.compare_and_set_expert_binding(spec.id, 0, "expert")
    chat = await repo.get_chat(spec.id)
    assert binding.expert_id == "expert"
    assert chat.meta["multi_agent_mode_enabled"] is False
    await _assert_native_approval_uses_existing_session(
        repo,
        spec,
        tmp_path,
        monkeypatch,
    )


async def _assert_native_approval_uses_existing_session(
    repo,
    spec,
    tmp_path,
    monkeypatch,
):
    pending = SimpleNamespace(
        request_id="approval-1",
        user_id=spec.user_id,
        session_id=spec.session_id,
        channel=spec.channel,
        extra={"chat_id": spec.id},
        status="pending",
    )
    console = ConsoleChannel(
        process=AsyncMock(),
        enabled=True,
        bot_prefix="",
        workspace_dir=tmp_path,
    )

    async def start_or_attach(chat_id, payload, producer, **kwargs):
        assert chat_id == spec.id
        request = console.build_agent_request_from_native(payload)
        assert request.channel == "zhaohu"
        assert request.channel_meta["chat_id"] == spec.id
        await repo.ensure_session_path(
            request.session_id,
            request.user_id,
            request.channel,
        )
        return SimpleNamespace(is_new_run=True)

    service = SimpleNamespace(
        get_request=AsyncMock(return_value=pending),
        record_external_submission=AsyncMock(),
    )
    workspace = SimpleNamespace(
        chat_manager=ChatManager(repo=repo),
        channel_manager=SimpleNamespace(
            get_channel=lambda name: console if name == "console" else None,
        ),
        task_tracker=SimpleNamespace(),
        answer_turn_coordinator=SimpleNamespace(
            start_or_attach=start_or_attach,
        ),
    )
    monkeypatch.setattr(
        approvals,
        "get_agent_for_request",
        AsyncMock(return_value=workspace),
    )
    monkeypatch.setattr(approvals, "get_approval_service", lambda: service)
    monkeypatch.setattr(external, "get_approval_service", lambda: service)
    monkeypatch.setattr(external, "notify_cron_approval_result", AsyncMock())
    app = FastAPI()
    app.include_router(approvals.router)
    for action in ("approve", "deny"):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/approvals/{pending.request_id}/{action}",
                headers={"X-User-Id": spec.user_id},
            )
        assert response.status_code == 200, response.text
        assert response.json()["chat_id"] == spec.id
    assert service.record_external_submission.await_count == 2
    assert [chat.id for chat in await repo.list_chats()] == [spec.id]


@pytest.mark.asyncio
async def test_resume_repaired_preflight_workspace_keeps_imported_rows(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    await apply_sql_script(db, SQL_ROOT / "conversation_mysql_metadata.sql")
    workspaces = [
        {
            "scope": ["svc", "tenant", "source", agent],
            "root": str(tmp_path / agent),
        }
        for agent in ("missing", "healthy")
    ]
    healthy = tmp_path / "healthy"
    healthy.mkdir()
    spec = ChatSpec(session_id="session", user_id="tenant", channel="console")
    registry = healthy / "chats.json"
    original_registry = json.dumps(
        {"version": 1, "chats": [spec.model_dump(mode="json")]},
    )
    registry.write_text(original_registry)
    manifest = tmp_path / "original.json"
    metadata.prepare(workspaces, db.config.database, manifest)
    original = metadata.read_manifest(manifest, db.config.database)
    progress = metadata.WorkspaceProgress(manifest, original[1], db.config)
    report = await metadata.migrate(
        db,
        *original,
        apply=True,
        progress=progress,
    )
    assert progress.path.is_file()
    assert report["failed"][0]["scope"][-1] == "missing"
    assert report["published"] == []
    before = await db.fetch_all("SELECT id FROM swe_conversation_chats")
    assert len(before) == 1

    (tmp_path / "missing").mkdir()
    registry.write_text(original_registry + "\n")
    rejected = tmp_path / "changed-bound-workspace.json"
    metadata.prepare(workspaces, db.config.database, rejected)
    report = await metadata.migrate(
        db,
        *metadata.read_manifest(rejected, db.config.database),
        apply=True,
        resume_from=original,
    )
    assert "changes a bound workspace" in report["failed"][0]["detail"]

    registry.write_text(original_registry)
    repaired = tmp_path / "repaired.json"
    metadata.prepare(workspaces, db.config.database, repaired)
    monkeypatch.setattr(
        "swe.database.config.get_database_config",
        lambda: db.config,
    )
    verify_workspace = metadata._verify_workspace

    def verify_only_unfinished(item, **kwargs):
        assert item["scope"][-1] != "healthy"
        return verify_workspace(item, **kwargs)

    monkeypatch.setattr(metadata, "_verify_workspace", verify_only_unfinished)
    result = await asyncio.to_thread(
        CliRunner().invoke,
        conversation_storage_group,
        [
            "migrate",
            "--manifest",
            str(repaired),
            "--resume-from",
            str(manifest),
            "--apply",
        ],
    )
    assert result.exit_code == 0, (result.output, result.exception)
    report = json.loads(result.output)
    assert report["skipped_workspaces"] == [
        ["svc", "tenant", "source", "healthy"],
    ]
    assert report["published"] == [
        ["svc", "tenant", "source"],
    ]
    assert (
        await db.fetch_all("SELECT id FROM swe_conversation_chats") == before
    )
