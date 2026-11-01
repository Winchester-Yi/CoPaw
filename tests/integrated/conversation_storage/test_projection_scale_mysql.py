# -*- coding: utf-8 -*-
"""Exercise full SQL publication/hydration across a lowered per-batch bound."""

from swe.app.conversation_storage import retention
from swe.app.conversation_storage.session_adapter import MySQLSessionAdapter
from tests.integrated.conversation_storage.test_archive_runtime_mysql import (
    setup,
)


async def test_task_history_commit_and_fresh_adapter_span_pin_batches(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    _, _, _, store = await setup(db, tmp_path)
    monkeypatch.setattr(retention, "MAX_PIN_BATCH", 2)
    state = {"task_messages": [], "future": {"keep": True}}
    for index in range(4):
        adapter = MySQLSessionAdapter(
            db,
            store.scope,
            store.bundles,
            channel="console",
        )
        state["task_messages"].append(
            {
                "id": f"cron-{index}",
                "role": "assistant",
                "content": f"cron history {index}",
            },
        )
        state = await adapter._save_state(
            adapter.identity("session", "alice"),
            state,
        )
        fresh = MySQLSessionAdapter(
            db,
            store.scope,
            store.bundles,
            channel="console",
        )
        assert await fresh.get_session_state_dict("session", "alice") == state
    current = await db.fetch_one(
        "SELECT * FROM swe_conversation_publications ORDER BY id DESC LIMIT 1",
    )
    dependencies = await db.fetch_all(
        "SELECT publication_id FROM swe_conversation_retentions WHERE retention_key=%s AND kind='dependency' AND state='active' ORDER BY publication_id",
        (f"dep:{current['id']}".encode(),),
    )
    assert len(dependencies) == 3
    assert not await db.fetch_all(
        "SELECT id FROM swe_conversation_retentions WHERE kind='reader' AND state='active'",
    )
