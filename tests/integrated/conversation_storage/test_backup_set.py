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
        WorkspaceScope("tenant", "source", "agent"),
        WorkspaceScope("tenant", "other", "agent"),
        WorkspaceScope("other-tenant", "source", "agent"),
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
    service = PublicationStore(db, bundles)
    operation = new_operation_key()
    await service.reserve(
        PublicationRequest(
            operation,
            WorkspaceScope("tenant", "", ""),
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
        assert report["scopes"] == [
            [s.tenant_id, s.source_id, s.agent_id] for s in scopes
        ]
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
            await ShareStorage(clone, restored_bundles).verify_snapshot(
                orphan.token,
            )
        )["future"] == {"keep": True}
