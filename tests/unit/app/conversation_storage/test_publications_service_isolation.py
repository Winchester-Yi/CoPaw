# -*- coding: utf-8 -*-
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.publications import (
    PublicationRequest,
    PublicationStore,
)
from swe.app.conversation_storage.commit_plan import CommitPlan
from swe.app.conversation_storage.errors import IntegrityError


@pytest.mark.parametrize("reference", ["source", "prune"])
async def test_plan_message_ids_reject_foreign_service_before_nas(
    reference,
    tmp_path,
):
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
        request,
    )
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.message_installs import MessageInstall

    class Database(PublicationDatabase):
        async def fetch_all(self, query, params=None):
            if "swe_conversation_messages" in query:
                return [
                    {
                        "id": 999,
                        "session_id": 1,
                        "service_unit_key": b"foreign",
                    },
                ]
            return await super().fetch_all(query, params)

    store = PublicationStore(
        Database(),
        BundleStore(tmp_path),
        service_unit_id="schema-test-service",
    )
    plan = (
        CommitPlan(prune_ids=(999,), recovery_block="root")
        if reference == "prune"
        else CommitPlan(
            messages=(
                MessageInstall(
                    "body",
                    "archive",
                    "user",
                    source_message_id=999,
                    archive_chat_id=2,
                ),
            ),
        )
    )
    with pytest.raises(IntegrityError, match="service"):
        await store._validate_reference_ownership(request(), plan)


async def test_foreign_service_request_is_rejected_before_database_or_nas():
    db, bundles = AsyncMock(), AsyncMock()
    store = PublicationStore(db, bundles, service_unit_id="service-a")
    request = PublicationRequest(
        "a" * 64,
        WorkspaceScope("t", "s", "a", service_unit_id="service-b"),
        "recovery",
        "owner",
        0,
    )
    with pytest.raises(IntegrityError, match="service"):
        await store.reserve(request, CommitPlan())
    db.transaction.assert_not_called()
    bundles.publish.assert_not_called()


async def test_operation_lookup_is_service_scoped():
    db = AsyncMock()
    store = PublicationStore(db, AsyncMock(), service_unit_id="service-a")
    await store.get("a" * 64)
    query, values = db.fetch_one.call_args.args
    assert "service_unit_key=%s" in query
    assert b"service-a" in values


async def test_foreign_turn_association_is_rejected():
    request = PublicationRequest(
        "a" * 64,
        WorkspaceScope("t", "s", "a", service_unit_id="service-a"),
        "state",
        "owner",
        1,
        session_id=1,
        chat_id=2,
        turn_id=3,
    )
    session = {"id": 1, **request.scope.fields}
    chat = {"id": 2, "session_id": 1, **request.scope.fields}
    turn = {
        "id": 3,
        "session_id": 1,
        "chat_id": 2,
        "service_unit_key": b"service-b",
    }
    with pytest.raises(IntegrityError, match="Turn"):
        PublicationStore._validate_associations(request, session, chat, turn)


async def test_same_operation_key_is_independent_between_services(tmp_path):
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
    )
    from swe.app.conversation_storage.bundle import BundleStore

    class ServiceDatabase(PublicationDatabase):
        async def fetch_one(self, query, params=None):
            if "operation_key=%s" in query:
                assert "service_unit_key=%s" in query
                return self.rows.get(tuple(params))
            return await super().fetch_one(query, params)

        async def insert(self, query, params=None):
            if "swe_conversation_publications" not in query:
                return await super().insert(query, params)
            columns = query.split("(", 1)[1].split(")", 1)[0].split(",")
            row = dict(zip((column.strip() for column in columns), params))
            key = (row["service_unit_key"], row["operation_key"])
            if key not in self.rows:
                row.update(
                    id=len(self.rows) + 1,
                    gc_state="live",
                    byte_length=None,
                )
                self.rows[key] = row
            return self.rows[key]["id"]

    db = ServiceDatabase()
    stores = [
        PublicationStore(
            db,
            BundleStore(tmp_path / unit),
            service_unit_id=unit,
        )
        for unit in ("service-a", "service-b")
    ]
    requests = [
        PublicationRequest(
            "a" * 64,
            WorkspaceScope(
                "t",
                "s",
                "a",
                service_unit_id=store.service_unit_id,
            ),
            "state",
            "owner",
            0,
        )
        for store in stores
    ]
    rows = [
        await store.reserve(request, CommitPlan())
        for store, request in zip(stores, requests)
    ]
    assert rows[0]["id"] != rows[1]["id"]
    assert len(db.rows) == 2
    assert (await stores[0].get("a" * 64))["service_unit_key"] == b"service-a"
    assert (await stores[1].get("a" * 64))["service_unit_key"] == b"service-b"


@pytest.mark.parametrize(
    "association",
    ["session", "chat", "turn", "target_session"],
)
@pytest.mark.parametrize(
    "phase",
    ["reserve", "retry", "write", "confirm", "commit"],
)
async def test_foreign_reference_is_rejected_before_publication_or_nas(
    association,
    phase,
    tmp_path,
):
    from datetime import datetime
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
        request,
        metadata_plan,
    )
    from swe.app.conversation_storage.bundle import BundleStore
    from dataclasses import replace

    class Database(PublicationDatabase):
        async def fetch_one(self, query, params=None):
            if "swe_conversation_sessions" in query and params[0] == 4:
                return {
                    **self.session,
                    "id": 4,
                    "service_unit_key": b"foreign",
                }
            return await super().fetch_one(query, params)

    db = Database()
    operation, plan = request(), CommitPlan()
    if association == "target_session":
        operation = replace(operation, kind="chat_metadata", turn_id=None)
        plan = replace(
            metadata_plan(),
            chat_metadata=replace(
                metadata_plan().chat_metadata,
                target_session_id=4,
            ),
        )
    else:
        getattr(db, association)["service_unit_key"] = b"foreign"
    store = PublicationStore(
        db,
        BundleStore(tmp_path),
        service_unit_id="schema-test-service",
    )
    store._nas_io = AsyncMock(
        side_effect=AssertionError("foreign reference reached NAS"),
    )
    if phase != "reserve":
        row = {
            **operation.fields(),
            "id": 1,
            "state": "prepared",
            "result_metadata_text": plan.serialize(),
            "byte_length": 10,
            "durable_at": datetime.now(),
        }
        db.rows[operation.operation_key.encode()] = row
    with pytest.raises(IntegrityError):
        if phase in ("reserve", "retry"):
            await store.reserve(operation, plan)
        elif phase == "write":
            await store.write_bundle(operation.operation_key, [])
        elif phase == "confirm":
            await store.confirm_bundle(operation.operation_key)
        else:
            await store.commit(operation.operation_key)
    store._nas_io.assert_not_called()
    if phase == "reserve":
        assert not db.rows


async def test_historical_chat_rebind_preserves_prepared_reference_ownership(
    tmp_path,
):
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
        request,
    )
    from swe.app.conversation_storage.bundle import BundleStore, BundleBlock

    db = PublicationDatabase()
    store = PublicationStore(
        db,
        BundleStore(tmp_path),
        service_unit_id="schema-test-service",
    )
    operation = request()
    await store.reserve(operation, CommitPlan())
    db.chat["session_id"] = 99
    await store.reserve(operation, CommitPlan())
    await store.write_bundle(
        operation.operation_key,
        [BundleBlock("m", "message", b"{}")],
    )
    await store.confirm_bundle(operation.operation_key)
    assert (await store.get(operation.operation_key))["durable_at"]


def test_historical_archive_chat_rebind_keeps_scope_ownership():
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
        request,
    )
    from swe.app.conversation_storage.publication_associations import (
        PublicationAssociations,
    )

    db = PublicationDatabase()
    db.chat["session_id"] = 99
    PublicationAssociations._validate_archive_chat(request(), db.chat)
    db.chat["service_unit_key"] = b"foreign"
    with pytest.raises(IntegrityError, match="scope"):
        PublicationAssociations._validate_archive_chat(request(), db.chat)
