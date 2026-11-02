# -*- coding: utf-8 -*-
"""Faults around real imported batches and real atomic installation."""

import json

import pytest

from swe.app.conversation_storage.migration.importer import (
    ConversationImporter,
    bundle_stores,
)
from swe.app.conversation_storage.migration.install import TABLES
from swe.app.conversation_storage.migration.manifest import MigrationManifest
from swe.app.conversation_storage.migration.verify import verify_import
from swe.database.connection import DatabaseConnection
from swe.database.session import DatabaseSession


async def resume_committed_import(
    importer,
    report,
    monkeypatch,
    *,
    message_batch=False,
):
    committed_positions = []
    batch_committed = importer._batch_committed

    def interrupted(position):
        if message_batch and "/messages:" not in position:
            batch_committed(position)
            return
        committed_positions.append(position)
        raise RuntimeError(
            "injected crash after SQL commit before manifest update",
        )

    with monkeypatch.context() as fault:
        fault.setattr(importer, "_batch_committed", interrupted)
        with pytest.raises(RuntimeError, match="after SQL commit"):
            await importer.run(report)
    assert len(committed_positions) == 1
    position = committed_positions[0]
    if message_batch:
        assert "/messages:" in position
    manifest = MigrationManifest.load(importer.manifest.path)
    assert manifest.data["batches"][position] == "importing"
    assignment = manifest.data["assignments"]["publication"][position]
    original = await importer.db.fetch_one(
        "SELECT * FROM swe_conversation_publications WHERE id=%s",
        (assignment["target_id"],),
    )
    assert original["state"] == "committed"
    store = importer.bundle_stores[original["service_unit_key"].decode()]
    before_bytes = (store.root / assignment["relative_path"]).read_bytes()
    resumed = ConversationImporter(
        importer.db,
        importer.bundle_stores,
        manifest,
        importer.target,
        batch_size=importer.batch_size,
    )
    await resumed.run(report)
    assert manifest.data["batches"][position] == "committed"
    assert (
        await importer.db.fetch_one(
            "SELECT * FROM swe_conversation_publications WHERE id=%s",
            (assignment["target_id"],),
        )
        == original
    )
    assert (
        store.root / assignment["relative_path"]
    ).read_bytes() == before_bytes
    checked = await verify_import(
        importer.db,
        importer.bundle_stores,
        manifest,
        report,
        importer.target,
    )
    assert checked["passed"], checked["blockers"]
    importer.manifest.data = MigrationManifest.load(manifest.path).data


async def reject_missing_body(installer, staging, bundles, report):
    publication = await staging.fetch_one(
        "SELECT relative_path,service_unit_key FROM swe_conversation_publications "
        "WHERE state='committed' ORDER BY id LIMIT 1",
    )
    stores = bundle_stores(bundles, installer.manifest)
    store = stores[publication["service_unit_key"].decode()]
    path = store.root / publication["relative_path"].decode()
    original = path.read_bytes()
    path.unlink()
    try:
        checked = await verify_import(
            staging,
            bundles,
            installer.manifest,
            report,
            installer.target,
        )
        assert not checked["passed"] and checked["blockers"]
        assert all(
            item["position"] and item["detail"] for item in checked["blockers"]
        )
        with pytest.raises(ValueError, match="verification"):
            await installer.install(staging, bundles, report)
        assert not installer.receipt_path.exists()
        await assert_before_install(installer)
    finally:
        path.write_bytes(original)
    assert (
        await verify_import(
            staging,
            bundles,
            installer.manifest,
            report,
            installer.target,
        )
    )["passed"]


async def reject_held_metadata_lock(installer, staging, bundles, report):
    holder = DatabaseConnection(installer.db.config)
    await holder.connect()
    try:
        async with holder.transaction() as session:
            await session.fetch_all(
                "SELECT token FROM swe_chat_shares WHERE token='permanent'",
            )
            with pytest.raises(
                ValueError,
                match="active/pending metadata locks",
            ):
                await installer.install(staging, bundles, report)
            assert not installer.receipt_path.exists()
            await assert_before_install(installer)
    finally:
        await holder.close()


async def assert_before_install(installer):
    target = installer.target
    app = await installer.db.fetch_all(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
        (target.application_schema,),
    )
    staging = await installer.db.fetch_all(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
        (target.schema,),
    )
    assert {row["TABLE_NAME"] for row in app} == {
        "swe_chat_shares",
        "unrelated_business",
    }
    assert {row["TABLE_NAME"] for row in staging} == set(TABLES)
    assert (
        await installer.db.fetch_one(
            "SELECT access_count FROM swe_chat_shares WHERE token='permanent'",
        )
    )["access_count"] == 17


async def inspect_lost_rename_reply(
    installer,
    staging,
    bundles,
    report,
    monkeypatch,
):
    execute = DatabaseSession.execute
    injected = []

    async def lose_reply(session, query, params=None):
        result = await execute(session, query, params)
        if query == installer.rename_sql():
            injected.append(query)
            raise RuntimeError("injected reply loss after actual RENAME")
        return result

    with monkeypatch.context() as fault:
        fault.setattr(DatabaseSession, "execute", lose_reply)
        with pytest.raises(RuntimeError, match="outcome is unknown"):
            await installer.install(staging, bundles, report)
    assert injected == [installer.rename_sql()]
    pending = installer.receipt_path.read_bytes()
    assert json.loads(pending)["phase"] == "rename_pending"
    observer = DatabaseConnection(installer.db.config)
    await observer.connect()
    try:
        names = await observer.fetch_all(
            "SELECT TABLE_SCHEMA,TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA IN (%s,%s)",
            (installer.target.application_schema, installer.target.schema),
        )
        assert {
            r["TABLE_NAME"]
            for r in names
            if r["TABLE_SCHEMA"] == installer.target.application_schema
        } == {*TABLES, "unrelated_business"}
        assert {
            r["TABLE_NAME"]
            for r in names
            if r["TABLE_SCHEMA"] == installer.target.schema
        } == {installer.old_share}
        assert (
            await observer.fetch_one(
                "SELECT access_count FROM swe_chat_shares WHERE token='permanent'",
            )
        )["access_count"] == 17
        assert (
            await observer.fetch_one(
                f"SELECT access_count FROM `{installer.target.schema}`.`{installer.old_share}` WHERE token='permanent'",
            )
        )["access_count"] == 17
        assert (
            await observer.fetch_one(
                "SELECT id FROM unrelated_business",
            )
        )["id"] == 91
        assert (
            len(
                await observer.fetch_all(
                    "SHOW COLUMNS FROM swe_chat_shares",
                ),
            )
            > 8
        )
        # The original staging now contains only the retained old share.
        # Its verifier rejects reentry before any second RENAME. The separate
        # unit case covers the receipt-exists guard with a valid verifier.
        with pytest.raises(ValueError):
            await installer.install(staging, bundles, report)
        assert installer.receipt_path.read_bytes() == pending
    finally:
        await observer.close()
