# -*- coding: utf-8 -*-
"""Recovery projection effective installers are immutable and scope bound."""

import json
from dataclasses import replace

import pytest

from swe.app.conversation_storage.bundle import BundleBlock, BundleStore
from swe.app.conversation_storage.publications import (
    CommitPlan,
    PublicationStore,
)
from tests.unit.app.conversation_storage.test_publications import (
    PublicationDatabase,
    request,
)


async def test_recovery_write_projects_message_bodies_into_delta_blocks(
    tmp_path,
):
    db = PublicationDatabase()
    db.session["recovery_publication_id"] = None
    db.fetch_all = __import__("unittest.mock").mock.AsyncMock(return_value=[])
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    await service.reserve(operation, CommitPlan(recovery_block="root"))
    raw = {
        "revision": 1,
        "agent": {
            "memory": {
                "content": [
                    [{"id": "q", "role": "user", "content": "large body"}, []],
                ],
            },
        },
        "unknown": {"keep": True},
    }
    index = await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", json.dumps(raw).encode())],
    )
    extent = index.blocks["root"]
    root = json.loads(
        service.bundles.read_extent(
            index.relative_path,
            extent.offset,
            extent.length,
            expected_length=index.byte_length,
        ),
    )
    assert root["format"] == "swe-recovery-projection"
    assert root["identity"]["operation_key"] == operation.operation_key
    assert root["identity"]["session_id"] == 1
    assert root["state"]["agent"]["memory"]["content"] == []
    assert root["state"]["unknown"] == {"keep": True}
    assert root["installations"][0]["layer"] == "memory"
    await service.commit(operation.operation_key)
    assert len(db.messages) == 1


@pytest.mark.parametrize(
    "field",
    ["identity", "dependencies", "installer", "samepub"],
)
async def test_effective_projection_cannot_change_prepared_identity_or_smuggle_installers(
    tmp_path,
    field,
):
    from swe.app.conversation_storage.projection_publication import (
        effective_recovery_plan,
    )
    from swe.app.conversation_storage.errors import IntegrityError

    db = PublicationDatabase()
    db.session["recovery_publication_id"] = None
    db.fetch_all = __import__("unittest.mock").mock.AsyncMock(return_value=[])
    service = PublicationStore(db, BundleStore(tmp_path))
    operation = request()
    plan = CommitPlan(recovery_block="root")
    await service.reserve(operation, plan)
    state = {
        "agent": {
            "memory": {
                "content": [
                    [{"id": "q", "role": "user", "content": "text"}, []],
                ],
            },
        },
    }
    index = await service.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", json.dumps(state).encode())],
    )
    extent = index.blocks["root"]
    value = json.loads(
        service.bundles.read_extent(
            index.relative_path,
            extent.offset,
            extent.length,
            expected_length=index.byte_length,
        ),
    )
    if field == "identity":
        value["identity"]["session_id"] = 99
    elif field == "dependencies":
        value["dependency_ids"] = [999]
    elif field == "installer":
        value["installations"][0]["layer"] = "anchor"
    else:
        value["lists"][0]["entries"][0]["ref"]["operation_key"] = "b" * 64
    original = service._nas_io

    async def forged(key, function, *args, **kwargs):
        return json.dumps(value).encode()

    service._nas_io = forged
    with pytest.raises(IntegrityError):
        await effective_recovery_plan(
            service,
            operation,
            await service.get(operation.operation_key),
            plan,
            index,
        )
