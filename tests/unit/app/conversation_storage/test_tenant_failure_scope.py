# -*- coding: utf-8 -*-
import errno
import json
from pathlib import Path

import pytest
from mysql.connector.errors import OperationalError

from swe.app.conversation_storage.migration.failure_scope import (
    strict_tenant_io,
)
from swe.app.conversation_storage.migration.inventory import _block
from swe.app.conversation_storage.migration.verify import _checked


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.EIO, "disk"),
        PermissionError("private"),
        OperationalError("sql private"),
    ],
)
def test_inventory_shared_errors_propagate_in_isolated_scope(error):
    with strict_tenant_io(), pytest.raises(type(error)):
        _block({"blockers": []}, "invalid", "workspace", error)


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.EIO, "disk"),
        PermissionError("private"),
        OperationalError("sql private"),
    ],
)
async def test_verification_shared_errors_propagate_in_isolated_scope(error):
    async def failing():
        raise error

    with strict_tenant_io(), pytest.raises(type(error)):
        await _checked({"blockers": [], "checked": []}, "scope", failing())


async def test_legacy_verification_and_missing_input_keep_blocker_contract():
    async def failing(error):
        raise error

    report = {"blockers": [], "checked": []}
    await _checked(report, "legacy", failing(OperationalError("sql")))
    with strict_tenant_io():
        await _checked(
            report,
            "missing",
            failing(FileNotFoundError("missing")),
        )
    assert len(report["blockers"]) == 2


def test_public_failure_report_does_not_expose_internal_details(tmp_path):
    from swe.cli.conversation_storage_discovery_cmd import (
        _write_failed_tenants,
    )

    path = _write_failed_tenants(
        tmp_path / "discovery.json",
        {
            "tenants": [
                {
                    "service_unit_id": "svc",
                    "tenant_id": "account",
                    "state": "failed",
                    "blockers": [
                        {
                            "code": "session_invalid",
                            "position": "/private/token",
                            "detail": "password=secret",
                        },
                    ],
                },
            ],
        },
    )
    raw = Path(path).read_text()
    assert "private" not in raw and "secret" not in raw
    assert set(json.loads(raw)[0]) == {
        "service_unit_id",
        "tenant_id",
        "stage",
        "code",
        "reason",
    }


def test_registry_reader_eio_is_not_reported_as_tenant_data_error(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.migration.inventory_v3 import (
        inventory_discovery,
    )

    registry = tmp_path / "chats.json"
    registry.write_text('{"chats": []}')
    original = Path.read_bytes

    def read(path):
        if path == registry:
            raise OSError(errno.EIO, "shared storage disconnected")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    data = {
        "tenant_isolation": True,
        "fingerprints": [],
        "services": [],
        "shares": [],
        "workspaces": [
            {
                "service_unit_id": "svc",
                "tenant_id": "tenant",
                "source_id": "source",
                "agent_id": "agent",
                "root": str(tmp_path),
                "position": "workspace",
            },
        ],
    }
    with pytest.raises(OSError, match="shared storage disconnected"):
        inventory_discovery(data)


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.EIO, "shared scandir"),
        PermissionError(errno.EACCES, "shared scandir"),
    ],
)
def test_discovery_config_scandir_failure_is_fatal_before_tenants_can_disappear(
    tmp_path,
    monkeypatch,
    error,
):
    from swe.app.conversation_storage.migration import discovery
    import os

    source = tmp_path / "source"
    source.mkdir()
    real_scandir = os.scandir

    def scandir(path):
        if Path(path) == source:
            raise error
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    service = {
        "service_unit_id": "svc",
        "source_root": str(source),
        "config_root": str(source),
        "original_root": str(tmp_path / "online"),
        "nas_root": str(tmp_path / "nas"),
    }
    (tmp_path / "nas").mkdir()
    with pytest.raises(type(error), match="shared scandir"):
        discovery.discover_services([service], tenant_isolation=True)


@pytest.mark.parametrize("phase", ["coverage", "freshness"])
def test_walk_onerror_is_not_silently_treated_as_empty_source(
    tmp_path,
    monkeypatch,
    phase,
):
    from swe.app.conversation_storage.migration import discovery, freshness
    import os

    def walk(root, *, onerror=None, **kwargs):
        assert onerror is not None
        onerror(OSError(errno.EIO, "scan disconnected"))
        return iter(())

    monkeypatch.setattr(os, "walk", walk)
    with strict_tenant_io(), pytest.raises(OSError, match="scan disconnected"):
        if phase == "coverage":
            discovery._coverage(
                {"source_root": str(tmp_path)},
                set(),
                {"blockers": []},
                [],
            )
        else:
            freshness.changed_source([tmp_path], 2**63)


def test_old_walk_error_contract_is_preserved_outside_partial_mode():
    from swe.app.conversation_storage.migration.failure_scope import (
        source_walk_error,
    )

    source_walk_error(PermissionError(errno.EACCES, "legacy ignored walk"))


def test_tenant_config_second_layer_permission_failure_propagates(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.migration.tenant_discovery import (
        _configs,
    )
    import os

    account = tmp_path / "tenant"
    account.mkdir()
    real_lstat = Path.lstat

    def lstat(path):
        if path == account / "config.json":
            raise PermissionError(
                errno.EACCES,
                "tenant config stat unavailable",
            )
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with (
        strict_tenant_io(),
        pytest.raises(PermissionError, match="config stat unavailable"),
    ):
        list(_configs({"config_root": str(tmp_path)}))


def test_legacy_freshness_also_rejects_incomplete_walk(tmp_path, monkeypatch):
    from swe.app.conversation_storage.migration.freshness import changed_source
    import os

    def walk(root, *, onerror=None, **kwargs):
        onerror(PermissionError(errno.EACCES, "incomplete freshness"))
        return iter(())

    monkeypatch.setattr(os, "walk", walk)
    with pytest.raises(PermissionError, match="incomplete freshness"):
        changed_source([tmp_path], 2**63)


def test_config_symlink_directory_is_rejected_before_external_config_read(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.migration.failure_scope import (
        config_candidates,
    )
    from swe.app.conversation_storage.migration.source_inputs import (
        SourceInputInvalid,
    )

    root = tmp_path / "config"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "config.json").write_text('{"private": true}')
    (root / "tenant-link").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda *_: pytest.fail("must not read external config"),
    )
    with (
        strict_tenant_io(),
        pytest.raises(SourceInputInvalid, match="symlink"),
    ):
        config_candidates(root)
