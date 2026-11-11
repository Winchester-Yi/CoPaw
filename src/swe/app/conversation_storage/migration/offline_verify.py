# -*- coding: utf-8 -*-
"""Committed retry verification with NAS work outside the SQL owner.

Only immutable byte blocks are admitted. Projection comparison uses an owner
capture of canonical reference rows and reader pins, then worker NAS reads.
Call release_recovery from the SQL owner if comparison fails or is cancelled.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
import json

from ..bundle import BundleBlock, BundleIndex, BundleStore
from ..chat_mutations import load_metadata_json
from ..errors import ConflictError, IntegrityError, StorageError, UnknownCommit
from ..publications import PublicationRequest, _request
from ..projection_references import (
    ProjectionReferences,
    _snapshot_rows,
    _text,
    references,
    ref_key,
)
from ..retention import ReaderPins, RetentionStore
from ..session_projection import hydrate_state, is_projection


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
    expected_state: str = "committed"
    verify_records: bool = True


@dataclass(frozen=True)
class VerifyEvidence:
    ticket: VerifyTicket
    index: BundleIndex
    import_plan: bytes
    recoveries: tuple[tuple[str, bytes], ...] = ()


@dataclass(frozen=True)
class RecoveryCapture:
    evidence: VerifyEvidence
    snapshots: tuple[dict, ...]


def _add_projection_blocks(index, envelope, expected_blocks):
    for ref in references(envelope):
        if "publication_id" in ref:
            continue
        name = ref["block_name"]
        extent = index.blocks.get(name)
        if extent is None:
            raise IntegrityError("projection block is missing")
        expected_blocks[name] = extent.kind


def _projection_payload(payloads, ref):
    return payloads[ref_key(ref)]


class OfflineVerifyStages:
    """Owner fixes expectations, workers verify NAS, owner verifies SQL."""

    def __init__(self, store):
        self.store = store
        self.tickets = {}
        self.receipts = {}
        self.recoveries = {}
        self._orphaned_snapshots = []

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
            ticket.expected_state,
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

    async def prepare_blocks(self, operation_key, blocks):
        """SQL admission for pre-install evidence; includes import_plan."""
        store = self.store
        await store.validate_target()
        row = await store.get(operation_key)
        if row is None or row["state"] != "prepared":
            raise ConflictError(
                "block verification requires a prepared publication",
            )
        request = _request(row)
        store._validate_service(request)
        blocks = tuple(blocks)
        if any(
            not isinstance(block, BundleBlock)
            or not isinstance(block.content, bytes)
            for block in blocks
        ):
            raise TypeError(
                "offline verification requires immutable byte blocks",
            )
        plans = [block for block in blocks if block.name == "import_plan"]
        if len(plans) != 1 or plans[0].kind != "import_plan":
            raise IntegrityError(
                "expected import plan block is missing or duplicated",
            )
        store._validate_plan(load_metadata_json(plans[0].content))
        ticket = VerifyTicket(
            request,
            row["id"],
            BundleStore.validate_publication_locator(row),
            row["byte_length"],
            row["result_metadata_text"],
            (store.target.schema, store.target.migration_id),
            plans[0].content,
            tuple(block for block in blocks if block.name != "import_plan"),
            "prepared",
            False,
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
            recoveries = []
            for expected in ticket.blocks:
                raw = block(expected.name, expected.kind)
                if raw == expected.content:
                    continue
                envelope = load_metadata_json(raw)
                if expected.kind == "recovery" and is_projection(envelope):
                    recoveries.append((expected.name, raw))
                    _add_projection_blocks(index, envelope, expected_blocks)
                    continue
                raise IntegrityError(
                    "original migration bundle content differs from source",
                )
            actual_blocks = {
                name: extent.kind for name, extent in index.blocks.items()
            }
            if actual_blocks != expected_blocks:
                raise IntegrityError(
                    "original migration bundle block set differs from source",
                )
            return VerifyEvidence(ticket, index, imported, tuple(recoveries))

        try:
            evidence = await io.call(read_evidence)
        except asyncio.CancelledError as error:
            raise UnknownCommit(
                ticket.request.operation_key,
                ticket.request,
            ) from error
        self.receipts[id(evidence)] = (evidence, deepcopy(evidence))
        return evidence

    def _evidence(self, evidence):
        known = self.receipts.get(id(evidence))
        if known is None or known[0] is not evidence or known[1] != evidence:
            raise IntegrityError("unknown or altered offline verify evidence")
        self._ticket(evidence.ticket)
        return known[1]

    async def capture_recovery(self, evidence):
        """Owner validates references and pins dependencies without NAS."""
        frozen = self._evidence(evidence)
        if not frozen.recoveries:
            return None
        if id(evidence) in self.recoveries:
            raise IntegrityError("recovery evidence is already captured")
        store, ticket = self.store, frozen.ticket
        snapshots, captured = [], []
        try:
            await store.validate_target()
            self._match(await store.get(ticket.request.operation_key), ticket)
            for name, raw in frozen.recoveries:
                envelope = load_metadata_json(raw)
                refs = references(envelope)
                snapshot = ProjectionReferences()
                snapshots.append(snapshot)
                snapshot.batch = ReaderPins(
                    RetentionStore(store.db, ticket.request.scope),
                )
                async with store.db.transaction():
                    await _snapshot_rows(
                        store,
                        ticket.request,
                        refs,
                        snapshot,
                    )
                    ids = sorted(
                        {
                            ref["publication_id"]
                            for ref in refs
                            if "publication_id" in ref
                        },
                    )
                    if any(
                        identity >= ticket.publication_id for identity in ids
                    ):
                        raise IntegrityError(
                            "recovery dependency must be an older publication",
                        )
                    if ids:
                        await snapshot.batch.pin(ids)
                        snapshot.publications = {
                            row["id"]: row
                            for row in snapshot.batch.publications
                        }
                captured.append(
                    {
                        "name": name,
                        "envelope": envelope,
                        "rows": deepcopy(snapshot.rows),
                        "turns": deepcopy(snapshot.turns),
                        "publications": deepcopy(snapshot.publications),
                    },
                )
            capture = RecoveryCapture(evidence, tuple(captured))
            self.recoveries[id(evidence)] = {
                "capture": capture,
                "frozen": deepcopy(capture),
                "snapshots": snapshots,
                "reading": False,
                "compared": False,
            }
            return capture
        except BaseException:
            try:
                await self._finish_snapshots(snapshots)
            except (Exception, asyncio.CancelledError):
                self._orphaned_snapshots.extend(snapshots)
            raise

    def _capture(self, capture):
        state = self.recoveries.get(id(capture.evidence))
        if (
            state is None
            or state["capture"] is not capture
            or state["frozen"] != capture
        ):
            raise IntegrityError("unknown or altered recovery capture")
        self._evidence(capture.evidence)
        return state

    async def compare_recovery(self, capture, io):
        """Worker reads pinned bodies and performs canonical hydration."""
        state = self._capture(capture)
        if state["reading"]:
            raise IntegrityError("recovery comparison is already running")
        state["reading"] = True
        try:
            await io.call(self._compare_recovery, state["frozen"])
            state["compared"] = True
            return capture.evidence
        finally:
            # MigrationIO drains its actual worker before returning/cancelling.
            state["reading"] = False

    def _compare_recovery(self, capture):
        from ..projection_publication import _validate_ref_shape

        evidence = capture.evidence
        ticket, bundles = evidence.ticket, self.store.bundles
        expected = {block.name: block.content for block in ticket.blocks}
        for snapshot in capture.snapshots:
            payloads = {}
            for ref in references(snapshot["envelope"]):
                _validate_ref_shape(ref)
                if "publication_id" not in ref:
                    if ref["operation_key"] != ticket.request.operation_key:
                        raise IntegrityError("projection operation differs")
                    index = evidence.index
                    extent = index.blocks[ref["block_name"]]
                    path, size = index.relative_path, index.byte_length
                    offset, length = extent.offset, extent.length
                else:
                    publication = snapshot["publications"][
                        ref["publication_id"]
                    ]
                    if publication["session_id"] != ticket.request.session_id:
                        raise IntegrityError(
                            "recovery projection dependency Session differs",
                        )
                    path = BundleStore.validate_publication_locator(
                        publication,
                    )
                    size = publication["byte_length"]
                    if "message_id" in ref:
                        offset, length = ref["offset"], ref["length"]
                    else:
                        prior = bundles.inspect(path, expected_length=size)
                        extent = prior.blocks.get(ref["block_name"])
                        if extent is None or extent.kind != "turn":
                            raise IntegrityError(
                                "cold Turn requires an independent Turn body",
                            )
                        offset, length = extent.offset, extent.length
                value = json.loads(
                    bundles.read_extent(
                        path,
                        offset,
                        length,
                        expected_length=size,
                    ),
                )
                payloads[ref_key(ref)] = value
                if "message_id" in ref:
                    row = snapshot["rows"][ref["message_id"]]
                    if (
                        not isinstance(value, dict)
                        or value.get("id") != _text(row["source_msg_key"])
                        or (value.get("role") or "assistant")
                        != _text(row["role_key"])
                    ):
                        raise IntegrityError(
                            "recovery projection body identity mismatch",
                        )
                elif "turn_id" in ref:
                    turn = snapshot["turns"][ref["turn_id"]]
                    if (
                        ref["block_name"]
                        != "turn:" + _text(turn["question_msg_key"])
                        or value.get("status") != turn["history_status"]
                    ):
                        raise IntegrityError(
                            "cold Turn independent body identity differs",
                        )

            actual = hydrate_state(
                snapshot["envelope"],
                partial(_projection_payload, payloads),
            )
            if actual != load_metadata_json(expected[snapshot["name"]]):
                raise IntegrityError(
                    "original migration recovery differs from frozen source",
                )

    async def _finish_snapshots(self, snapshots):
        # Await cleanup directly in the SQL owner; shielding would change task.
        cancelled = False
        for snapshot in reversed(snapshots):
            pins = snapshot.batch
            if pins is None:
                continue
            for batch in reversed(pins.batches):
                while True:
                    try:
                        await pins.store.finish_reader(batch)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                        continue
            snapshot.batch = None
        if cancelled:
            raise asyncio.CancelledError

    async def release_recovery(self, capture):
        """SQL owner releases pins after comparison has drained."""
        state = self._capture(capture)
        if state["reading"]:
            raise IntegrityError("recovery comparison has not drained")
        try:
            await self._finish_snapshots(state["snapshots"])
        except asyncio.CancelledError:
            self.recoveries.pop(id(capture.evidence), None)
            raise
        self.recoveries.pop(id(capture.evidence), None)

    async def close(self):
        """Owner cleanup after all actual NAS comparisons have drained."""
        if any(state["reading"] for state in self.recoveries.values()):
            raise IntegrityError("recovery comparison has not drained")
        error = None
        pending = self._orphaned_snapshots
        self._orphaned_snapshots = []
        try:
            await self._finish_snapshots(pending)
        except (Exception, asyncio.CancelledError) as caught:
            self._orphaned_snapshots.extend(
                snapshot for snapshot in pending if snapshot.batch is not None
            )
            error = caught
        for state in list(self.recoveries.values()):
            try:
                await self._finish_snapshots(state["snapshots"])
            except asyncio.CancelledError as caught:
                self.recoveries.pop(id(state["capture"].evidence), None)
                error = error or caught
            except Exception as caught:
                error = error or caught
            else:
                self.recoveries.pop(id(state["capture"].evidence), None)
        if error is not None:
            raise error

    async def verify(self, evidence):
        frozen = self._evidence(evidence)
        recovery = self.recoveries.get(id(evidence))
        if frozen.recoveries and (
            recovery is None or not recovery["compared"] or recovery["reading"]
        ):
            raise IntegrityError("recovery comparison is incomplete")
        ticket = frozen.ticket
        store = self.store
        try:
            await store.validate_target()
            async with store.db.transaction():
                row = await store.get(ticket.request.operation_key, lock=True)
                self._match(row, ticket)
                if recovery is not None:
                    for snapshot in recovery["snapshots"]:
                        await snapshot.revalidate(store.db)
                imported = load_metadata_json(frozen.import_plan)
                store._validate_plan(imported)
                if ticket.verify_records:
                    await store._verify_records_sql(
                        row,
                        imported,
                        frozen.index,
                    )
                from ..publications import CommitPlan

                plan = CommitPlan.parse(ticket.plan_text)
                if ticket.verify_records and plan.share is not None:
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
        finally:
            if recovery is not None:
                # Receipts may already be consumed, so release internal state.
                try:
                    await self._finish_snapshots(recovery["snapshots"])
                except asyncio.CancelledError:
                    self.recoveries.pop(id(evidence), None)
                    raise
                self.recoveries.pop(id(evidence), None)

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
