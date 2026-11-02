# -*- coding: utf-8 -*-
from datetime import datetime
from types import SimpleNamespace

import pytest

from swe.app.chat_sharing.store import ChatShareStore


async def test_scoped_share_token_rejects_foreign_row_before_use():
    calls = []

    async def fetch(sql, params):
        calls.append((sql, params))
        return {"service_unit_key": b"service-b"}

    store = ChatShareStore(
        SimpleNamespace(fetch_one=fetch),
        service_unit_id="service-a",
    )
    assert await store.get("token") is None
    assert "service_unit_key" in calls[0][0]
    assert calls[0][1] == ("token", b"service-a")


async def test_scoped_share_count_write_uses_bound_service():
    calls = []

    async def execute(sql, params):
        calls.append((sql, params))
        return 1

    store = ChatShareStore(
        SimpleNamespace(execute=execute),
        service_unit_id="service-a",
    )
    await store.record_access("token")
    assert "service_unit_key" in calls[0][0]
    assert calls[0][1][1:] == ("token", b"service-a")


async def test_scoped_share_create_rejects_foreign_owner_before_sql():
    store = ChatShareStore(None, service_unit_id="service-a")
    record = SimpleNamespace(
        service_unit_id="service-b",
        created_at=datetime(2026, 11, 2),
    )
    with pytest.raises(ValueError, match="service"):
        await store.create(record)
