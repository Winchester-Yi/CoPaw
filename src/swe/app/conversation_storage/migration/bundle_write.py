# -*- coding: utf-8 -*-
"""NAS-only publication of recovery blocks prepared by the SQL workflow."""

import asyncio
from typing import TYPE_CHECKING

from ..bundle import BundleBlock, BundleStore
from ..errors import IntegrityError, UnknownCommit
from ..identity import WorkspaceScope

if TYPE_CHECKING:
    from .install_records import OfflineConfirmTicket
    from .parallel import MigrationIO


async def publish_bundle(
    bundles: BundleStore,
    ticket: "OfflineConfirmTicket",
    prepared_blocks,
    io: "MigrationIO",
):
    """Publish pre-transformed blocks without SQL or recovery preparation.

    The caller must first validate the ticket's issuer identity in the owning
    OfflineImportStore. Path checks here do not establish SQL admission. The
    final SQL stage must revalidate that identity and the current prepared row.

    The block container is snapshotted before scheduling. BundleBlock is frozen
    and bytes are immutable; stream contents remain borrowed. Their caller must
    keep them open, positioned and exclusively owned until this call completes,
    including cancellation drain. No stream is copied into memory here.
    """
    request = ticket.request
    if not isinstance(request.scope, WorkspaceScope):
        raise IntegrityError("publication ticket requires a workspace scope")
    expected = BundleStore.path_for(
        request.operation_key,
        request.scope,
        scope_kind=request.scope_kind,
    )
    if ticket.relative_path != expected:
        raise IntegrityError("publication ticket path mismatch")
    blocks = tuple(prepared_blocks)
    if any(not isinstance(block, BundleBlock) for block in blocks):
        raise TypeError("prepared publication requires BundleBlock values")
    try:
        index = await io.call(
            bundles.publish,
            request.operation_key,
            blocks,
            scope=request.scope,
            scope_kind=request.scope_kind,
            metadata={"operation_key": request.operation_key},
        )
    except asyncio.CancelledError as error:
        # MigrationIO drains the actual write before cancellation propagates.
        raise UnknownCommit(request.operation_key, request) from error
    if (
        index.relative_path != expected
        or index.metadata.get("operation_key") != request.operation_key
    ):
        raise IntegrityError("published bundle operation or path mismatch")
    return index
