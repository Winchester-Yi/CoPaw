# -*- coding: utf-8 -*-
"""Installer gates are exercised without any application-schema DDL."""

import pytest

from swe.app.conversation_storage.migration.manifest import (
    MigrationManifest,
    SourceBoundary,
)
from swe.app.conversation_storage.migration.install_records import (
    OfflineTarget,
)


def manifest_for(tmp_path):
    manifest = MigrationManifest.create(
        tmp_path / "manifest.json",
        SourceBoundary(
            "snap",
            "2026-10-30T00:00:00Z",
            "old@sha256:" + "a" * 64,
            ("stopped", "readonly"),
        ),
        [],
    )
    manifest.data["offline"] = {
        "application_schema": "app",
        "nas_root": str(tmp_path),
    }
    manifest._persist()
    return manifest


def test_plan_renames_exactly_seven_tables_and_preserves_old_share(tmp_path):
    from swe.app.conversation_storage.migration.install import (
        ConversationInstaller,
        TABLES,
    )

    manifest = manifest_for(tmp_path)
    staging = "conversation_staging_" + manifest.migration_id.replace("-", "")
    target = OfflineTarget(staging, "app", manifest.migration_id)
    installer = ConversationInstaller(None, manifest, target)
    sql = installer.rename_sql()
    assert sql.startswith("RENAME TABLE ")
    assert sql.count(" TO ") == 8
    assert (
        f"`app`.`swe_chat_shares` TO `{staging}`.`{installer.old_share}`"
        in sql
    )
    for table in TABLES:
        assert f"`{staging}`.`{table}` TO `app`.`{table}`" in sql
    reverse = installer.rename_sql(reverse=True)
    assert reverse.count(" TO ") == 8
    assert (
        f"`{staging}`.`{installer.old_share}` TO `app`.`swe_chat_shares`"
        in reverse
    )


def test_even_safe_looking_nongenerated_staging_is_rejected(tmp_path):
    from swe.app.conversation_storage.migration.install import (
        ConversationInstaller,
    )

    manifest = manifest_for(tmp_path)
    with pytest.raises(ValueError, match="generated"):
        ConversationInstaller(
            None,
            manifest,
            OfflineTarget(
                "conversation_staging_manual",
                "app",
                manifest.migration_id,
            ),
        )


async def test_verify_blocker_cannot_be_bypassed_by_manifest_boolean(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.migration import install, verify

    manifest = manifest_for(tmp_path)
    manifest.data["verification"] = {"passed": True, "blockers": []}
    target = OfflineTarget(
        "conversation_staging_" + manifest.migration_id.replace("-", ""),
        "app",
        manifest.migration_id,
    )
    installer = install.ConversationInstaller(None, manifest, target)

    async def blocked(*_):
        return {
            "passed": False,
            "checked": [],
            "blockers": [{"detail": "missing source object"}],
        }

    monkeypatch.setattr(verify, "verify_import", blocked)
    with pytest.raises(ValueError, match="verification"):
        await installer.install(
            None,
            None,
            {"workspaces": [], "shares": [], "files": []},
        )
    assert not installer.receipt_path.exists()


def test_partial_verification_report_rejected(tmp_path):
    from swe.app.conversation_storage.migration.install import (
        check_verification,
    )

    expected = {
        "workspaces": [
            {
                "sessions": [{"position": "session:0"}],
                "chats": [{"position": "chat:0"}],
            },
        ],
        "shares": [],
        "files": [],
    }
    with pytest.raises(ValueError, match="coverage"):
        check_verification(
            {"passed": True, "blockers": [], "checked": ["session:0"]},
            expected,
        )


async def test_cross_server_staging_rejected_before_ddl(tmp_path):
    from swe.app.conversation_storage.migration.install import (
        ConversationInstaller,
    )

    manifest = manifest_for(tmp_path)
    target = OfflineTarget(
        "conversation_staging_" + manifest.migration_id.replace("-", ""),
        "app",
        manifest.migration_id,
    )

    class ReadOnly:
        async def fetch_one(self, _query, _params=None):
            return {
                "server_uuid": "server-b",
                "version": "8.0.35",
                "schema_name": target.schema,
            }

    installer = ConversationInstaller(None, manifest, target)
    with pytest.raises(ValueError, match="same MySQL instance"):
        await installer.check_server(
            ReadOnly(),
            {"server_uuid": "server-a", "version": "8.0.35"},
        )


async def test_live_isolated_atomic_rename_probe(conversation_test_database):
    from swe.app.conversation_storage.migration.install import (
        probe_atomic_rename,
    )

    db = conversation_test_database
    report = await probe_atomic_rename(db)
    assert report["failure_atomic"] and report["success_atomic"]
    assert report["server_uuid"]


# pylint: disable=too-many-statements
async def test_live_seven_table_install_and_before_startup_reversal(
    conversation_test_database,
    tmp_path,
):
    from uuid import uuid4
    from swe.app.chat_sharing.store import ChatShareStore
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.migration.install import (
        ConversationInstaller,
        TABLES,
    )
    from swe.app.conversation_storage.migration.importer import (
        ConversationImporter,
    )
    from swe.app.conversation_storage.migration.inventory import (
        WorkspaceMapping,
        inventory,
    )
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.database.connection import DatabaseConnection

    control = conversation_test_database
    app_schema = "test_install_app_" + uuid4().hex
    manifest = manifest_for(tmp_path)
    manifest.data["offline"]["application_schema"] = app_schema
    source = tmp_path / "frozen"
    source.mkdir()
    (source / "chats.json").write_text('{"chats": []}')
    (source / "share.json").write_text('{"chat_name": "old", "messages": []}')
    (source / "chats.json").chmod(0o444)
    (source / "share.json").chmod(0o444)
    source.chmod(0o555)
    manifest.data["workspaces"] = [
        {
            "root": str(source),
            "tenant_id": "tenant",
            "source_id": "source",
            "agent_id": "agent",
        },
    ]
    manifest._persist()
    target = OfflineTarget(
        "conversation_staging_" + manifest.migration_id.replace("-", ""),
        app_schema,
        manifest.migration_id,
    )
    app = DatabaseConnection(
        control.config.model_copy(update={"database": app_schema}),
    )
    staging = DatabaseConnection(
        control.config.model_copy(update={"database": target.schema}),
    )
    try:
        await control.execute(f"CREATE DATABASE `{app_schema}`")
        await app.connect()
        await ChatShareStore(app).ensure_schema()
        await app.execute(
            "INSERT INTO swe_chat_shares (token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count) VALUES ('permanent','deleted-chat','user','tenant','share.json','2026-10-30 00:00:00',17)",
        )
        await app.execute("CREATE TABLE unrelated_business (id INT)")
        await app.execute("INSERT INTO unrelated_business VALUES (91)")
        installer = ConversationInstaller(app, manifest, target)
        await installer.prepare_staging()
        await staging.connect()
        bundles = BundleStore(tmp_path)
        report = inventory(
            [
                WorkspaceMapping(
                    WorkspaceScope("tenant", "source", "agent"),
                    source,
                ),
            ],
            SourceBoundary(**manifest.data["source_boundary"]),
            shares=[
                {
                    "token": "permanent",
                    "chat_id": "deleted-chat",
                    "creator_id": "user",
                    "tenant_id": "tenant",
                    "snapshot_key": "share.json",
                    "created_at": "2026-10-30T00:00:00Z",
                    "access_count": 17,
                    "last_access_at": None,
                    "snapshot_workspace_index": 0,
                    "snapshot_path": "share.json",
                },
            ],
        )
        await ConversationImporter(staging, bundles, manifest, target).run(
            report,
        )
        from swe.app.conversation_storage.migration.verify import verify_import
        from swe.cli.conversation_storage_cmd import _readonly_manifest

        before_retention = await retention_snapshot(staging)
        before_manifest = manifest.path.read_bytes()
        checked = await verify_import(
            staging,
            bundles,
            _readonly_manifest(manifest),
            report,
            target,
        )
        assert checked["passed"], checked["blockers"]
        assert await retention_snapshot(staging) == before_retention
        assert manifest.path.read_bytes() == before_manifest
        receipt = await installer.install(staging, bundles, report)
        assert receipt["phase"] == "installed_before_startup"
        assert set(receipt["after"]["application_tables"]) == set(TABLES)
        assert (await app.fetch_one("SELECT id FROM unrelated_business"))[
            "id"
        ] == 91
        # No new process has started, and no data write occurred after install.
        # This is the only direct-rename rollback window.
        await app.execute(receipt["reverse_sql_before_any_startup_or_write"])
        columns = await app.fetch_all("SHOW COLUMNS FROM swe_chat_shares")
        assert len(columns) == 8
        assert (
            await app.fetch_one(
                "SELECT access_count FROM swe_chat_shares WHERE token='permanent'",
            )
        )["access_count"] == 17
        assert (await app.fetch_one("SELECT id FROM unrelated_business"))[
            "id"
        ] == 91
        with pytest.raises(ValueError, match="receipt already exists"):
            await installer.install(staging, bundles, report)
    finally:
        await staging.close()
        await app.close()
        await control.execute(f"DROP DATABASE IF EXISTS `{target.schema}`")
        await control.execute(f"DROP DATABASE IF EXISTS `{app_schema}`")


async def test_omitted_or_changed_old_share_rows_block_install():
    from swe.app.conversation_storage.migration.install import (
        _check_share_rows,
    )

    class Database:
        async def fetch_one(self, _query, _params=None):
            return {"n": 1}

        async def fetch_all(self, query, _params=None):
            return [
                {
                    "token": "permanent",
                    "access_count": 4 if "`old`" in query else 0,
                },
            ]

    with pytest.raises(ValueError, match="share rows"):
        await _check_share_rows(Database(), "stage", "old")


async def test_lost_rename_reply_leaves_pending_receipt_and_blocks_retry(
    tmp_path,
    monkeypatch,
):
    import json
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.migration import install, verify

    manifest = manifest_for(tmp_path)
    target = OfflineTarget(
        "conversation_staging_" + manifest.migration_id.replace("-", ""),
        "app",
        manifest.migration_id,
    )

    class Database:
        @asynccontextmanager
        async def named_lock(self, *_args, **_kwargs):
            yield

        @asynccontextmanager
        async def acquire(self):
            yield self

        async def execute(self, query):
            if query.startswith("RENAME"):
                raise RuntimeError("reply lost")

    installer = install.ConversationInstaller(Database(), manifest, target)

    async def verified(*_args):
        return {"passed": True, "blockers": [], "checked": []}

    async def preflight(*_args):
        return {"server": {"server_uuid": "same"}}

    async def probe(*_args):
        return {"server_uuid": "same"}

    async def mdl(*_args):
        pass

    monkeypatch.setattr(verify, "verify_import", verified)
    monkeypatch.setattr(installer, "preflight", preflight)
    monkeypatch.setattr(install, "probe_atomic_rename", probe)
    monkeypatch.setattr(install, "_check_mdl", mdl)
    inventory = {"workspaces": [], "shares": [], "files": []}
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        await installer.install(None, None, inventory)
    assert (
        json.loads(installer.receipt_path.read_text())["phase"]
        == "rename_pending"
    )
    with pytest.raises(ValueError, match="receipt already exists"):
        await installer.install(None, None, inventory)


async def test_offline_readonly_retention_never_writes_and_checks_scope():
    from contextlib import asynccontextmanager
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.conversation_storage.migration.readonly import (
        ReadOnlyDatabase,
        ReadOnlyRetention,
    )
    from swe.app.conversation_storage.errors import IntegrityError

    class Database:
        is_connected = True

        async def fetch_all(self, _query, _params=None):
            return [
                {
                    "id": 1,
                    "scope_kind": "workspace",
                    "tenant_key": b"tenant",
                    "source_key": b"source",
                    "agent_key": b"agent",
                    "state": "committed",
                    "gc_state": "live",
                },
            ]

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def execute(self, *_args):
            pytest.fail("offline verification executed SQL mutation")

    boundary = SourceBoundary(
        "snapshot",
        "2026-10-30T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("stopped", "readonly"),
    )
    db = ReadOnlyDatabase(Database(), boundary)
    retention = ReadOnlyRetention(
        db,
        WorkspaceScope("tenant", "source", "agent"),
    )
    batch = await retention.pin([1], kind="reader")
    assert [row["id"] for row in batch.publications] == [1]
    await retention.finish_reader(batch)
    with pytest.raises(RuntimeError, match="read-only"):
        await db.execute("UPDATE anything SET value=1")
    with pytest.raises(RuntimeError, match="read-only"):
        await db.fetch_all("DELETE FROM anything")
    with pytest.raises(IntegrityError, match="scope"):
        await ReadOnlyRetention(
            db,
            WorkspaceScope("wrong", "source", "agent"),
        ).pin([1], kind="reader")


@pytest.mark.parametrize(
    "field,value",
    [("COLUMN_DEFAULT", "unexpected"), ("EXTRA", "")],
)
async def test_default_and_auto_increment_drift_rejected_before_install(
    monkeypatch,
    field,
    value,
):
    from swe.app.conversation_storage.migration import install
    from swe.app.conversation_storage.schema_contract import (
        COLUMNS,
        DEFAULTS,
        EXTRAS,
        INDEXES,
    )

    async def columns(_db, _schema, table):
        if table not in COLUMNS:
            return [{"COLUMN_NAME": name} for name in install.SHARE_COLUMNS]
        rows = [
            {
                "COLUMN_NAME": name,
                "COLUMN_TYPE": spec[0],
                "IS_NULLABLE": spec[1],
                "COLUMN_DEFAULT": DEFAULTS[table].get(name),
                "EXTRA": EXTRAS[table].get(name, ""),
                "GENERATION_EXPRESSION": "",
            }
            for name, spec in COLUMNS[table].items()
        ]
        if table == "swe_conversation_sessions":
            rows[0][field] = value
        return rows

    async def indexes(_db, _schema, table):
        return INDEXES.get(table, {})

    monkeypatch.setattr(install, "_columns", columns)
    monkeypatch.setattr(install, "_indexes", indexes)
    with pytest.raises(ValueError, match="default|auto_increment"):
        await install._check_definitions(None, "staging", "application")


async def retention_snapshot(database):
    rows = await database.fetch_all(
        "SELECT * FROM swe_conversation_retentions ORDER BY id",
    )
    counter = await database.fetch_one(
        "SELECT AUTO_INCREMENT AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='swe_conversation_retentions'",
    )
    return rows, counter


async def test_readonly_recovery_hydrates_direct_messages_and_cold_turn_without_pins(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.execution import ExecutionStore
    from swe.app.conversation_storage.bundle import new_operation_key
    from swe.app.conversation_storage.identity import SessionIdentity
    from swe.app.conversation_storage.session_adapter import (
        MySQLSessionAdapter,
    )
    from swe.app.conversation_storage.migration.readonly import (
        ReadOnlyDatabase,
        read_only_adapter,
    )
    from tests.integrated.conversation_storage.test_archive_runtime_mysql import (
        setup,
        source,
    )

    db = conversation_test_database
    _, _, _, store = await setup(db, tmp_path)
    identity = SessionIdentity(store.scope, "alice", "console", "session")
    execution = ExecutionStore(
        db,
        owner_key="owner",
        lease_seconds=60,
        publications=store.publications,
    )
    admission = await execution.reserve_admission(
        identity,
        "first",
        "q",
        operation_key=new_operation_key(),
        runtime_turn_key="runtime-q",
    )
    question = source("q", "question").to_dict()
    turn = {
        "status": "completed",
        "chat_id": "first",
        "message": question,
        "unknown": {"preserve": True},
    }
    state = {
        "turn_states": {"q": turn},
        "future": {"keep": True},
        "agent": {"memory": {"content": [[question, []]]}},
    }
    await execution.publish_admission(admission, question, state)
    await execution.finish(
        admission.authority,
        "completed",
        state,
        operation_key=new_operation_key(),
    )
    expected = await store.session_adapter.get_session_state_dict(
        "session",
        "alice",
    )
    expected = await store.session_adapter._save_state(identity, expected)
    before = await retention_snapshot(db)
    boundary = SourceBoundary(
        "frozen",
        "2026-10-30T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("writers-stopped", "read-only"),
    )
    adapter = read_only_adapter(
        MySQLSessionAdapter(
            ReadOnlyDatabase(db, boundary),
            store.scope,
            store.bundles,
            channel="console",
        ),
    )
    assert await adapter.get_session_state_dict("session", "alice") == expected
    assert await adapter.get_turn_state("session", "alice", {}, "q") == turn
    assert await retention_snapshot(db) == before


async def test_probe_cleanup_attempts_both_owned_schemas_after_error():
    from swe.app.conversation_storage.migration.install import (
        _cleanup_probe_schemas,
    )

    source = "conversation_probe_" + "a" * 32
    target = "conversation_probe_dst_" + "a" * 32
    attempts = []

    class Database:
        async def execute(self, query):
            attempts.append(query)
            if len(attempts) == 1:
                raise TimeoutError("reply lost")

    with pytest.raises(RuntimeError, match="probe cleanup"):
        await _cleanup_probe_schemas(Database(), [source, target])
    assert attempts == [
        f"DROP DATABASE `{target}`",
        f"DROP DATABASE `{source}`",
    ]


def install_catalog(monkeypatch, *, change=None):
    """A complete reviewed catalog; one supplied metadata cell may drift."""
    from copy import deepcopy
    from swe.app.conversation_storage.migration import install
    from swe.app.conversation_storage.schema_contract import (
        COLUMNS,
        DEFAULTS,
        EXTRAS,
        INDEXES,
    )

    catalog = {}
    for table, column_definitions in COLUMNS.items():
        catalog[table] = []
        for name, (column_type, nullable) in column_definitions.items():
            charset, collation = None, None
            if column_type.startswith("varchar"):
                charset, collation = "ascii", "ascii_bin"
            elif column_type in ("text", "mediumtext"):
                charset, collation = "utf8mb4", "utf8mb4_0900_ai_ci"
            catalog[table].append(
                {
                    "COLUMN_NAME": name,
                    "COLUMN_TYPE": column_type,
                    "IS_NULLABLE": nullable,
                    "COLUMN_DEFAULT": DEFAULTS[table].get(name),
                    "EXTRA": EXTRAS[table].get(name, ""),
                    "GENERATION_EXPRESSION": "",
                    "CHARACTER_SET_NAME": charset,
                    "COLLATION_NAME": collation,
                },
            )
    original_share = [{"COLUMN_NAME": name} for name in install.SHARE_COLUMNS]
    catalog[install.SHARE] = deepcopy(original_share) + [
        {
            "COLUMN_NAME": name,
            "COLUMN_TYPE": "bigint unsigned",
            "IS_NULLABLE": "YES",
            "COLUMN_DEFAULT": None,
            "EXTRA": "",
            "GENERATION_EXPRESSION": "",
            "CHARACTER_SET_NAME": None,
            "COLLATION_NAME": None,
        }
        for name in ("publication_id", "byte_offset", "byte_length")
    ]
    if change is not None:
        table, column, field, value = change
        next(row for row in catalog[table] if row["COLUMN_NAME"] == column)[
            field
        ] = value

    async def columns(_database, schema, table):
        if table == install.SHARE and schema == "application":
            return deepcopy(original_share)
        return deepcopy(catalog[table])

    async def indexes(_database, schema, table):
        if table in INDEXES:
            return INDEXES[table]
        return (
            {}
            if schema == "application"
            else {"ix_swe_chat_shares_publication": (1, ("publication_id",))}
        )

    monkeypatch.setattr(install, "_columns", columns)
    monkeypatch.setattr(install, "_indexes", indexes)
    return install


@pytest.mark.parametrize(
    "column,field,value",
    [
        ("title", "CHARACTER_SET_NAME", "ascii"),
        ("metadata_text", "CHARACTER_SET_NAME", "ascii"),
        ("title", "CHARACTER_SET_NAME", None),
        ("title", "CHARACTER_SET_NAME", "unknown_charset"),
        ("title", "COLLATION_NAME", "ascii_bin"),
        ("title", "COLLATION_NAME", None),
        ("status", "CHARACTER_SET_NAME", "utf8mb4"),
        ("status", "COLLATION_NAME", "ascii_general_ci"),
        ("id", "CHARACTER_SET_NAME", "ascii"),
        ("id", "COLLATION_NAME", "ascii_bin"),
    ],
)
async def test_install_rejects_charset_or_collation_drift(
    monkeypatch,
    column,
    field,
    value,
):
    install = install_catalog(
        monkeypatch,
        change=("swe_conversation_chats", column, field, value),
    )
    with pytest.raises(ValueError, match="charset/collation"):
        await install._check_definitions(None, "staging", "application")


@pytest.mark.parametrize(
    "collation",
    ["utf8mb4_0900_ai_ci", "utf8mb4_bin", "utf8mb4_unicode_ci"],
)
async def test_install_accepts_correct_utf8mb4_default_collations(
    monkeypatch,
    collation,
):
    install = install_catalog(
        monkeypatch,
        change=(
            "swe_conversation_chats",
            "title",
            "COLLATION_NAME",
            collation,
        ),
    )
    await install._check_definitions(None, "staging", "application")


@pytest.mark.parametrize(
    "column",
    ["publication_id", "byte_offset", "byte_length"],
)
@pytest.mark.parametrize(
    "field,value",
    [
        ("COLUMN_DEFAULT", "0"),
        ("COLUMN_DEFAULT", "NULL"),
        ("EXTRA", "auto_increment"),
        ("EXTRA", "DEFAULT_GENERATED"),
        ("GENERATION_EXPRESSION", "(`publication_id` + 1)"),
        ("CHARACTER_SET_NAME", "ascii"),
        ("COLLATION_NAME", "ascii_bin"),
    ],
)
async def test_install_rejects_share_locator_definition_drift(
    monkeypatch,
    column,
    field,
    value,
):
    install = install_catalog(
        monkeypatch,
        change=("swe_chat_shares", column, field, value),
    )
    with pytest.raises(ValueError, match="locator"):
        await install._check_definitions(None, "staging", "application")


async def test_reviewed_schema_preserves_chinese_and_four_byte_metadata(
    conversation_test_database,
):
    from tests.integrated.conversation_storage.test_schema import (
        insert_chat,
        insert_session,
    )

    db = conversation_test_database
    session = await insert_session(db)
    chat = await insert_chat(db, session, b"unicode")
    title, metadata = "中文会话😀", '{"说明":"中文及😀"}'
    await db.execute(
        "UPDATE swe_conversation_chats SET title=%s,metadata_text=%s WHERE id=%s",
        (title, metadata, chat),
    )
    assert await db.fetch_one(
        "SELECT title,metadata_text FROM swe_conversation_chats WHERE id=%s",
        (chat,),
    ) == {"title": title, "metadata_text": metadata}
