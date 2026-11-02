# -*- coding: utf-8 -*-
"""Multi-scope recovery into a single isolated MySQL schema and NAS root."""

import json
from copy import deepcopy

import pytest

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.backup import (
    ConversationBackup,
    RestoreEvidence,
    restore_backup,
    TABLES,
    load_backup,
    _atomic_json,
)
from swe.app.conversation_storage.errors import IntegrityError
from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.publications import (
    PublicationStore,
    PublicationRequest,
    CommitPlan,
)
from swe.app.conversation_storage.session_adapter import MySQLSessionAdapter
from swe.app.conversation_storage.shares import ShareInstall, ShareStorage
from tests.integrated.conversation_storage.test_rollback import (
    seed_new_data,
    empty_clone,
)


async def test_all_scopes_restore_together_with_shared_orphan(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    scopes = [
        WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id="schema-test-service",
        ),
        WorkspaceScope(
            "tenant",
            "other",
            "agent",
            service_unit_id="schema-test-service",
        ),
        WorkspaceScope(
            "other-tenant",
            "source",
            "agent",
            service_unit_id="schema-test-service",
        ),
    ]
    seeded = [
        await seed_new_data(
            db,
            tmp_path / "original",
            scope,
            initialize_schema=index == 0,
        )
        for index, scope in enumerate(scopes)
    ]
    bundles = seeded[0][1]
    service = PublicationStore(
        db,
        bundles,
        service_unit_id="schema-test-service",
    )
    operation = new_operation_key()
    await service.reserve(
        PublicationRequest(
            operation,
            WorkspaceScope(
                "tenant",
                "",
                "",
                service_unit_id="schema-test-service",
            ),
            "share_import",
            "offline",
            0,
            scope_kind="legacy_share",
        ),
        CommitPlan(
            share=ShareInstall(
                "orphan-token",
                "deleted-chat",
                "owner",
                "tenant",
                "old-original-key",
                "2026-10-30T00:00:00Z",
                access_count=17,
            ),
        ),
    )
    await service.write_bundle(
        operation,
        [
            BundleBlock(
                "share",
                "share",
                json.dumps(
                    {
                        "chat_name": "orphan",
                        "messages": [],
                        "future": {"keep": True},
                    },
                ).encode(),
            ),
        ],
    )
    await service.commit(operation)
    paths = []
    for index, scope in enumerate(scopes):
        path = tmp_path / f"backup-{index}"
        assert (await ConversationBackup(db, bundles, scope).capture(path))[
            "passed"
        ]
        paths.append(path)
    documents = [load_backup(path) for path in paths]
    expected_counts = {
        name: len(
            {
                row["token" if name == "shares" else "id"]
                for document in documents
                for row in document["snapshot"][name]
            },
        )
        for name in TABLES
    }
    async with empty_clone(db) as clone:
        restored_bundles = BundleStore(tmp_path / "restored")
        divergent = deepcopy(documents[1])
        next(
            row
            for row in divergent["snapshot"]["shares"]
            if row["token"] == "orphan-token"
        )["access_count"] += 1
        _atomic_json(paths[1] / "metadata.json", divergent)
        try:
            with pytest.raises(
                IntegrityError,
                match="tenant share snapshots differ",
            ):
                await restore_backup(
                    clone,
                    restored_bundles,
                    paths,
                    evidence=RestoreEvidence("stopped-all", "isolated"),
                )
            assert not restored_bundles.root.exists()
            for table in TABLES.values():
                assert (
                    await clone.fetch_one("SELECT COUNT(*) AS n FROM " + table)
                )["n"] == 0
        finally:
            _atomic_json(paths[1] / "metadata.json", documents[1])
        report = await restore_backup(
            clone,
            restored_bundles,
            paths,
            evidence=RestoreEvidence(
                "all-source-writers-and-gc-stopped",
                "generated-empty-clone",
            ),
        )
        assert report["passed"] and report["restore_ready"]
        assert report["scopes"] == [list(s.values) for s in scopes]
        for name in (
            "sessions",
            "chats",
            "turns",
            "publications",
            "messages",
            "shares",
        ):
            assert (
                await clone.fetch_one(
                    "SELECT COUNT(*) AS n FROM " + TABLES[name],
                )
            )["n"] == expected_counts[name]
        assert report["reader_counts"]["sessions"] == 3
        for scope, _, _, chat, share, state in seeded:
            actual = await MySQLSessionAdapter(
                clone,
                scope,
                restored_bundles,
                channel="console",
            ).get_session_state_dict("full_session", "alice")
            assert actual["future_module"] == state["future_module"]
            assert actual["turn_states"]["new-question"]["chat_id"] == chat
            assert (
                await ChatShareStore(clone).get(share.token)
            ).access_count == 1
        orphan = await ChatShareStore(clone).get("orphan-token")
        assert (
            orphan.access_count == 17
            and orphan.snapshot_key == "old-original-key"
        )
        assert (
            await ShareStorage(
                clone,
                restored_bundles,
                service_unit_id="schema-test-service",
            ).verify_snapshot(
                orphan.token,
            )
        )["future"] == {"keep": True}


async def test_multi_service_restore_routes_identical_bundle_paths_to_own_nas(
    conversation_test_database,
    tmp_path,
):
    from tests.integrated.conversation_storage.conftest import (
        SQL_ROOT,
        apply_sql_script,
    )

    db = conversation_test_database
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    operation = new_operation_key()
    paths = []
    originals = {}
    for service in ("service-A", "service-B"):
        scope = WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id=service,
        )
        original_root = tmp_path / ("original-" + service)
        original_root.mkdir()
        bundles = BundleStore(original_root)
        store = PublicationStore(db, bundles, service_unit_id=service)
        await store.reserve(
            PublicationRequest(
                operation,
                WorkspaceScope("tenant", "", "", service_unit_id=service),
                "share_import",
                "offline",
                0,
                scope_kind="legacy_share",
            ),
            CommitPlan(
                share=ShareInstall(
                    "token-" + service,
                    "deleted-chat",
                    "owner",
                    "tenant",
                    "old-key",
                    "2026-11-02T00:00:00Z",
                ),
            ),
        )
        payload = {
            "chat_name": service,
            "messages": [],
            "service_payload": service,
        }
        await store.write_bundle(
            operation,
            [BundleBlock("share", "share", json.dumps(payload).encode())],
        )
        await store.commit(operation)
        originals[service] = (
            bundles.root / bundles.path_for(operation)
        ).read_bytes()
        path = tmp_path / ("backup-" + service)
        await ConversationBackup(db, bundles, scope).capture(path)
        paths.append(path)
    assert originals["service-A"] != originals["service-B"]
    async with empty_clone(db) as clone:
        stores = {
            service: BundleStore(tmp_path / ("restored-" + service))
            for service in originals
        }
        with pytest.raises(IntegrityError, match="NAS mapping"):
            await restore_backup(
                clone,
                stores["service-A"],
                paths,
                evidence=RestoreEvidence("all-services-stopped", "isolated"),
            )
        assert not stores["service-A"].root.exists()
        report = await restore_backup(
            clone,
            stores,
            paths,
            evidence=RestoreEvidence("all-services-stopped", "isolated"),
        )
        assert report["passed"] and report["restore_ready"]
        for service, store in stores.items():
            assert (
                store.root / store.path_for(operation)
            ).read_bytes() == originals[service]
            payload = await ShareStorage(
                clone,
                store,
                service_unit_id=service,
            ).verify_snapshot("token-" + service)
            assert payload["service_payload"] == service
            with pytest.raises(KeyError):
                await ShareStorage(
                    clone,
                    store,
                    service_unit_id=service,
                ).verify_snapshot(
                    "token-"
                    + ("service-B" if service == "service-A" else "service-A"),
                )


async def test_restore_rejects_source_tokens_equivalent_in_target_collation(
    conversation_test_database,
    tmp_path,
):
    from tests.integrated.conversation_storage.conftest import (
        SQL_ROOT,
        apply_sql_script,
    )

    db = conversation_test_database
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    # A valid stopped source can use stricter equality than the restore target.
    await db.execute(
        "ALTER TABLE swe_chat_shares MODIFY token VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL",
    )
    paths = []
    for service, token in (("A", "ShareToken"), ("B", "sharetoken")):
        scope = WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id=service,
        )
        root = tmp_path / ("source-" + service)
        root.mkdir()
        bundles = BundleStore(root)
        store = PublicationStore(db, bundles, service_unit_id=service)
        operation = new_operation_key()
        await store.reserve(
            PublicationRequest(
                operation,
                WorkspaceScope("tenant", "", "", service_unit_id=service),
                "share_import",
                "offline",
                0,
                scope_kind="legacy_share",
            ),
            CommitPlan(
                share=ShareInstall(
                    token,
                    "deleted-chat",
                    "owner",
                    "tenant",
                    "old-key",
                    "2026-11-02T00:00:00Z",
                ),
            ),
        )
        await store.write_bundle(
            operation,
            [
                BundleBlock(
                    "share",
                    "share",
                    b'{"chat_name":"kept","messages":[]}',
                ),
            ],
        )
        await store.commit(operation)
        path = tmp_path / ("backup-" + service)
        await ConversationBackup(db, bundles, scope).capture(path)
        paths.append(path)
    async with empty_clone(db) as clone:
        stores = {
            service: BundleStore(tmp_path / ("target-" + service))
            for service in ("A", "B")
        }
        with pytest.raises(IntegrityError, match="token.*collation"):
            await restore_backup(
                clone,
                stores,
                paths,
                evidence=RestoreEvidence("all-stopped", "isolated"),
            )
        assert all(not store.root.exists() for store in stores.values())
        for table in TABLES.values():
            assert (
                await clone.fetch_one("SELECT COUNT(*) AS n FROM " + table)
            )["n"] == 0
