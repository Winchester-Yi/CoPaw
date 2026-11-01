# -*- coding: utf-8 -*-
"""Ended-owner recovery requires exact, externally verified evidence."""

from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.identity import (
    SessionIdentity,
    WorkspaceScope,
)


def test_recovery_requires_complete_stopped_incarnation_proof():
    from swe.app.conversation_storage.recovery import OwnerExitProof

    with pytest.raises(ValueError, match="stop evidence"):
        OwnerExitProof("owner", 1, "", "operator", True, True)
    with pytest.raises(ValueError, match="positive"):
        OwnerExitProof("owner", 0, "pod/uid-exited", "operator", True, True)


@pytest.mark.asyncio
async def test_recovery_never_infers_death_from_lease_or_unverified_boolean():
    from swe.app.conversation_storage.errors import ConflictError
    from swe.app.conversation_storage.recovery import (
        EndedOwnerRecovery,
        OwnerExitProof,
    )

    storage = AsyncMock()
    storage.scope = WorkspaceScope("tenant", "source", "agent")
    proof = OwnerExitProof(
        "owner",
        1,
        "pod/uid-exited",
        "operator",
        True,
        True,
    )
    service = EndedOwnerRecovery(
        storage,
        verify_stopped=AsyncMock(return_value=False),
    )
    with pytest.raises(ConflictError, match="not verified"):
        await service.recover(
            SessionIdentity(storage.scope, "alice", "console", "session"),
            1,
            proof,
        )
    storage.db.fetch_one.assert_not_awaited()


def test_recovery_evidence_rejects_unsafe_receipt_symlink(tmp_path):
    from swe.app.conversation_storage.recovery_cli import RecoveryJob

    target = tmp_path / "unrelated"
    target.write_text("preserve")
    link = tmp_path / "audit"
    link.symlink_to(target)
    with pytest.raises(OSError):
        RecoveryJob._record(link, {"phase": "started"})
    assert target.read_text() == "preserve"


@pytest.mark.parametrize("generation", [None, True, "1", -1])
async def test_reconcile_rejects_invalid_generation_before_normal_commit(
    generation,
):
    from swe.app.conversation_storage.errors import ConflictError
    from swe.app.conversation_storage.publications import PublicationStore

    store = PublicationStore(AsyncMock(), AsyncMock())
    store.commit = AsyncMock()
    with pytest.raises(ConflictError, match="generation"):
        await store.reconcile(
            "a" * 64,
            owner_stopped=True,
            expected_generation=generation,
        )
    store.commit.assert_not_awaited()
