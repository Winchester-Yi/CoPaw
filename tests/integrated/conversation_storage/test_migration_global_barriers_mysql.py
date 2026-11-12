# -*- coding: utf-8 -*-
"""Cross-layer cutoff and all-service finalize barriers on small real inputs."""

import asyncio
import json

import pytest

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.migration.discovery import (
    discover_services,
    freeze_discovery,
)
from swe.app.conversation_storage.migration.freshness import tenant_key
from swe.app.conversation_storage.migration.importer import (
    ParallelWorkspaceImporter,
)
from swe.app.conversation_storage.migration.manifest import SourceBoundary
from swe.app.conversation_storage.migration.parallel_import import (
    MigrationDomainRouter,
)
from swe.app.conversation_storage.migration.v3_manifest import V3Manifest
from swe.cli import conversation_storage_v3_cmd as command
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)
from tests.integrated.conversation_storage.test_migration_lock_loss_mysql import (
    _source,
)


async def _prepare(db, tmp_path, *, share_only=False):
    await ChatShareStore(db).ensure_schema()
    for name in (
        "conversation_share_publication_extension.sql",
        "conversation_migration_control_tables.sql",
    ):
        await apply_sql_script(db, SQL_ROOT / name)
    services, roots = [], {}
    for service in ("A", "B"):
        source, nas = _source(tmp_path / service)
        roots[service] = (source, nas)
        services.append(
            {
                "service_unit_id": service,
                "source_root": str(source),
                "config_root": str(source),
                "original_root": str(nas),
                "nas_root": str(nas),
            },
        )
    if share_only:
        source, nas = tmp_path / "share-source", tmp_path / "share-nas"
        parent = source / "sharetenant/chat_shares"
        parent.mkdir(parents=True)
        nas.mkdir()
        (parent / "share-only.json").write_text(
            json.dumps({"chat_name": "orphan", "messages": []}),
        )
        for path in source.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        source.chmod(0o555)
        roots["SHARES"] = (source, nas)
        services.append(
            {
                "service_unit_id": "SHARES",
                "source_root": str(source),
                "config_root": str(source),
                "snapshot_root": str(source),
                "original_root": str(nas),
                "nas_root": str(nas),
            },
        )
        await db.execute(
            "INSERT INTO swe_chat_shares (token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count,last_access_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                "share-only",
                "missing-chat",
                "alice",
                "sharetenant",
                "sharetenant/chat_shares/share-only.json",
                "2026-11-01 00:00:00",
                3,
                None,
            ),
        )
    shares = await db.fetch_all(
        "SELECT token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count,last_access_at FROM swe_chat_shares",
    )
    report = discover_services(services, shares=shares)
    assert report["ready"], report["blockers"]
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
    return manifest, work, roots


def _restore_roots(roots):
    for source, _nas in roots.values():
        for path in source.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        source.chmod(0o755)


async def _run(operation, manifest, work, **options):
    return await asyncio.to_thread(
        command.run_v3_command,
        operation,
        manifest.path,
        workers=1,
        io_workers=1,
        work_dir=work,
        **options,
    )


async def test_all_service_and_share_only_cutoffs_are_durable_before_first_workspace(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    manifest, work, roots = await _prepare(db, tmp_path, share_only=True)
    monkeypatch.setattr(
        "swe.database.config.get_database_config",
        lambda: db.config,
    )
    expected = {
        tenant_key("A", "default"),
        tenant_key("B", "default"),
        tenant_key("SHARES", "sharetenant"),
    }
    cutoffs, opens = {}, []
    original_open = MigrationDomainRouter._open
    original_finish = ParallelWorkspaceImporter._batch_finished

    async def checked_open(router, workspace):
        saved = V3Manifest.load(manifest.path).data[
            "tenant_sync_started_at_ns"
        ]
        assert set(saved) == expected
        assert all(
            type(value) is int and value > 0 for value in saved.values()
        )
        if not cutoffs:
            cutoffs.update(saved)
        assert saved == cutoffs
        opens.append(tuple(workspace["scope"]))
        return await original_open(router, workspace)

    async def stop_after_committed_batch(importer, position):
        await original_finish(importer, position)
        raise RuntimeError("stop after durable batch")

    monkeypatch.setattr(MigrationDomainRouter, "_open", checked_open)
    try:
        with monkeypatch.context() as interrupted:
            interrupted.setattr(
                ParallelWorkspaceImporter,
                "_batch_finished",
                stop_after_committed_batch,
            )
            with pytest.raises(RuntimeError, match="stop after durable batch"):
                await _run("import", manifest, work, apply=True)
        assert opens
        first_opens = len(opens)
        committed = await db.fetch_one(
            "SELECT COUNT(*) AS total FROM swe_conversation_publications WHERE state='committed'",
        )
        assert committed["total"] > 0
        await _run("resume", manifest, work, apply=True)
        assert len(opens) > first_opens
        assert (
            V3Manifest.load(manifest.path).data["tenant_sync_started_at_ns"]
            == cutoffs
        )
        assert {scope[0] for scope in opens} == {"A", "B", "SHARES"}
    finally:
        _restore_roots(roots)


@pytest.mark.parametrize(
    "fault",
    ["source_after_verify", "service_b_legacy_root", "service_b_recovery"],
)
# pylint: disable-next=too-many-statements
async def test_finalize_requires_source_recheck_and_every_service_body(
    conversation_test_database,
    tmp_path,
    monkeypatch,
    fault,
):
    from swe.app.conversation_storage.migration import verify

    db = conversation_test_database
    manifest, work, roots = await _prepare(db, tmp_path)
    monkeypatch.setattr(
        "swe.database.config.get_database_config",
        lambda: db.config,
    )
    try:
        await _run("import", manifest, work, apply=True)
        await apply_sql_script(
            db,
            SQL_ROOT / "conversation_share_service_unit_finalize.sql",
        )
        observed, normal_a, normal_a_chats, rechecked = [], [], [], []
        original_chat = verify._verify_chat
        original_recheck = command._recheck_spooled_inventory
        original_verify, original_session = (
            command.verify_import,
            verify._verify_session,
        )

        async def record_session(
            database,
            bundles,
            frozen,
            workspace,
            session,
            **options,
        ):
            await original_session(
                database,
                bundles,
                frozen,
                workspace,
                session,
                **options,
            )
            if workspace["scope"][0] == "A":
                normal_a.append(session["position"])

        async def record_chat(
            database,
            bundles,
            frozen,
            workspace,
            chat,
            **options,
        ):
            await original_chat(
                database,
                bundles,
                frozen,
                workspace,
                chat,
                **options,
            )
            if workspace["scope"][0] == "A":
                normal_a_chats.append(chat["position"])

        async def record_recheck(
            manifest,
            expected,
            base,
            *,
            disk_limit,
            record_limit,
            protected,
            workers,
            io_workers,
            memory_bytes,
        ):
            result = await original_recheck(
                manifest,
                expected,
                base,
                disk_limit=disk_limit,
                record_limit=record_limit,
                protected=protected,
                workers=workers,
                io_workers=io_workers,
                memory_bytes=memory_bytes,
            )
            rechecked.append(result)
            return result

        async def verify_then_fault(*args, **kwargs):
            result = await original_verify(*args, **kwargs)
            observed.append(result["passed"])
            if fault != "source_after_verify":
                assert any(
                    item["position"] == target_position
                    for item in result["blockers"]
                ), result
            if fault == "source_after_verify":
                assert result["passed"]
                path = (
                    roots["B"][0] / "default_SRC/workspaces/agent1/agent.json"
                )
                path.chmod(0o644)
                path.write_text('{"id":"changed-after-verify"}')
                path.chmod(0o444)
            return result

        monkeypatch.setattr(command, "verify_import", verify_then_fault)
        monkeypatch.setattr(verify, "_verify_session", record_session)
        monkeypatch.setattr(verify, "_verify_chat", record_chat)
        monkeypatch.setattr(
            command,
            "_recheck_spooled_inventory",
            record_recheck,
        )
        target_position = None
        if fault in ("service_b_legacy_root", "service_b_recovery"):
            row = await db.fetch_one(
                "SELECT p.relative_path,p.byte_length,s.tenant_key,s.source_key,s.agent_key,s.user_key,s.channel_key,s.external_session_key FROM swe_conversation_sessions s JOIN swe_conversation_publications p ON p.id=s.recovery_publication_id AND p.service_unit_key=s.service_unit_key WHERE s.service_unit_key=%s ORDER BY s.id DESC LIMIT 1",
                (b"B",),
            )
            path = roots["B"][1] / row["relative_path"].decode()
            from swe.app.conversation_storage.bundle import BundleStore
            from swe.app.conversation_storage.migration.discovery import (
                source_key,
            )

            target_position = source_key(
                "session",
                "B",
                *(
                    row[key].decode()
                    for key in (
                        "tenant_key",
                        "source_key",
                        "agent_key",
                        "user_key",
                        "channel_key",
                        "external_session_key",
                    )
                ),
            )
            index = BundleStore(roots["B"][1]).inspect(
                row["relative_path"].decode(),
                expected_length=row["byte_length"],
            )
            block = index.blocks[
                (
                    "legacy_root"
                    if fault == "service_b_legacy_root"
                    else "recovery"
                )
            ]
            with path.open("r+b") as handle:
                handle.seek(block.offset + block.length - 1)
                original = handle.read(1)
                assert (
                    original == b"}"
                ), "fixture must corrupt an actual JSON root"
                handle.seek(block.offset + block.length - 1)
                handle.write(b"!")
            assert (
                path.read_bytes()[
                    block.offset
                    + block.length
                    - 1 : block.offset
                    + block.length
                ]
                == b"!"
            )
        output = tmp_path / "completion-output"
        with pytest.raises(
            ValueError,
            match="source inventory changed|semantic verification failed",
        ):
            await _run(
                "finalize",
                manifest,
                work,
                apply=True,
                output_dir=output,
            )
        assert observed == [fault == "source_after_verify"]
        assert len(normal_a) == 2
        assert len(normal_a_chats) == 2
        assert rechecked == ([False] if fault == "source_after_verify" else [])
        state = await db.fetch_one(
            "SELECT state FROM swe_conversation_migration_jobs WHERE migration_id=%s",
            (manifest.migration_id,),
        )
        assert state["state"] != "ready"
        assert not (output / "completion.json").exists()
        assert list(output.glob("*.runtime.json")) == []
    finally:
        _restore_roots(roots)
