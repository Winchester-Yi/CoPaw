# -*- coding: utf-8 -*-
"""Zero-data config repair after an independent tenant has gone online."""

import json
from types import SimpleNamespace

import pytest

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.migration.discovery import (
    discover_services,
    freeze_discovery,
)
from swe.app.conversation_storage.migration.manifest import SourceBoundary
from swe.app.conversation_storage.migration.tenant_retry import review_retry
from swe.app.conversation_storage.runtime import ConversationRuntime
from swe.app.conversation_storage.tenant_admission import TenantNotPublished
from swe.config.context import encode_scope_id
from swe.cli import conversation_storage_tenants as tenant_cli
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)
from tests.integrated.conversation_storage.test_tenant_cutover_mysql import (
    _frozen_tenant,
)


# Keep the complete publication/online-write/review/resume scenario together.
@pytest.mark.asyncio
async def test_review_failed_config_then_resume_preserves_published_account(  # pylint: disable=too-many-statements
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_migration_control_tables.sql",
    )
    source, original, nas = (
        tmp_path / name for name in ("frozen", "runtime", "body")
    )
    nas.mkdir()
    _chat_a, runtime_a = _frozen_tenant(source, original, "alice")
    _chat_b, _runtime_b = _frozen_tenant(source, original, "bob")
    config_b = source / encode_scope_id("bob", "SRC") / "config.json"
    original_config = config_b.read_bytes()
    config_b.write_text("{invalid")
    for path in source.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    services = [
        {
            "service_unit_id": "svc",
            "source_root": str(source),
            "config_root": str(source),
            "original_root": str(original),
            "nas_root": str(nas),
        },
    ]
    report = discover_services(services, tenant_isolation=True)
    assert {t["tenant_id"]: t["state"] for t in report["tenants"]} == {
        "alice": "discovered",
        "bob": "failed",
    }
    manifest = freeze_discovery(
        report,
        tmp_path / "manifest.json",
        SourceBoundary(
            "snapshot",
            "2026-11-13T00:00:00Z",
            "old@sha256:" + "a" * 64,
            ("stopped", "readonly"),
        ),
        db.config.database,
    )
    monkeypatch.setattr(
        "swe.database.config.get_database_config",
        lambda: db.config,
    )
    work = tmp_path / "work"
    work.mkdir()
    resources = {
        "workers": 2,
        "io_workers": 2,
        "memory_budget_mib": 128,
        "work_dir": str(work),
        "apply": True,
    }
    await tenant_cli.run_tenants("import", manifest, None, **resources)
    first = await tenant_cli.run_tenants(
        "finalize",
        manifest,
        tmp_path / "completion",
        **resources,
    )
    assert first["published"] == 1
    document = {
        "version": 3,
        "service_unit_id": "svc",
        "nas_root": str(nas),
        "migration_id": manifest.migration_id,
    }
    runtime = ConversationRuntime(
        db,
        document,
        b"cursor",
        service_unit_id="svc",
    )
    await runtime.admit("alice")
    with pytest.raises(TenantNotPublished):
        await runtime.admit("bob")
    storage = runtime.for_workspace(
        SimpleNamespace(
            workspace_dir=runtime_a,
            agent_id="agent",
            conversation_scope=WorkspaceScope(
                "alice",
                "SRC",
                "agent",
                service_unit_id="svc",
            ),
        ),
    )
    await storage.db.execute(
        "UPDATE swe_conversation_chats SET title=%s WHERE tenant_key=%s",
        ("online title", b"alice"),
    )
    before = await db.fetch_all(
        "SELECT * FROM swe_conversation_chats WHERE tenant_key=%s ORDER BY id",
        (b"alice",),
    )
    config_b.chmod(0o644)
    config_b.write_bytes(original_config)
    config_b.chmod(0o444)
    reviewed = discover_services(services, tenant_isolation=True)
    assert reviewed["ready"], reviewed["blockers"]
    async with db.named_lock(
        "conversation-migration:" + db.config.database,
        timeout=0,
    ):
        result = await review_retry(db, manifest, reviewed)
    assert result["outcome"] == "new"
    real_inventory = tenant_cli._inventory
    parsed = []

    async def only_unpublished(view, spool, options):
        parsed.append(view.key[1])
        assert view.key[1] == "bob"
        return await real_inventory(view, spool, options)

    monkeypatch.setattr(tenant_cli, "_inventory", only_unpublished)
    resumed = await tenant_cli.run_tenants(
        "resume",
        manifest,
        None,
        **resources,
    )
    assert resumed["failed"] == 0
    final = await tenant_cli.run_tenants(
        "finalize",
        manifest,
        tmp_path / "completion",
        **resources,
    )
    assert final["published"] == 2
    assert parsed and set(parsed) == {"bob"}
    assert (
        await db.fetch_all(
            "SELECT * FROM swe_conversation_chats WHERE tenant_key=%s ORDER BY id",
            (b"alice",),
        )
        == before
    )
    await runtime.admit("bob")
    job = await db.fetch_one(
        "SELECT verification_text FROM swe_conversation_migration_jobs WHERE migration_id=%s",
        (manifest.migration_id.encode(),),
    )
    assert (
        json.loads(job["verification_text"])["tenant_review_revision"]
        == result["revision"]
    )
