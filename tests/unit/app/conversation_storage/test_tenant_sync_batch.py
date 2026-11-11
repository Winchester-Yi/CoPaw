# -*- coding: utf-8 -*-
from unittest.mock import Mock

from swe.app.conversation_storage.migration.discovery import (
    discover_services,
    freeze_discovery,
)
from swe.app.conversation_storage.migration.manifest import SourceBoundary
from swe.app.conversation_storage.migration.v3_manifest import V3Manifest
from tests.unit.app.conversation_storage.test_v3_discovery import (
    fixture_service,
)


def test_all_tenant_cutoffs_persist_once_and_resume_keeps_existing_values(
    tmp_path,
):
    frozen = freeze_discovery(
        discover_services([fixture_service(tmp_path)]),
        tmp_path / "manifest.json",
        SourceBoundary(
            "snapshot",
            "2026-11-02T00:00:00Z",
            "old@sha256:" + "a" * 64,
            ("stop", "readonly"),
        ),
        "business",
    )
    original = frozen.begin_tenant_sync("SERVICE", "default")
    persist = Mock(wraps=frozen._persist)
    frozen._persist = persist
    tenants = [("SERVICE", "tenant-" + str(index)) for index in range(1000)]
    tenants += [("SERVICE", "default"), ("SERVICE", "share-only")]
    result = frozen.begin_tenant_syncs(reversed(tenants))
    persist.assert_called_once_with()
    assert result["SERVICE\x00default"] == original
    assert (
        len(
            set(
                value
                for key, value in result.items()
                if key != "SERVICE\x00default"
            ),
        )
        == 1
    )
    assert (
        V3Manifest.load(frozen.path).data["tenant_sync_started_at_ns"]
        == result
    )
    persist.reset_mock()
    assert frozen.begin_tenant_syncs(tenants) == result
    persist.assert_not_called()
