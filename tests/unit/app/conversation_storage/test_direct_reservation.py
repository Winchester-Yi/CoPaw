# -*- coding: utf-8 -*-
"""Publication reservation uses its known position without a full mirror scan."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.errors import IntegrityError
from swe.app.conversation_storage.migration.direct_import import (
    DirectImporter,
    DirectImportStore,
)
from swe.app.conversation_storage.migration.install_records import (
    OfflineImportStore,
)


class IndexedTickets(dict):
    def items(self):
        pytest.fail("normal reservation must not scan all publication tickets")


@pytest.mark.parametrize(
    "target_id,operation",
    [(84, "original"), (85, "original"), (84, "changed")],
)
async def test_direct_reservation_uses_exact_position(
    monkeypatch,
    target_id,
    operation,
):
    tickets = IndexedTickets(
        {"source/batch": {"target_id": 84, "operation_key": "original"}},
    )
    store = object.__new__(DirectImportStore)
    store.journal = SimpleNamespace(
        manifest=SimpleNamespace(
            data={"assignments": {"publication": tickets}},
        ),
    )
    store.batch_positions = {}
    reserve = AsyncMock(return_value={"state": "prepared"})
    monkeypatch.setattr(OfflineImportStore, "reserve_batch", reserve)
    request = SimpleNamespace(operation_key=operation)
    if target_id == 84 and operation == "original":
        result = await store.reserve_batch(
            request,
            target_id,
            position="source/batch",
        )
        assert result == {"state": "prepared"}
        assert store.batch_positions == {"original": "source/batch"}
        reserve.assert_awaited_once_with(request, 84, None)
    else:
        with pytest.raises(IntegrityError, match="durable SQL ticket"):
            await store.reserve_batch(
                request,
                target_id,
                position="source/batch",
            )
        reserve.assert_not_awaited()
        assert store.batch_positions == {}


async def test_direct_importer_forwards_known_position():
    importer = object.__new__(DirectImporter)
    importer.store = SimpleNamespace(reserve_batch=AsyncMock(return_value={}))
    request, plan = object(), object()
    await importer._reserve_batch(
        "known-position",
        request,
        {"target_id": 84},
        plan,
    )
    importer.store.reserve_batch.assert_awaited_once_with(
        request,
        84,
        plan,
        position="known-position",
    )
