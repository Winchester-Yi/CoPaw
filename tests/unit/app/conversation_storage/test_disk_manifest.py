# -*- coding: utf-8 -*-
"""Large progress mirrors retain their JSON contract without whole-file reads."""

import json

import pytest

from swe.app.conversation_storage.migration.disk_manifest import (
    DiskMap,
    iter_json,
    load_disk_manifest,
)


def test_manifest_mirrors_round_trip_on_disk(tmp_path):
    path = tmp_path / "manifest.json"
    value = {
        "workspaces": [{"root": "路径"}],
        "assignments": {
            "session": {str(i): {"target_id": i} for i in range(12000)},
        },
        "batches": {"large-value": "x" * 100000, "a": "committed"},
    }
    path.write_text(json.dumps(value))
    actual = load_disk_manifest(path)
    assert isinstance(actual["assignments"]["session"], DiskMap)
    assert isinstance(actual["batches"], DiskMap)
    assert actual == value
    assert json.loads("".join(iter_json(actual))) == value
    assert len(list(actual["assignments"]["session"])) == 12000


@pytest.mark.parametrize(
    "source",
    [
        '{"batches":{"x":1,"x":2}}',
        '{"assignments":{"session":{},"session":{}}}',
        '{"batches":{}} trailing',
        '{"batches":{"x":1,}}',
        '{"batches":{"x":',
    ],
)
def test_streaming_manifest_rejects_invalid_json(tmp_path, source):
    path = tmp_path / "manifest.json"
    path.write_text(source)
    with pytest.raises(ValueError):
        load_disk_manifest(path)


def test_disk_mapping_updates_delete_and_json_default():
    values = DiskMap()
    values["a"] = {"state": "first"}
    values["a"] = {"state": "second"}
    assert values["a"]["state"] == "second"
    del values["a"]
    assert not values
    with pytest.raises(KeyError):
        del values["missing"]
    assert json.loads("".join(iter_json({"value": object()}, default=str)))[
        "value"
    ]


def test_disk_policy_enforces_shared_budget_and_closes(tmp_path):
    from swe.app.conversation_storage.migration.disk_manifest import (
        disk_index_policy,
    )
    from swe.app.conversation_storage.migration.spool import (
        MigrationCapacityError,
    )

    with disk_index_policy(tmp_path.resolve(), 65536) as policy:
        first = DiskMap()
        second = DiskMap()
        first["data"] = "a" * 10000
        assert policy.used_bytes() <= 65536
        assert first._directory.name.startswith(str(tmp_path))
        with pytest.raises(MigrationCapacityError):
            second["too-large"] = "b" * 100000
    assert first._db is None
    assert second._db is None
    assert list(tmp_path.iterdir()) == []


def test_header_discards_progress_regardless_of_field_order(tmp_path):
    from swe.app.conversation_storage.migration.disk_manifest import (
        read_manifest_header,
    )

    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "assignments": {
                    "session": {
                        str(i): {"target_id": i} for i in range(10000)
                    },
                },
                "batches": {"a": "committed"},
                "workflow": "direct",
                "version": 3,
            },
        ),
    )
    assert read_manifest_header(path) == {"workflow": "direct", "version": 3}


def test_manifest_reader_rejects_oversized_value_before_decoding(tmp_path):
    from swe.app.conversation_storage.migration.spool import (
        MigrationCapacityError,
    )

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"workspaces": ["x" * 100000]}))
    with pytest.raises(MigrationCapacityError, match="reader memory limit"):
        load_disk_manifest(path, max_value_bytes=65536)
    assert load_disk_manifest(path, max_value_bytes=200000)["workspaces"]


def test_header_scan_has_bounded_indexes_and_closes_them(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.migration import disk_manifest

    indexes = []
    original = DiskMap.__init__

    def track(mapping):
        original(mapping)
        assert mapping._policy.max_bytes == 64 * 1024 * 1024
        indexes.append(mapping)

    monkeypatch.setattr(DiskMap, "__init__", track)
    path = tmp_path / "manifest.json"
    path.write_text('{"version":3,"batches":{"sample":"committed"}}')
    assert disk_manifest.read_manifest_header(path) == {"version": 3}
    assert indexes and all(mapping._db is None for mapping in indexes)
