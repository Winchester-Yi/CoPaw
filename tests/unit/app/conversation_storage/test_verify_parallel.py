# -*- coding: utf-8 -*-
"""Parallel message evidence keeps SQL on the owner and NAS work bounded."""

import asyncio
import threading
from types import SimpleNamespace

from swe.app.conversation_storage.migration import verify
from swe.app.conversation_storage.migration.parallel import MigrationIO
from swe.app.conversation_storage.migration.verify_parallel import (
    verify_workspace_messages,
)


async def test_workspace_message_checks_overlap_without_worker_database(
    monkeypatch,
):
    owner = asyncio.current_task()
    barrier = threading.Barrier(2)
    checked = []

    class Database:
        async def fetch_all(self, _query, _params):
            assert asyncio.current_task() is owner
            return []

    async def session_check(
        evidence,
        _bundles,
        _workspace,
        session,
        sid,
        *,
        io,
        io_workers,
    ):
        assert asyncio.current_task() is not owner
        assert not hasattr(evidence, "transaction")
        assert (
            await evidence.fetch_all(
                "SELECT * FROM swe_conversation_messages WHERE session_id=%s "
                "AND layer<>%s ORDER BY session_order",
                (sid, "archive"),
            )
            == []
        )
        await io.call(barrier.wait, timeout=5)
        checked.append(session["position"])

    monkeypatch.setattr(verify, "_verify_session_messages", session_check)
    manifest = SimpleNamespace(
        data={
            "assignments": {
                "session": {
                    "session-0": {"target_id": 1},
                    "session-1": {"target_id": 2},
                },
            },
        },
    )
    inventory = {
        "workspaces": [
            {
                "scope": ["SERVICE", "tenant", "source", str(index)],
                "sessions": [{"position": "session-" + str(index)}],
                "chats": [],
            }
            for index in range(2)
        ],
    }
    report = {"blockers": []}
    async with MigrationIO(2) as io:
        await verify_workspace_messages(
            Database(),
            {"SERVICE": object()},
            manifest,
            inventory,
            report,
            io=io,
            workers=2,
        )
    assert sorted(checked) == ["session-0", "session-1"]
    assert report["blockers"] == []


async def test_sql_evidence_budget_rejects_before_unbounded_rows():
    import pytest
    from swe.app.conversation_storage.migration.verify_parallel import (
        collect_message_evidence,
    )

    class Database:
        async def fetch_all(self, query, params):
            assert query.endswith("LIMIT %s")
            assert params[-1] == 2
            return [{"publication_id": 1}, {"publication_id": 2}]

    manifest = SimpleNamespace(
        data={"assignments": {"session": {"s": {"target_id": 1}}}},
    )
    with pytest.raises(ValueError, match="SQL evidence exceeds"):
        await collect_message_evidence(
            Database(),
            manifest,
            {"sessions": [{"position": "s"}], "chats": []},
            memory_bytes=16384,
        )


async def test_auxiliary_epoch_reads_overlap_and_failure_is_reported(
    monkeypatch,
):
    from swe.app.conversation_storage.migration.verify_parallel import (
        verify_auxiliary_bodies,
    )

    owner = asyncio.current_task()
    barrier = threading.Barrier(2)

    class Database:
        async def fetch_one(self, _query, params):
            assert asyncio.current_task() is owner
            return {"id": params[0]}

    async def epoch_check(evidence, _bundles, _manifest, chat, _scope, *, io):
        assert asyncio.current_task() is not owner
        assert await evidence.fetch_one("detached", (1,)) == {"id": 1}
        await io.call(barrier.wait, timeout=5)
        if chat["position"] == "chat-1":
            raise ValueError("bad epoch")

    monkeypatch.setattr(verify, "_verify_epochs", epoch_check)
    manifest = SimpleNamespace(
        data={
            "assignments": {
                "publication": {
                    "chat-0/epoch:0": {"target_id": 1},
                    "chat-1/epoch:0": {"target_id": 1},
                },
            },
        },
    )
    inventory = {
        "workspaces": [
            {
                "scope": ["SERVICE", "tenant", "source", "agent"],
                "chats": [
                    {
                        "position": "chat-" + str(index),
                        "archive": {"epochs": [{"epoch": 0}]},
                    }
                    for index in range(2)
                ],
            },
        ],
        "files": [],
        "shares": [],
    }
    report = {"checked": [], "blockers": []}
    async with MigrationIO(2) as io:
        await verify_auxiliary_bodies(
            Database(),
            {"SERVICE": object()},
            manifest,
            inventory,
            report,
            io=io,
            workers=2,
        )
    assert report["blockers"] == [
        {"position": "chat-1", "detail": "bad epoch"},
    ]
