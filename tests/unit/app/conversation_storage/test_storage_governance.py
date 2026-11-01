# -*- coding: utf-8 -*-
"""General workspace cleanup must never own immutable conversation bodies."""

import importlib

import pytest
from fastapi import HTTPException

from swe.app.conversation_storage.bundle import BundleBlock, BundleStore


@pytest.fixture
def committed_body(tmp_path, request):
    store = BundleStore(tmp_path)
    index = store.publish(
        "a" * 64,
        [BundleBlock("root", "recovery", b'{"unknown":true}')],
        metadata={"operation_key": "a" * 64, "kind": "root"},
    )
    if getattr(request, "param", "direct") == "alias":
        (tmp_path / "alias").symlink_to(
            tmp_path / "bundles",
            target_is_directory=True,
        )
        return tmp_path, index.relative_path.replace("bundles/", "alias/", 1)
    return tmp_path, index.relative_path


@pytest.mark.parametrize(
    "committed_body",
    ["direct", "alias"],
    indirect=True,
)
@pytest.mark.parametrize(
    ("module_name", "scanner"),
    [
        ("swe.app.file_governance.archive_maintenance", "scan_orphan_files"),
        ("swe.app.routers.dream_logs", "_scan_orphan_files"),
    ],
)
def test_conversation_bundle_is_not_a_workspace_orphan(
    committed_body,
    module_name,
    scanner,
):
    root, relative_path = committed_body
    module = importlib.import_module(module_name)
    assert relative_path not in {
        item.path for item in getattr(module, scanner)(root)
    }
    assert (root / relative_path).is_file()


@pytest.mark.parametrize(
    "committed_body",
    ["direct", "alias"],
    indirect=True,
)
@pytest.mark.parametrize(
    ("module_name", "resolver"),
    [
        (
            "swe.app.file_governance.archive_maintenance",
            "resolve_workspace_file",
        ),
        ("swe.app.routers.dream_logs", "_resolve_workspace_file"),
    ],
)
def test_workspace_cleanup_rejects_direct_conversation_bundle_path(
    committed_body,
    module_name,
    resolver,
):
    root, relative_path = committed_body
    module = importlib.import_module(module_name)
    with pytest.raises(HTTPException) as error:
        getattr(module, resolver)(root, relative_path)
    assert error.value.status_code == 403
    assert (root / relative_path).is_file()
