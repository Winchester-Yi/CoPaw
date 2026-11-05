# -*- coding: utf-8 -*-
"""Atomicity of conversation batches on an explicitly isolated MySQL schema."""

from types import SimpleNamespace

import pytest

from swe.app.conversation_storage.bundle import BlockExtent, BundleIndex
from swe.app.conversation_storage.message_installs import (
    MESSAGE_INSERT,
    MessageInstall,
    install_messages,
)
from swe.app.conversation_storage.retention import MEMBER_INSERT
from swe.database import prepared
from tests.integrated.conversation_storage.test_retention_mysql import (
    retention,
)
from tests.integrated.conversation_storage.test_schema import (
    insert_chat,
    insert_publication,
    insert_session,
)


def fail_chunk(monkeypatch, template, failure_at):
    execute = prepared._execute_chunk
    calls = []

    async def failing(session, statement, count, params):
        if statement is template:
            calls.append(count)
            if len(calls) == failure_at:
                raise RuntimeError("injected batch failure")
        return await execute(session, statement, count, params)

    monkeypatch.setattr(prepared, "_execute_chunk", failing)
    return calls


@pytest.mark.parametrize("failure_at", [1, 2, 3])
async def test_pin_batch_failure_rolls_back_every_member(
    conversation_test_database,
    monkeypatch,
    failure_at,
):
    db = conversation_test_database
    ids = [
        await insert_publication(db, str(i).encode(), state="committed")
        for i in range(201)
    ]
    calls = fail_chunk(monkeypatch, MEMBER_INSERT, failure_at)
    with pytest.raises(RuntimeError, match="injected"):
        await retention(db).pin(ids, kind="reader")
    assert len(calls) == failure_at
    assert (
        await db.fetch_all("SELECT id FROM swe_conversation_retentions") == []
    )


@pytest.mark.parametrize("failure_at", [1, 2, 3])
async def test_message_batch_failure_rolls_back_rows_and_high_watermarks(
    conversation_test_database,
    monkeypatch,
    failure_at,
):
    db = conversation_test_database
    session = await insert_session(db, history_high=9)
    chat = await insert_chat(db, session)
    publication = await insert_publication(db)
    request = SimpleNamespace(
        scope=retention(db).scope,
        session_id=session,
        chat_id=chat,
        turn_id=None,
    )
    messages = [MessageInstall(str(i), "memory", "user") for i in range(201)]
    index = BundleIndex(
        "bundle",
        10000,
        {},
        {str(i): BlockExtent("message", 50 + i, 1) for i in range(201)},
    )
    calls = fail_chunk(monkeypatch, MESSAGE_INSERT, failure_at)
    with pytest.raises(RuntimeError, match="injected"):
        async with db.transaction():
            await install_messages(
                db,
                request,
                messages,
                {"id": publication},
                index,
                ({"history_high": 9}, {"history_high": 0}, None),
            )
    assert len(calls) == failure_at
    assert await db.fetch_all("SELECT id FROM swe_conversation_messages") == []
    current = await db.fetch_one(
        "SELECT history_high FROM swe_conversation_sessions WHERE id=%s",
        (session,),
    )
    assert current["history_high"] == 9
    current = await db.fetch_one(
        "SELECT history_high,history_revision FROM swe_conversation_chats WHERE id=%s",
        (chat,),
    )
    assert current == {"history_high": 0, "history_revision": 0}
