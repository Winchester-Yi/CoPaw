# -*- coding: utf-8 -*-
import json
from types import SimpleNamespace

import pytest

from swe.app.conversation_storage.runtime import (
    ConversationRuntime,
    resolved_scope,
)
from swe.app.conversation_storage.schema_contract import COLUMNS, INDEXES
from swe.app.conversation_storage import runtime


def manifest(root, service="service-a"):
    return {
        "version": 2,
        "service_unit_id": service,
        "nas_root": str(root),
        "workspaces": [{"root": str(root), "scope": [service, "t", "s", "a"]}],
    }


def test_runtime_rejects_manifest_service_before_adapters(tmp_path):
    with pytest.raises(ValueError, match="service"):
        ConversationRuntime(
            None,
            manifest(tmp_path),
            b"secret",
            service_unit_id="service-b",
        )


def test_runtime_resolves_scope_from_frozen_service_context():
    workspace = SimpleNamespace(tenant_id="t", source_id="s", agent_id="a")
    scope = resolved_scope(workspace, service_unit_id="service-a")
    assert scope.values == ("service-a", "t", "s", "a")
    workspace.conversation_scope = scope
    with pytest.raises(ValueError, match="service"):
        resolved_scope(workspace, service_unit_id="service-b")


def test_runtime_rejects_old_manifest_without_inferred_service(tmp_path):
    document = manifest(tmp_path)
    document["version"] = 1
    with pytest.raises(ValueError, match="manifest"):
        ConversationRuntime(
            None,
            document,
            b"secret",
            service_unit_id="service-a",
        )


async def test_configured_service_remains_bound_after_environment_change(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest(tmp_path)))
    monkeypatch.setenv(runtime.AUTHORITY_ENV, "mysql")
    monkeypatch.setenv(runtime.MAPPING_ENV, str(path))
    monkeypatch.setenv(runtime.CURSOR_ENV, "secret")
    monkeypatch.delenv("CMB_CAAS_SERVICEUNITID", raising=False)
    monkeypatch.setattr(runtime, "_runtime", None)
    database = SimpleNamespace(is_connected=True)
    with pytest.raises(ValueError, match="CMB_CAAS_SERVICEUNITID"):
        await runtime.configure_runtime(database)

    async def verified(_self):
        return None

    monkeypatch.setattr(ConversationRuntime, "verify", verified)
    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "service-a")
    await runtime.configure_runtime(database)
    shares = runtime.runtime_shares()
    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "service-b")
    assert runtime.runtime_shares() is shares
    assert shares.service_unit_id == "service-a"


def test_schema_contract_matches_service_target():
    assert all(
        fields["service_unit_key"] == ("varbinary(256)", "NO")
        for fields in COLUMNS.values()
    )
    assert INDEXES["swe_conversation_publications"][
        "uq_conv_publication_operation"
    ] == (0, ("service_unit_key", "operation_key"))
