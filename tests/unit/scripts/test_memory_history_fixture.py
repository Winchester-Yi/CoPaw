# -*- coding: utf-8 -*-
"""Privacy and source-safety checks for historical rehearsal copies."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "scripts/memory_history_fixture.py"
)


def module():
    spec = importlib.util.spec_from_file_location(
        "history_fixture",
        MODULE_PATH,
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_sanitizer_masks_text_keys_and_preserves_bindings():
    sanitizer = module().Sanitizer(b"test salt")
    secret = "Owner alice@example.com 中文 é 🔑\n"
    original = {
        "chat_id": "fd1b3c74-7780-46da-9d15-3d932d5dc2b2",
        "turn_states": {
            "private-turn": {"id": "private-turn", "status": "completed"},
        },
        "message_id": "private-turn",
        "source_refs": ["message:private-turn"],
        "evidence_refs": ["message:private-turn"],
        "role": "user",
        "type": "message_added",
        "content": secret,
        "metadata": {
            "alice@example.com": {"password": secret, "account": 12345},
        },
        "occurred_at": "2026-10-01T10:20:30+08:00",
        "sequence": 17,
    }
    copied = sanitizer.clean(original)
    payload = json.dumps(copied, ensure_ascii=False)
    assert "alice@example.com" not in payload
    assert "password" not in payload
    assert "12345" not in payload
    assert len(copied["content"].encode()) == len(secret.encode())
    assert copied["content"].endswith("\n")
    assert copied["role"] == "user"
    assert copied["type"] == "message_added"
    assert copied["sequence"] == 17
    assert copied["occurred_at"] == "2000-01-01T00:00:00+00:00"
    assert str(UUID(copied["chat_id"])) == copied["chat_id"]
    turn = copied["message_id"]
    assert copied["turn_states"][turn]["id"] == turn
    assert (
        copied["source_refs"] == copied["evidence_refs"] == [f"message:{turn}"]
    )


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_prepare_cases_copies_legacy_and_reports_invalid_without_source_edits(
    tmp_path,
):
    source = tmp_path / "source"
    session = source / "alice/sessions/private.json"
    write_json(
        session,
        {
            "schema_version": 2,
            "agent": {"memory": {"content": []}},
            "turn_states": {"secret": {"status": "completed"}},
        },
    )
    journal = (
        source
        / "alice/dialog/fd1b3c74-7780-46da-9d15-3d932d5dc2b2/events.jsonl"
    )
    write_json(
        journal,
        {
            "id": "event-secret",
            "sequence": 1,
            "epoch": 1,
            "occurred_at": "2026-01-01T00:00:00Z",
            "type": "message_added",
            "facts": {"message_id": "secret", "role": "user"},
            "source_refs": ["message:secret"],
        },
    )
    write_json(source / "alice/sessions/new.json", {"schema_version": 3})
    (source / "alice/sessions/broken.json").write_text("{broken")
    write_json(source / ".storage-verification/sessions/ignored.json", {})
    write_json(source / "models/sessions/ignored.json", {})
    (source / "alice/sessions/link.json").symlink_to(session)
    before = {path: path.read_bytes() for path in (session, journal)}
    result = module().prepare_cases(source, tmp_path / "copies")
    assert len(result["cases"]) == 2
    assert result["inventory"]["invalid"] == 1
    assert result["inventory"]["unsupported"] == 1
    assert result["inventory"]["symlinks_skipped"] == 1
    assert result["inventory"]["sessions"]["count"] == 1
    assert result["inventory"]["journals"]["events"] == 1
    assert module().verify_sources(result["source_fingerprints"])
    assert all(
        path.read_bytes() == content for path, content in before.items()
    )
    assert str(source) not in json.dumps(result["inventory"])
    for case in result["cases"]:
        root = Path(case["root"])
        if case["kind"] == "session":
            assert case["session_id"] == "s"
            copied = json.loads((root / "sessions/s.json").read_text())
            assert copied["agent"]["memory"]["content"] == []
            assert case["turns"] == 1
        else:
            assert str(UUID(case["chat_id"])) == case["chat_id"]
            assert (
                root / "dialog" / case["chat_id"] / "events.jsonl"
            ).exists()
    session.write_text("changed")
    assert not module().verify_sources(result["source_fingerprints"])


@pytest.mark.parametrize("destination", ["source", "source/child", "existing"])
def test_prepare_rejects_unsafe_destinations(tmp_path, destination):
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "existing").mkdir()
    with pytest.raises(ValueError):
        module().prepare_cases(source, tmp_path / destination)


@pytest.mark.parametrize("invalid", ["duplicate", "event-id", "event-refs"])
def test_corrupt_data_is_excluded_without_normalization(tmp_path, invalid):
    source = tmp_path / "source"
    event = {
        "id": "event",
        "sequence": 1,
        "epoch": 1,
        "occurred_at": "2026-01-01T00:00:00Z",
        "type": "message_added",
        "facts": {},
        "source_refs": [],
    }
    if invalid == "duplicate":
        path = source / "sessions/s.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version":1,"schema_version":2}')
    else:
        event["id" if invalid == "event-id" else "source_refs"] = 123
        write_json(
            source
            / "dialog/11111111-1111-4111-8111-111111111111/events.jsonl",
            event,
        )
    result = module().prepare_cases(source, tmp_path / "copies")
    assert result["inventory"]["invalid"] == 1
    assert result["cases"] == []


def test_reference_prefixes_and_composite_archive_bindings():
    sanitizer = module().Sanitizer(b"test salt")
    batch = "11111111-1111-4111-8111-111111111111"
    message = "private:message"
    original = {
        "message_id": message,
        "source_refs": [
            "private-owner:private-suffix",
            f"message:{message}",
            "boundary:private-suffix",
            f"archive:{batch}:7:private-message",
        ],
        "evidence_refs": ["private-owner:private-suffix"],
        "artifact_ref": "private-owner:private-suffix",
        "artifact_refs": ["boundary:private-suffix"],
    }
    copied = sanitizer.clean(original)
    unknown, message_ref, boundary, archive = copied["source_refs"]
    assert unknown == copied["evidence_refs"][0] == copied["artifact_ref"]
    assert unknown.count(":") == 1
    assert "private-owner" not in unknown
    assert message_ref == f"message:{copied['message_id']}"
    assert boundary == copied["artifact_refs"][0]
    assert boundary == "boundary:" + sanitizer.identifier("private-suffix")
    assert archive.split(":") == [
        "archive",
        sanitizer.identifier(batch),
        "7",
        sanitizer.identifier("private-message"),
    ]
    assert "private" not in json.dumps(copied)


def test_numeric_privacy_masks_arbitrary_nested_values_and_keeps_protocol_counts():
    sanitizer = module().Sanitizer(b"test salt")
    account = 6225881234567890
    original = {
        "schema_version": 2,
        "revision": 31,
        "memory": {
            "account_number": account,
            "custom_tool": {"balance": 1234.56, "enabled": True},
            "messages": [
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "input": {
                                "sequence": account,
                                "parameters": [account, 1234.56],
                            },
                        },
                    ],
                    "metadata": {"token_count": account},
                },
            ],
        },
        "turn_states": {
            "turn-secret": {
                "admission_order": 8,
                "private_numeric": account,
            },
        },
        "facts": {
            "epoch": 3,
            "sequence": 9,
            "token_count": 114,
            "exit_code": 1,
            "private_numeric": account,
        },
        "metadata": 1234.56,
    }
    copied = sanitizer.clean(original)
    assert copied["schema_version"] == 2
    assert copied["revision"] == 31
    assert copied["facts"]["epoch"] == 3
    assert copied["facts"]["sequence"] == 9
    assert copied["facts"]["token_count"] == 114
    assert copied["facts"]["exit_code"] == 1
    assert next(iter(copied["turn_states"].values()))["admission_order"] == 8
    assert copied["memory"][sanitizer.identifier("account_number")] == 0
    tool = copied["memory"][sanitizer.identifier("custom_tool")]
    assert tool[sanitizer.identifier("balance")] == 0.0
    assert type(tool[sanitizer.identifier("balance")]) is float
    assert tool["enabled"] is True
    message = copied["memory"]["messages"][0]
    assert message["content"][0]["input"]["sequence"] == 0
    assert message["metadata"]["token_count"] == 0
    assert copied["metadata"] == 0.0
    assert type(copied["metadata"]) is float
    payload = json.dumps(copied)
    assert str(account) not in payload
    assert "1234.56" not in payload


@pytest.mark.parametrize(
    "folder",
    [
        "chat",
        "FD1B3C74-7780-46DA-9D15-3D932D5DC2B2",
        "11111111111141118111111111111111",
    ],
)
def test_noncanonical_chat_folders_are_rejected_without_repair(
    tmp_path,
    folder,
):
    source = tmp_path / "source"
    write_json(
        source / "dialog" / folder / "events.jsonl",
        {
            "id": "event",
            "sequence": 1,
            "epoch": 1,
            "occurred_at": "2026-01-01T00:00:00Z",
            "type": "message_added",
            "facts": {},
            "source_refs": ["message:one"],
        },
    )
    result = module().prepare_cases(source, tmp_path / "copies")
    assert result["inventory"]["invalid"] == 1
    assert result["inventory"]["journals"]["count"] == 0
    assert result["cases"] == []


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-01T00:00:00 alice@example.com",
        "2026-01-01T00:00:00Z email=alice@example.com",
        "2026-13-99T00:00:00Z",
    ],
)
def test_invalid_timestamp_masks_entire_value_including_private_suffix(
    timestamp,
):
    sanitizer = module().Sanitizer(b"test salt")
    copied = sanitizer.clean({"occurred_at": timestamp})["occurred_at"]
    assert copied == sanitizer.mask(timestamp)
    assert "alice@example.com" not in copied


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-01T12:34:56Z",
        "2026-01-01T12:34:56.123456+08:30",
        "2026-01-01 12:34:56.123-05:00",
        "2026-01-01T12:34:56",
    ],
)
def test_valid_timestamp_normalization_preserves_shape(timestamp):
    copied = (
        module().Sanitizer().clean({"occurred_at": timestamp})["occurred_at"]
    )
    assert datetime.fromisoformat(copied).year == 2000
    assert len(copied) == len(timestamp)
    assert copied[:10] == "2000-01-01"
