# -*- coding: utf-8 -*-
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.errors import IntegrityError
from swe.app.conversation_storage.migration.exporter import OldFormatTarget
from swe.app.conversation_storage.migration.manifest import SourceBoundary
from swe.app.conversation_storage.migration.tenant_rollback import (
    TenantRollbackEvidence,
    plan_tenant_rollback,
    validate_current_config,
)


def fixture(tmp_path):
    roots = {
        key: str(tmp_path / key)
        for key in ("source_root", "config_root", "nas_root", "original_root")
    }
    for value in roots.values():
        __import__("pathlib").Path(value).mkdir()
    original = tmp_path / "original_root"
    workspace = original / "alice" / "agent"
    workspace.mkdir(parents=True)
    (workspace / "agent.json").write_text('{"id":"agent"}')
    config = original / "alice" / "config.json"
    config.write_text(
        json.dumps(
            {
                "agents": {
                    "profiles": {
                        "agent": {
                            "id": "agent",
                            "workspace_dir": str(workspace),
                        },
                    },
                },
            },
        ),
    )
    service = {"service_unit_id": "svc", **roots}
    mapping = {
        "service_unit_id": "svc",
        "tenant_id": "alice",
        "source_id": "SRC",
        "config_path": str(config),
        "review_ref": "review-ticket",
    }
    return service, mapping, workspace


def test_current_mapping_binds_profile_and_agent_identity(tmp_path):
    service, mapping, workspace = fixture(tmp_path)
    assert validate_current_config(mapping, [service]) == [
        {"root": str(workspace), "scope": ["svc", "alice", "SRC", "agent"]},
    ]
    (workspace / "agent.json").write_text('{"id":"other"}')
    with pytest.raises(IntegrityError, match="evidence"):
        validate_current_config(mapping, [service])


def test_current_mapping_rejects_symlink_and_unreviewed_identity(tmp_path):
    service, mapping, workspace = fixture(tmp_path)
    (workspace / "agent.json").unlink()
    (workspace / "agent.json").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        validate_current_config(mapping, [service])
    mapping["review_ref"] = ""
    with pytest.raises(ValueError, match="reviewed"):
        validate_current_config(mapping, [service])


async def test_preflight_never_claims_complete_or_creates_destination(
    tmp_path,
):
    service, mapping, _workspace = fixture(tmp_path)
    target = OldFormatTarget("old@sha256:" + "a" * 64, 2, 1)
    boundary = SourceBoundary(
        "stopped",
        "2026-11-13T00:00:00Z",
        target.image_digest,
        ("review",),
    )
    evidence = TenantRollbackEvidence(boundary, "stop", "drain", "review")
    manifest = SimpleNamespace(
        migration_id="00000000-0000-4000-8000-000000000001",
        data={
            "tenant_isolation": True,
            "services": [service],
            "tenants": [
                {"service_unit_id": "svc", "tenant_id": "alice"},
                {"service_unit_id": "svc", "tenant_id": "bob"},
            ],
        },
    )
    db = SimpleNamespace(
        fetch_all=AsyncMock(
            return_value=[
                {
                    "service_unit_key": b"svc",
                    "tenant_key": b"alice",
                    "state": "published",
                },
                {
                    "service_unit_key": b"svc",
                    "tenant_key": b"bob",
                    "state": "failed",
                },
            ],
        ),
    )
    destination = tmp_path / "rollback"
    report = await plan_tenant_rollback(
        db,
        manifest,
        destination,
        target,
        evidence,
        current_configs=[mapping],
    )
    assert not report["passed"] and not report["rollback_ready"]
    assert not report["mutated"] and not destination.exists()
    assert "whole_service_export_not_verified" in report["unresolved_blockers"]
    with pytest.raises(IntegrityError, match="overlaps"):
        await plan_tenant_rollback(
            db,
            manifest,
            tmp_path / "source_root" / "output",
            target,
            evidence,
            current_configs=[mapping],
        )


async def test_compose_unpublished_uses_frozen_inputs_and_never_partial_sql(
    tmp_path,
):
    from swe.app.conversation_storage.migration.tenant_rollback import (
        compose_tenant_rollback,
    )

    service, mapping, _workspace = fixture(tmp_path)
    source = tmp_path / "source_root"
    frozen = source / "alice" / "agent"
    frozen.mkdir(parents=True)
    (frozen / "chats.json").write_text('{"version":1,"chats":[]}')
    (frozen / "agent.json").write_text('{"id":"agent"}')
    (source / "alice" / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "profiles": {
                        "agent": {
                            "id": "agent",
                            "workspace_dir": str(
                                tmp_path / "original_root" / "alice" / "agent",
                            ),
                        },
                    },
                },
            },
        ),
    )
    service["tenant_sources"] = {"alice": ["SRC"]}
    service["config_root"] = str(source)
    for path in source.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    target = OldFormatTarget("old@sha256:" + "a" * 64, 2, 1)
    evidence = TenantRollbackEvidence(
        SourceBoundary(
            "stop",
            "2026-11-13T00:00:00Z",
            target.image_digest,
            ("review",),
        ),
        "stop",
        "drain",
        "review",
    )
    manifest = SimpleNamespace(
        migration_id="00000000-0000-4000-8000-000000000001",
        data={
            "tenant_isolation": True,
            "services": [service],
            "shares": [],
            "tenants": [
                {
                    "service_unit_id": "svc",
                    "tenant_id": "alice",
                    "state": "discovered",
                },
            ],
            "workspaces": [
                {
                    "service_unit_id": "svc",
                    "tenant_id": "alice",
                    "root": str(frozen),
                    "evidence_refs": [],
                },
            ],
        },
    )

    async def rows(query, params=None):
        if "migration_tenants" in query:
            return [
                {
                    "service_unit_key": b"svc",
                    "tenant_key": b"alice",
                    "state": "failed",
                },
            ]
        if "SELECT DISTINCT" in query:
            # Deliberately present half-imported SQL data, never copied out.
            return [
                {
                    "service_unit_key": b"svc",
                    "tenant_key": b"alice",
                    "source_key": b"SRC",
                    "agent_key": b"agent",
                },
            ]
        if "swe_chat_shares" in query:
            return []
        raise AssertionError(query)

    db = SimpleNamespace(fetch_all=AsyncMock(side_effect=rows))
    report = await compose_tenant_rollback(
        db,
        manifest,
        tmp_path / "output",
        target,
        evidence,
        current_configs=[],
    )
    assert report["content_verified"] and not report["rollback_ready"]
    from pathlib import Path

    output = Path(report["services"]["svc"]) / "alice" / "agent"
    assert (output / "chats.json").read_bytes() == (
        frozen / "chats.json"
    ).read_bytes()
    assert not report["workspace_reports"]
    with pytest.raises(IntegrityError, match="empty"):
        await compose_tenant_rollback(
            db,
            manifest,
            tmp_path / "output",
            target,
            evidence,
            current_configs=[],
        )


async def test_share_merge_uses_token_service_not_same_named_unpublished_account(
    tmp_path,
):
    from swe.app.conversation_storage.migration.tenant_rollback import (
        _merge_shares,
    )

    roots = {service: tmp_path / service for service in ("svcA", "svcB")}
    for root in roots.values():
        root.mkdir()
    (roots["svcA"] / "alice" / "chat_shares").mkdir(parents=True)
    (roots["svcA"] / "alice" / "chat_shares" / "token.json").write_text(
        '{"private":"A"}',
    )
    share = {
        "token": "token",
        "tenant_id": "alice",
        "chat_id": "chat",
        "creator_id": "alice",
        "snapshot_key": "alice/chat_shares/token.json",
        "created_at": None,
        "last_access_at": None,
        "access_count": 7,
    }
    context = SimpleNamespace(
        database=SimpleNamespace(
            fetch_all=AsyncMock(
                return_value=[{**share, "service_unit_key": b"svcA"}],
            ),
        ),
        data={
            "shares": [{**share, "service_unit_id": "svcA"}],
            "source_shares": [share],
            "services": [],
        },
        registered={("svcA", "alice"), ("svcB", "alice")},
        published={("svcA", "alice")},
        service_roots=roots,
        by_scope={},
    )
    await _merge_shares(context)
    assert json.loads((roots["svcB"] / "shares.json").read_text()) == []
    assert not (roots["svcB"] / share["snapshot_key"]).exists()
    context.database.fetch_all.return_value = []
    with pytest.raises(IntegrityError, match="missing"):
        await _merge_shares(context)
    context.database.fetch_all.return_value = [
        {**share, "service_unit_key": b"unknown-service"},
    ]
    with pytest.raises(IntegrityError, match="ownership"):
        await _merge_shares(context)
    context.database.fetch_all.return_value = [
        {**share, "service_unit_key": b"svcB"},
    ]
    with pytest.raises(IntegrityError, match="ownership"):
        await _merge_shares(context)


def test_published_frozen_permanent_share_cannot_disappear():
    from swe.app.conversation_storage.migration.tenant_rollback import (
        _select_share_records,
    )

    context = SimpleNamespace(
        data={
            "shares": [
                {
                    "token": "permanent",
                    "service_unit_id": "svc",
                    "tenant_id": "alice",
                },
            ],
        },
        published={("svc", "alice")},
        service_roots={"svc": object()},
    )
    with pytest.raises(IntegrityError, match="missing"):
        _select_share_records(context, [])
