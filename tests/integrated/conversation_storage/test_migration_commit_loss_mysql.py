# -*- coding: utf-8 -*-
"""Drop a real driver COMMIT return, then reconcile the original migration."""

import asyncio
from hashlib import sha256

import pytest
from mysql.connector.errors import InterfaceError

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.errors import UnknownCommit
from swe.app.conversation_storage.migration.discovery import (
    discover_services,
    freeze_discovery,
)
from swe.app.conversation_storage.migration.manifest import SourceBoundary
from swe.cli.conversation_storage_v3_cmd import run_v3_command
from swe.database.driver import ManagedMySQLConnection
from swe.database.session import DatabaseSession
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)
from tests.integrated.conversation_storage.test_migration_lock_loss_mysql import (
    _resume_and_verify,
    _source,
)


def _lose_publication_commit_return(fault):
    """Inject inside transaction's driver await, after confirmed server COMMIT.

    Unlike a proxy that raises after transaction.__aexit__, this exercises the
    production failed-commit connection disposal and UnknownCommit conversion.
    Only the physical connection that installed a publication is affected.
    """
    observed = {}
    query = DatabaseSession._query
    execute = ManagedMySQLConnection._execute_query

    async def observe_install(session, statement, params, mode):
        result = await query(session, statement, params, mode)
        if not observed and statement.startswith(
            "UPDATE swe_conversation_publications SET state='committed',",
        ):
            assert session._in_transaction
            observed.update(
                connection=session._connection,
                session=session,
                publication_id=params[0],
                commit_return_lost=False,
            )
        return result

    async def lose_return(connection, statement):
        result = await execute(connection, statement)
        if (
            statement == "COMMIT"
            and connection is observed.get("connection")
            and not observed["commit_return_lost"]
        ):
            # Server execution and driver OK parsing both completed. Suppress
            # only the client's return, while still inside session._run.
            observed["commit_return_lost"] = True
            raise InterfaceError(
                msg="injected lost COMMIT response",
                errno=2013,
            )
        return result

    fault.setattr(DatabaseSession, "_query", observe_install)
    fault.setattr(ManagedMySQLConnection, "_execute_query", lose_return)
    return observed


@pytest.mark.asyncio
async def test_lost_driver_commit_response_resumes_original_manifest(
    conversation_test_database,
    tmp_path,
    monkeypatch,
    record_property,
):
    db = conversation_test_database
    record_property("owned_schema", db.config.database)
    await ChatShareStore(db).ensure_schema()
    for name in (
        "conversation_share_publication_extension.sql",
        "conversation_migration_control_tables.sql",
    ):
        await apply_sql_script(db, SQL_ROOT / name)
    source, nas = _source(tmp_path)
    report = discover_services(
        [
            {
                "service_unit_id": "COMMIT-LOSS-TEST",
                "source_root": str(source),
                "config_root": str(source),
                "nas_root": str(nas),
                "original_root": str(nas),
            },
        ],
    )
    manifest = freeze_discovery(
        report,
        tmp_path / "manifest.json",
        SourceBoundary(
            "snapshot",
            "2026-11-02T00:00:00Z",
            "old@sha256:" + "a" * 64,
            ("stopped", "readonly"),
        ),
        db.config.database,
    )
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(
        "swe.database.config.get_database_config",
        lambda: db.config,
    )
    try:
        with monkeypatch.context() as fault:
            observed = _lose_publication_commit_return(fault)
            with pytest.raises(UnknownCommit) as failed:
                await asyncio.to_thread(
                    run_v3_command,
                    "import",
                    manifest.path,
                    apply=True,
                    workers=1,
                    io_workers=1,
                    work_dir=work,
                )
            assert observed["commit_return_lost"]
            assert not observed["session"]._entry.usable
            assert not observed["session"]._usable
            assert isinstance(failed.value.__cause__, InterfaceError)
            assert failed.value.__cause__.errno == 2013
        # A separate real connection sees the committed row despite the caller's
        # UnknownCommit. The failed transaction must not claim job readiness.
        committed = await db.fetch_one(
            "SELECT id,operation_key,relative_path,state FROM swe_conversation_publications WHERE id=%s",
            (observed["publication_id"],),
        )
        assert committed["state"] == "committed"
        stopped = await db.fetch_one(
            "SELECT COUNT(*) AS total FROM swe_conversation_publications WHERE state='committed'",
        )
        assert stopped["total"] == 1, "import advanced after unknown commit"
        operation = committed["operation_key"].decode()
        assert failed.value.operation_key == operation
        job = await db.fetch_one(
            "SELECT state FROM swe_conversation_migration_jobs WHERE migration_id=%s",
            (manifest.migration_id,),
        )
        assert job["state"] != "ready"
        path = nas / committed["relative_path"].decode()
        original_digest = sha256(path.read_bytes()).digest()
        publish = BundleStore.publish

        def no_republication(store, operation_key, *args, **kwargs):
            assert (
                operation_key != operation
            ), "resume replaced committed operation"
            return publish(store, operation_key, *args, **kwargs)

        with monkeypatch.context() as resumed:
            resumed.setattr(BundleStore, "publish", no_republication)
            await _resume_and_verify(db, manifest, work, tmp_path)
        current = await db.fetch_one(
            "SELECT id,operation_key,relative_path,state FROM swe_conversation_publications WHERE id=%s",
            (committed["id"],),
        )
        assert current == committed
        assert sha256(path.read_bytes()).digest() == original_digest
        for table in ("sessions", "chats", "messages"):
            count = await db.fetch_one(
                "SELECT COUNT(*) AS total FROM swe_conversation_" + table,
            )
            assert count["total"] == 2
        pending = await db.fetch_one(
            "SELECT COUNT(*) AS total FROM swe_conversation_publications WHERE state<>'committed'",
        )
        assert pending["total"] == 0
    finally:
        for path in source.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        source.chmod(0o755)
