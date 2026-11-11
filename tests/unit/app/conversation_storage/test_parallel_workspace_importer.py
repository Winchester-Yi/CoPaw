# -*- coding: utf-8 -*-
"""Compare real domain batch generation through both publication paths."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest
from agentscope.message import Msg

from swe.app.conversation_storage.chat_mutations import load_metadata_json
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.migration.importer import (
    ConversationImporter,
    ParallelWorkspaceImporter,
)
from swe.app.conversation_storage.migration.parallel_import import _freeze

STAMP = "2026-11-01T00:00:00Z"


def workspace():
    def message(identity, role, content):
        value = Msg(role, content, role, timestamp=STAMP).to_dict()
        value["id"] = identity
        return value

    question = message("question", "user", "question")
    answer = message("answer", "assistant", "answer")
    archived = message("old-question", "user", "old")
    state = {
        "revision": 4,
        "context_epoch": 2,
        "agent": {"memory": {"content": [question, answer]}},
        "turn_states": {
            "question": {
                "chat_id": "chat",
                "status": "completed",
                "message": question,
                "admission_order": 0,
            },
        },
    }
    batch = {
        "boundary": {
            "id": "boundary",
            "archived_message_count": 1,
            "first_message_id": "old-question",
            "last_message_id": "old-question",
            "first_timestamp": STAMP,
            "last_timestamp": STAMP,
            "created_at": STAMP,
        },
        "messages": [archived],
    }
    epochs = [
        {
            "epoch": epoch,
            "record": {"revision": epoch, "applied_event_sequence": 1},
            "events": [{"epoch": epoch, "sequence": 1, "kind": "fixture"}],
            "batches": [batch],
            "evidence": {},
        }
        for epoch in (2, 1)
    ]
    return {
        "scope": ["svc", "tenant", "source", "agent"],
        "position": "workspace",
        "sessions": [
            {
                "position": "session",
                "identity": [
                    "svc",
                    "tenant",
                    "source",
                    "agent",
                    "user",
                    "web",
                    "session",
                ],
                "chat_positions": [0],
                "legacy": {"state": state, "root": deepcopy(state)},
            },
        ],
        "chats": [
            {
                "position": "chat",
                "registry_order": 1,
                "spec": {
                    "id": "chat",
                    "name": "A chat",
                    "session_id": "session",
                    "user_id": "user",
                    "channel": "web",
                    "created_at": STAMP,
                    "updated_at": STAMP,
                    "meta": {
                        "prompt": "heavy content",
                        "plan_mode_enabled": True,
                    },
                },
                "archive": {"deleted": False, "epochs": epochs},
            },
        ],
    }


class Recorder:
    def __init__(self):
        self.assignments, self.allocations, self.rows = {}, [], {}
        self.publications, self.blocks, self.finished = [], {}, []
        self.chat_sessions = {}
        self.verifications, self.commit_verifications, self.writes = [], [], []
        self.manifest_data = {"source_boundary": {"captured_at": STAMP}}

    def allocate(self, kind, position, **_kwargs):
        self.allocations.append((kind, position))
        key = kind, position
        if key not in self.assignments:
            self.assignments[key] = {
                "target_id": len(self.assignments) + 1,
                "operation_key": sha256(
                    (kind + position).encode(),
                ).hexdigest(),
                "relative_path": "unused",
            }
        return self.assignments[key]

    async def reserve_batch(self, request, publication_id, plan, **_kwargs):
        self.publications.append((request, publication_id, plan.serialize()))
        row = {
            "id": publication_id,
            "state": "prepared",
            "operation_key": request.operation_key,
        }
        return self.rows.setdefault(request.operation_key, row)

    async def write_bundle(self, operation, blocks):
        self.writes.append(operation)
        self.blocks[operation] = tuple(blocks)
        imported = load_metadata_json(blocks[0].content)
        for item in imported["rows"]:
            if item["table"] == "chats":
                self.chat_sessions[item["values"]["id"]] = item["values"][
                    "session_id"
                ]

    async def get(self, operation):
        return self.rows[operation]

    async def verify_blocks(self, row, blocks):
        assert tuple(blocks) == self.blocks[row["operation_key"]]

    async def commit(self, operation, *, verification=None):
        self.commit_verifications.append(verification)
        self.rows[operation]["state"] = "committed"
        return self.rows[operation]

    async def verify_batch(
        self,
        request,
        publication_id,
        plan,
        expected,
        blocks=None,
    ):
        self.verifications.append(
            (request, publication_id, plan, expected, blocks),
        )
        original = self.blocks[request.operation_key]
        assert load_metadata_json(original[0].content) == expected
        if blocks is not None:
            assert tuple(blocks) == original
        return self.rows[request.operation_key]

    async def journal(self, name, *args, **kwargs):
        if name == "allocate":
            return self.allocate(*args, **kwargs)
        if name == "refresh_batch":
            self.finished.append(args[0])

    async def store(self, name, *args, **kwargs):
        if name == "chat_session_id":
            return self.chat_sessions[args[0]]
        return await getattr(self, name)(*args, **kwargs)

    async def bundle_exists(self, _row):
        return False

    async def fetch_one(self, _query, params):
        return {"session_id": self.chat_sessions[params[0]]}


async def serial_run(record, source, target, tmp_path):
    importer = ConversationImporter.__new__(ConversationImporter)
    importer.target, importer.batch_size = target, 1
    importer.db = record
    importer.bundle_stores = {"svc": SimpleNamespace(root=tmp_path)}
    importer.stores = {"svc": record}
    importer.session_ids, importer.chat_ids, importer.turn_ids = {}, {}, {}
    importer.manifest = SimpleNamespace(
        migration_id=target.migration_id,
        data=record.manifest_data,
        allocate=record.allocate,
        set_batch_state=lambda *_args: None,
    )

    async def finished(position):
        record.finished.append(position)

    importer._batch_finished = finished
    scope = WorkspaceScope.from_values(source["scope"])
    await importer._identities(source, scope)
    for session in source["sessions"]:
        await importer._session(session, source, scope)
    for chat in source["chats"]:
        await importer._archive(chat, scope)
    return importer


@pytest.mark.parametrize(
    "frozen",
    [False, True],
    ids=["plain", "router-frozen"],
)
async def test_workspace_domain_publications_match_serial(tmp_path, frozen):
    source = workspace()
    original = deepcopy(source)
    target = SimpleNamespace(
        schema="test_conversation_fixture",
        migration_id="migration",
    )
    serial, parallel = Recorder(), Recorder()
    expected = await serial_run(serial, deepcopy(source), target, tmp_path)
    if frozen:
        parallel.manifest_data = _freeze(parallel.manifest_data)
    importer = ParallelWorkspaceImporter(parallel, target, batch_size=1)
    await importer.run_workspace(_freeze(source) if frozen else source)
    assert parallel.allocations == serial.allocations
    assert parallel.publications == serial.publications
    assert parallel.blocks == serial.blocks
    assert parallel.finished == serial.finished
    assert importer.session_ids == expected.session_ids
    assert importer.chat_ids == expected.chat_ids
    assert importer.turn_ids == expected.turn_ids
    assert source == original
    positions = [
        position
        for kind, position in parallel.allocations
        if kind == "publication"
    ]
    assert any("/turn-record:" in position for position in positions)
    assert any("/boundary:" in position for position in positions)
    assert positions.index("chat/epoch:1") < positions.index("chat/epoch:2")
    kinds = {
        block.kind for blocks in parallel.blocks.values() for block in blocks
    }
    memory_ids = [
        load_metadata_json(block.content)["id"]
        for blocks in parallel.blocks.values()
        for block in blocks
        if block.kind == "memory"
    ]
    assert memory_ids == ["question", "answer"]
    imported = [
        load_metadata_json(blocks[0].content)
        for blocks in parallel.blocks.values()
    ]
    turns = [
        row["values"]
        for plan in imported
        for row in plan["rows"]
        if row["table"] == "turns"
    ]
    assert len(turns) == 1
    assert turns[0]["state"] == "succeeded"
    assert turns[0]["history_status"] == "completed"
    assert {
        "recovery",
        "archive",
        "checkpoint",
        "events",
        "content_meta",
    } <= kinds


@pytest.mark.parametrize("state", ["prepared", "committed"])
async def test_workspace_retry_preserves_original_blocks(tmp_path, state):
    source = workspace()
    target = SimpleNamespace(
        schema="test_conversation_fixture",
        migration_id="migration",
    )
    record = Recorder()
    await ParallelWorkspaceImporter(
        record,
        target,
        batch_size=1,
    ).run_workspace(_freeze(source))
    originals = dict(record.blocks)
    written = len(record.writes)
    publications = tuple(record.publications)
    record.commit_verifications.clear()
    for row in record.rows.values():
        row.update(state=state, durable_at=STAMP)
    await ParallelWorkspaceImporter(
        record,
        target,
        batch_size=1,
    ).run_workspace(_freeze(source))
    assert record.blocks == originals
    assert len(record.writes) == written
    assert tuple(record.publications[len(publications) :]) == publications
    if state == "committed":
        assert len(record.verifications) == len(publications)
        assert not record.commit_verifications
        verifications = record.verifications
    else:
        assert not record.verifications
        assert len(record.commit_verifications) == len(publications)
        verifications = record.commit_verifications
    for request, publication_id, plan, expected, blocks in verifications:
        assert publication_id == record.rows[request.operation_key]["id"]
        assert tuple(blocks) == originals[request.operation_key]
        assert load_metadata_json(blocks[0].content) == expected
        assert (request, publication_id, plan.serialize()) in publications
