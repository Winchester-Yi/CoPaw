# -*- coding: utf-8 -*-
"""Offline write stages: SQL admission, NAS publish, then SQL durability."""

import asyncio
from copy import deepcopy

from ..bundle import BundleStore
from ..errors import ConflictError, IntegrityError, UnknownCommit
from ..projection_publication import prepare_recovery_blocks
from ..publications import _request as PublicationRequestFrom
from .bundle_write import publish_bundle
from .install_records import OfflineConfirmReceipt, OfflineConfirmTicket


class OfflineWriteStages:
    """Issue write tickets and consume their NAS receipts in the SQL owner."""

    def __init__(self, store):
        self.store = store
        self.prepared = {}

    async def prepare(self, operation_key, blocks):
        store = self.store
        blocks = tuple(blocks)
        try:
            row = await store.get(operation_key)
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(operation_key) from error
        if row is None or row["state"] != "prepared":
            raise ConflictError(
                "publication must be prepared before writing NAS",
            )
        request = PublicationRequestFrom(row)
        store._validate_service(request)
        plan = await store._validate_prepared_references(row)
        # Recovery transformation remains in the SQL owner. It may read NAS
        # while resolving historical message references and is not SQL-only.
        prepared_blocks = tuple(
            await prepare_recovery_blocks(store, row, plan, blocks),
        )
        ticket = OfflineConfirmTicket(
            request,
            row["id"],
            BundleStore.validate_publication_locator(row),
            row["result_metadata_text"],
        )
        store._confirm_tickets[id(ticket)] = ticket
        self.prepared[id(ticket)] = (ticket, prepared_blocks)
        return ticket, prepared_blocks

    async def publish(self, ticket, prepared_blocks, io):
        known = self.prepared.get(id(ticket))
        if known is None or known[0] is not ticket:
            raise IntegrityError("unknown offline write ticket")
        blocks = tuple(prepared_blocks)
        if len(blocks) != len(known[1]) or any(
            block is not original for block, original in zip(blocks, known[1])
        ):
            raise IntegrityError(
                "offline write blocks differ from preparation",
            )
        self.store._check_confirm_ticket(ticket)
        index = await publish_bundle(self.store.bundles, ticket, blocks, io)
        receipt = OfflineConfirmReceipt(ticket, index)
        self.store._confirm_receipts[id(receipt)] = (
            receipt,
            deepcopy(index),
        )
        return receipt

    async def commit(self, receipt):
        ticket = receipt.ticket
        known = self.prepared.get(id(ticket))
        if known is None or known[0] is not ticket:
            raise IntegrityError("unknown offline write ticket")
        return await self.store._commit_bundle_receipt(receipt)
