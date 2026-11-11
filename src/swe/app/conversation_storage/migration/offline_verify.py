# -*- coding: utf-8 -*-
"""Committed retry verification with NAS work outside the SQL owner.

Only immutable byte blocks are admitted. Recovery bodies must match exactly;
hydrating older projection bodies remains in the original verification path.
The interface is independent and is not wired into the default importer.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json

from ..bundle import BundleBlock, BundleIndex, BundleStore
from ..chat_mutations import load_metadata_json
from ..errors import ConflictError, IntegrityError, StorageError, UnknownCommit
from ..publications import PublicationRequest


@dataclass(frozen=True)
class VerifyTicket:
    request: PublicationRequest
    publication_id: int
    relative_path: str
    byte_length: int
    plan_text: str
    target: tuple[str, str]
    expected_import_plan: bytes
    blocks: tuple[BundleBlock, ...]


@dataclass(frozen=True)
class VerifyEvidence:
    ticket: VerifyTicket
    index: BundleIndex
    import_plan: bytes


class OfflineVerifyStages:
    """Owner fixes expectations, workers verify NAS, owner verifies SQL."""

    def __init__(self, store):
        self.store = store
        self.tickets = {}
        self.receipts = {}

    def _ticket(self, ticket):
        known = self.tickets.get(id(ticket))
        if known is None or known[0] is not ticket or known[1] != ticket:
            raise IntegrityError("unknown or altered offline verify ticket")

    def _match(self, row, ticket):
        store = self.store
        store._matches(row, ticket.request, ticket.plan_text)
        actual = (
            row["state"],
            row["id"],
            BundleStore.validate_publication_locator(row),
            row["byte_length"],
            bool(row.get("durable_at")),
            store.target.schema,
            store.target.migration_id,
        )
        expected = (
            "committed",
            ticket.publication_id,
            ticket.relative_path,
            ticket.byte_length,
            True,
            *ticket.target,
        )
        if actual != expected:
            raise IntegrityError("committed verification publication changed")

    async def prepare(self, request, publication_id, plan, expected, blocks):
        store = self.store
        await store.validate_target()
        store._validate_service(request)
        store._validate_plan(expected)
        row = await store.get(request.operation_key)
        store._matches(row, request, plan.serialize())
        if row["state"] != "committed":
            raise ConflictError(
                "verification requires a committed publication",
            )
        frozen_blocks = tuple(blocks)
        if any(
            not isinstance(block, BundleBlock)
            or not isinstance(block.content, bytes)
            for block in frozen_blocks
        ):
            raise TypeError(
                "offline verification requires immutable byte blocks",
            )
        ticket = VerifyTicket(
            request,
            publication_id,
            BundleStore.validate_publication_locator(row),
            row["byte_length"],
            plan.serialize(),
            (store.target.schema, store.target.migration_id),
            json.dumps(
                expected,
                separators=(",", ":"),
                allow_nan=False,
            ).encode(),
            frozen_blocks,
        )
        self._match(row, ticket)
        self.tickets[id(ticket)] = (ticket, deepcopy(ticket))
        return ticket

    async def read(self, ticket, io):
        self._ticket(ticket)
        bundles = self.store.bundles

        def read_evidence():
            index = bundles.inspect(
                ticket.relative_path,
                expected_length=ticket.byte_length,
            )
            if (
                index.relative_path != ticket.relative_path
                or index.byte_length != ticket.byte_length
                or index.metadata.get("operation_key")
                != ticket.request.operation_key
            ):
                raise IntegrityError(
                    "offline verification bundle identity mismatch",
                )

            expected_blocks = {"import_plan": "import_plan"}
            for expected in ticket.blocks:
                if expected.name in expected_blocks:
                    raise IntegrityError(
                        "offline verification block names are duplicated",
                    )
                expected_blocks[expected.name] = expected.kind
            actual_blocks = {
                name: extent.kind for name, extent in index.blocks.items()
            }
            if actual_blocks != expected_blocks:
                raise IntegrityError(
                    "original migration bundle block set differs from source",
                )

            def block(name, kind):
                extent = index.blocks.get(name)
                if extent is None or extent.kind != kind:
                    raise IntegrityError(
                        "original migration bundle block identity differs",
                    )
                return bundles.read_extent(
                    index.relative_path,
                    extent.offset,
                    extent.length,
                    expected_length=index.byte_length,
                )

            imported = block("import_plan", "import_plan")
            if load_metadata_json(imported) != load_metadata_json(
                ticket.expected_import_plan,
            ):
                raise IntegrityError(
                    "original import plan differs from current source mapping",
                )
            for expected in ticket.blocks:
                if block(expected.name, expected.kind) != expected.content:
                    detail = (
                        "; recovery projection requires original verification"
                        if expected.kind == "recovery"
                        else ""
                    )
                    raise IntegrityError(
                        "original migration bundle content differs from source"
                        + detail,
                    )
            return VerifyEvidence(ticket, index, imported)

        try:
            evidence = await io.call(read_evidence)
        except asyncio.CancelledError as error:
            raise UnknownCommit(
                ticket.request.operation_key,
                ticket.request,
            ) from error
        self.receipts[id(evidence)] = (evidence, deepcopy(evidence))
        return evidence

    async def verify(self, evidence):
        known = self.receipts.get(id(evidence))
        if known is None or known[0] is not evidence or known[1] != evidence:
            raise IntegrityError("unknown or altered offline verify evidence")
        self._ticket(evidence.ticket)
        frozen = known[1]
        ticket = frozen.ticket
        store = self.store
        try:
            await store.validate_target()
            async with store.db.transaction():
                row = await store.get(ticket.request.operation_key, lock=True)
                self._match(row, ticket)
                imported = load_metadata_json(frozen.import_plan)
                store._validate_plan(imported)
                await store._verify_records_sql(row, imported, frozen.index)
                from ..publications import CommitPlan

                plan = CommitPlan.parse(ticket.plan_text)
                if plan.share is not None:
                    await self._verify_direct_share(
                        ticket,
                        plan.share,
                        frozen.index,
                    )
            self.receipts.pop(id(evidence))
            self.tickets.pop(id(evidence.ticket))
            return row
        except StorageError:
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(
                ticket.request.operation_key,
                ticket.request,
            ) from error

    async def _verify_direct_share(self, ticket, share, index):
        from .direct_import import DirectImportStore

        store = self.store
        if not isinstance(store, DirectImportStore):
            await store._verify_share_record(share)
            return
        actual = await store.db.fetch_one(
            "SELECT * FROM swe_chat_shares WHERE token=%s FOR UPDATE",
            (share.token,),
        )
        extent = index.blocks.get(share.block_name)
        if extent is None or actual is None:
            raise IntegrityError("committed imported share locator is missing")
        await store._verify_share_record(share)
        locator = (
            ticket.request.scope.sql_values[0],
            ticket.publication_id,
            extent.offset,
            extent.length,
        )
        before = tuple(
            actual[key]
            for key in (
                "service_unit_key",
                "publication_id",
                "byte_offset",
                "byte_length",
            )
        )
        if before != locator:
            raise IntegrityError("committed imported share locator changed")
        await store.journal.record_share(share.token, before, locator)
