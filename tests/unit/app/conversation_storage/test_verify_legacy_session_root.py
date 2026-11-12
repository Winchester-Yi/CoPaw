# -*- coding: utf-8 -*-
"""Canonical export roots must be verified even when runtime state is intact."""

import json
from types import SimpleNamespace

import pytest

from swe.app.conversation_storage.bundle import BundleBlock, BundleStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.migration import verify
from swe.app.conversation_storage.migration.parallel import MigrationIO
from swe.app.conversation_storage.migration.runtime_prefetch import _prefetch


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "body",
        "kind",
        "missing",
        "scope",
        "scope_kind",
        "operation",
        "path",
        "semantic",
        "uncommitted",
        "reclaimed",
    ],
)
async def test_canonical_root_validates_full_root_and_identity(
    tmp_path,
    monkeypatch,
    cached,
    fault,
):
    scope = WorkspaceScope("t", "s", "a", service_unit_id="svc")
    store = BundleStore(tmp_path)
    operation = "ab" + "0" * 62
    root = {"state": {"module": {"value": 1}}, "unknown_top_level": [1, 2]}
    payload = json.dumps(root).encode()
    if fault == "body":
        payload = payload[:-1] + b"!"
    elif fault == "semantic":
        payload = json.dumps({**root, "unknown_top_level": [999]}).encode()
    name = "other" if fault == "missing" else "legacy_root"
    kind = "recovery" if fault == "kind" else "legacy_root"
    index = store.publish(
        operation,
        [
            BundleBlock("recovery", "recovery", b'{"ok":true}'),
            BundleBlock(name, kind, payload),
        ],
        scope=scope,
    )
    row = {
        **scope.fields,
        "scope_kind": "workspace",
        "id": 1,
        "operation_key": operation.encode(),
        "relative_path": index.relative_path.encode(),
        "byte_length": index.byte_length,
    }
    assignment = {
        "target_id": 1,
        "operation_key": operation,
        "relative_path": index.relative_path,
    }
    if fault == "scope":
        row["agent_key"] = b"other"
    elif fault == "scope_kind":
        row["scope_kind"] = "legacy_share"
    elif fault == "operation":
        assignment["operation_key"] = "cd" + "0" * 62
    elif fault == "path":
        assignment["relative_path"] += ".other"
    pins, releases = [], []

    class Retention:
        def __init__(self, *_args):
            pass

        async def pin(self, ids, **_kwargs):
            pins.extend(ids)
            return ids

        async def finish_reader(self, ids):
            releases.extend(ids)

    class Database:
        async def fetch_one(self, query, params):
            assert "state='committed' AND gc_state='live'" in query
            assert params == (1,)
            return None if fault in ("uncommitted", "reclaimed") else row

    class RuntimeAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_session_state_dict(self, *_args):
            return {"runtime": "intact", "revision": 0}

    monkeypatch.setattr(verify, "ReadOnlyRetention", Retention)
    monkeypatch.setattr(verify, "MySQLSessionAdapter", RuntimeAdapter)
    monkeypatch.setattr(verify, "read_only_adapter", lambda adapter: adapter)
    manifest = SimpleNamespace(
        data={
            "assignments": {
                "publication": {"session/recovery": assignment},
                "session": {"session": {"target_id": 1}},
            },
        },
    )
    async with MigrationIO(1) as io:
        bundles = (
            await _prefetch(store, [row], io, 65536)
            if cached and fault not in ("scope", "scope_kind")
            else store
        )
        call = verify._verify_session(
            Database(),
            bundles,
            manifest,
            {"scope": ["svc", "t", "s", "a"]},
            {
                "position": "session",
                "identity": [
                    "svc",
                    "t",
                    "s",
                    "a",
                    "user",
                    "console",
                    "session",
                ],
                "legacy": {"root": root, "state": {"runtime": "intact"}},
            },
            io=io if cached else None,
            messages_verified=True,
        )
        if fault:
            with pytest.raises((ValueError, KeyError)):
                await call
        else:
            await call
    assert releases == pins
    if fault not in (
        "scope",
        "scope_kind",
        "operation",
        "path",
        "uncommitted",
        "reclaimed",
    ):
        assert pins == [1]
