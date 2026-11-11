# -*- coding: utf-8 -*-
"""Offline write stages: SQL admission, NAS publish, then SQL durability."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass

from ..bundle import BundleStore
from ..chat_mutations import load_metadata_json
from ..errors import ConflictError, IntegrityError, UnknownCommit
from ..publications import CommitPlan
from ..publications import _request as PublicationRequestFrom
from ..retention import RetentionStore
from ..session_projection import is_projection
from .bundle_write import publish_bundle
from .parallel import _drain
from .install_records import OfflineConfirmReceipt, OfflineConfirmTicket


@dataclass(frozen=True)
class TurnCandidate:
    turn_id: int
    key: str
    publication_id: int
    operation_key: str
    relative_path: str
    byte_length: int


@dataclass(frozen=True)
class TurnRecoveryTicket:
    operation_key: str
    candidates: tuple[TurnCandidate, ...]


@dataclass(frozen=True)
class TurnRecoveryEvidence:
    ticket: TurnRecoveryTicket
    bodies: tuple[bytes | None, ...]


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


class OfflineWriteStages:
    """Issue write tickets and consume their NAS receipts in the SQL owner."""

    def __init__(self, store):
        self.store = store
        self.prepared = {}
        self.recoveries = {}
        self.recovery_evidence = {}
        self._recovery_reads = set()
        self._orphaned_readers = []

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
        if plan.recovery_block is not None:
            return await self._prepare_recovery(row, plan, blocks)
        return self._issue(row, blocks)

    def _issue(self, row, prepared_blocks):
        request = PublicationRequestFrom(row)
        ticket = OfflineConfirmTicket(
            request,
            row["id"],
            BundleStore.validate_publication_locator(row),
            row["result_metadata_text"],
        )
        self.store._confirm_tickets[id(ticket)] = ticket
        self.prepared[id(ticket)] = (ticket, prepared_blocks)
        return ticket, prepared_blocks

    async def _prepare_recovery(self, row, plan, blocks):
        # Select locators and pin them in the same owner transaction. Worker
        # NAS reads are not admitted until its commit has returned.
        async with self.store.db.transaction():
            return await self._prepare_recovery_locked(row, plan, blocks)

    async def _prepare_recovery_locked(self, row, plan, blocks):
        request = PublicationRequestFrom(row)
        root = next(
            (block for block in blocks if block.name == plan.recovery_block),
            None,
        )
        if (
            plan != CommitPlan(recovery_block="recovery")
            or request.kind != "offline_import"
            or request.turn_id is None
            or root is None
            or not isinstance(root.content, bytes)
        ):
            raise IntegrityError(
                "recovery requires the general projection protocol",
            )
        state = load_metadata_json(root.content)
        if (
            is_projection(state)
            or not isinstance(state, dict)
            or set(state) != {"turn_states"}
            or not isinstance(state["turn_states"], dict)
            or len(state["turn_states"]) != 1
        ):
            raise IntegrityError(
                "recovery requires the general projection protocol",
            )
        key, value = next(iter(state["turn_states"].items()))
        turns = await self.store.db.fetch_all(
            "SELECT * FROM swe_conversation_turns WHERE session_id=%s AND service_unit_key=%s AND terminal_publication_id IS NOT NULL ORDER BY id",
            (row["session_id"], row["service_unit_key"]),
        )
        selected = []
        for turn in turns:
            if turn["service_unit_key"] != row["service_unit_key"]:
                raise IntegrityError("cold Turn service unit mismatch")
            if (
                _text(turn["question_msg_key"]) == key
                and isinstance(value, dict)
                and value.get("status") == turn["history_status"]
            ):
                selected.append(turn)
        if not selected:
            return self._issue(row, blocks)
        if len(selected) > 128:
            raise IntegrityError("offline recovery candidate limit exceeded")
        retentions = RetentionStore(self.store.db, request.scope)
        batch = None
        try:
            ids = sorted(
                {turn["terminal_publication_id"] for turn in selected},
            )
            batch = await retentions.pin(ids, kind="reader")
            publications = {
                publication["id"]: publication
                for publication in batch.publications
            }
            candidates = []
            for turn in selected:
                publication = publications[turn["terminal_publication_id"]]
                if (
                    publication["session_id"] != row["session_id"]
                    or publication["turn_id"] != turn["id"]
                ):
                    raise IntegrityError(
                        "cold Turn publication association differs",
                    )
                candidates.append(
                    TurnCandidate(
                        turn["id"],
                        key,
                        publication["id"],
                        _text(publication["operation_key"]),
                        BundleStore.validate_publication_locator(publication),
                        publication["byte_length"],
                    ),
                )
            ticket = TurnRecoveryTicket(
                request.operation_key,
                tuple(candidates),
            )
            self.recoveries[id(ticket)] = (
                ticket,
                deepcopy(row),
                blocks,
                deepcopy(selected),
                deepcopy(batch.publications),
                retentions,
                batch,
                deepcopy(value),
            )
            return ticket
        except BaseException:
            if batch is not None:
                try:
                    await retentions.finish_reader(batch)
                except (Exception, asyncio.CancelledError):
                    self._orphaned_readers.append((retentions, batch))
            raise

    def _recovery(self, ticket):
        known = self.recoveries.get(id(ticket))
        if known is None or known[0] is not ticket:
            raise IntegrityError("unknown offline recovery ticket")
        return known

    async def read_recovery(self, ticket, io):
        self._recovery(ticket)

        def read():
            bodies = []
            for candidate in ticket.candidates:
                index = self.store.bundles.inspect(
                    candidate.relative_path,
                    expected_length=candidate.byte_length,
                )
                if (
                    index.metadata.get("operation_key")
                    != candidate.operation_key
                ):
                    raise IntegrityError("cold Turn bundle identity mismatch")
                block = index.blocks.get("turn:" + candidate.key)
                if block is None or block.kind != "turn":
                    bodies.append(None)
                    continue
                bodies.append(
                    self.store.bundles.read_extent(
                        candidate.relative_path,
                        block.offset,
                        block.length,
                        expected_length=candidate.byte_length,
                    ),
                )
            return TurnRecoveryEvidence(ticket, tuple(bodies))

        task = asyncio.create_task(io.call(read))
        self._recovery_reads.add(task)
        try:
            evidence = await task
        except asyncio.CancelledError as error:
            raise UnknownCommit(ticket.operation_key) from error
        finally:
            self._recovery_reads.discard(task)
        self.recovery_evidence[id(evidence)] = (evidence, deepcopy(evidence))
        return evidence

    async def finish_recovery(self, ticket, evidence):
        known = self._recovery(ticket)
        _, original, blocks, turns, publications, retentions, batch, value = (
            known
        )
        try:
            if evidence is None:
                return None
            receipt = self.recovery_evidence.get(id(evidence))
            if (
                receipt is None
                or receipt[0] is not evidence
                or receipt[1] != evidence
                or evidence.ticket is not ticket
            ):
                raise IntegrityError("unknown or altered recovery evidence")
            async with self.store.db.transaction():
                for expected in turns:
                    current = await self.store.db.fetch_one(
                        "SELECT * FROM swe_conversation_turns WHERE id=%s AND service_unit_key=%s FOR UPDATE",
                        (expected["id"], expected["service_unit_key"]),
                    )
                    keys = (
                        "id",
                        "service_unit_key",
                        "session_id",
                        "question_msg_key",
                        "history_status",
                        "terminal_publication_id",
                    )
                    if current is None or any(
                        current[key] != expected[key] for key in keys
                    ):
                        raise IntegrityError(
                            "cold Turn changed during recovery preparation",
                        )
                ids = sorted(publication["id"] for publication in publications)
                current_publications = await retentions._lock_publications(ids)
                if tuple(current_publications) != tuple(publications):
                    raise IntegrityError("cold Turn publication changed")
                await retentions._validate_members(
                    batch.retention_key,
                    ids,
                    "reader",
                )
                current = await self.store.get(ticket.operation_key, lock=True)
                request = PublicationRequestFrom(original)
                self.store._matches(
                    current,
                    request,
                    original["result_metadata_text"],
                )
                if (
                    current["state"] != "prepared"
                    or current["id"] != original["id"]
                    or current["relative_path"] != original["relative_path"]
                ):
                    raise IntegrityError(
                        "offline recovery publication changed",
                    )
                if any(
                    raw is not None and load_metadata_json(raw) == value
                    for raw in receipt[1].bodies
                ):
                    raise IntegrityError(
                        "cold Turn projection requires the general projection protocol",
                    )
            return self._issue(current, blocks)
        finally:
            try:
                await retentions.finish_reader(batch)
            finally:
                self.recoveries.pop(id(ticket), None)
                for key, entry in tuple(self.recovery_evidence.items()):
                    if entry[0].ticket is ticket:
                        self.recovery_evidence.pop(key)

    async def close(self):
        """Owner cleanup after workers stop; drain NAS before releasing pins."""
        if self._recovery_reads:
            await _drain(
                asyncio.ensure_future(
                    asyncio.gather(
                        *tuple(self._recovery_reads),
                        return_exceptions=True,
                    ),
                ),
            )
        failure = None
        try:
            pending = list(self._orphaned_readers)
            self._orphaned_readers.clear()
            for retentions, batch in pending:
                try:
                    await retentions.finish_reader(batch)
                except (Exception, asyncio.CancelledError) as error:
                    self._orphaned_readers.append((retentions, batch))
                    if failure is None:
                        failure = error
            for known in tuple(self.recoveries.values()):
                try:
                    await self.finish_recovery(known[0], None)
                except asyncio.CancelledError as error:
                    if failure is None:
                        failure = error
                except Exception as error:
                    if failure is None:
                        failure = error
        finally:
            self.recovery_evidence.clear()
            for key, known in tuple(self.prepared.items()):
                self.store._confirm_tickets.pop(key, None)
                for receipt_key, receipt in tuple(
                    self.store._confirm_receipts.items(),
                ):
                    if receipt[0].ticket is known[0]:
                        self.store._confirm_receipts.pop(receipt_key, None)
            self.prepared.clear()
        if failure is not None:
            raise failure

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
