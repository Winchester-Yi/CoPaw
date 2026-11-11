# -*- coding: utf-8 -*-
"""Bounded NAS evidence for the three plans emitted by the offline importer."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace

from ..archive_payloads import validate_archive_payloads
from ..bundle import BundleIndex, BundleStore
from ..chat_mutations import load_metadata_json
from ..errors import ConflictError, IntegrityError, StorageError, UnknownCommit
from ..projection_publication import _context_usage_projection
from ..projection_references import snapshot_projection
from ..publications import CommitPlan, PublicationRequest, _request
from ..retention import RetentionStore
from ..session_projection import is_projection
from ..shares import ChatShareSnapshot
from ..source_messages import snapshot_sources


@dataclass(frozen=True)
class CommitTicket:
    request: PublicationRequest
    publication_id: int
    relative_path: str
    byte_length: int
    plan_text: str


@dataclass(frozen=True)
class CommitEvidence:
    ticket: CommitTicket
    index: BundleIndex
    import_plan: bytes
    recovery: bytes | None = None
    share: bytes | None = None


def _matches_evidence(row, ticket):
    if (
        row is None
        or row["id"] != ticket.publication_id
        or BundleStore.validate_publication_locator(row)
        != ticket.relative_path
        or row["byte_length"] != ticket.byte_length
        or not row.get("durable_at")
    ):
        raise IntegrityError("offline commit publication changed")


class _EvidenceSources:
    def __init__(self, sources, ticket):
        self.sources, self.ticket = sources, ticket

    def __getattr__(self, name):
        return getattr(self.sources, name)

    async def lock_current(self, store, initial, operation_key):
        row, locked = await self.sources.lock_current(
            store,
            initial,
            operation_key,
        )
        _matches_evidence(row, self.ticket)
        return row, locked


def _allowed_plan(ticket):
    plan = CommitPlan.parse(ticket.plan_text)
    if plan == CommitPlan(result_ids=(ticket.publication_id,)):
        valid = ticket.request.kind == "offline_import"
    elif plan == CommitPlan(recovery_block="recovery"):
        valid = (
            ticket.request.kind == "offline_import"
            and ticket.request.turn_id is not None
        )
    elif plan.share is not None and plan == CommitPlan(share=plan.share):
        valid = ticket.request.kind == "share_import"
    else:
        valid = False
    if not valid:
        raise IntegrityError("plan requires the general publication protocol")
    return plan


def _evidence_plan(evidence):
    plan = _allowed_plan(evidence.ticket)
    if evidence.recovery is None:
        return plan
    return replace(
        plan,
        context_usage_projection=_context_usage_projection(
            load_metadata_json(evidence.recovery),
            plan,
        ),
    )


class OfflineCommitStages:
    """Owner issues tickets; workers read NAS; owner consumes frozen evidence."""

    def __init__(self, store):
        self.store = store
        self.tickets = {}
        self.receipts = {}

    def _ticket(self, ticket):
        known = self.tickets.get(id(ticket))
        if known is None or known[0] is not ticket:
            raise IntegrityError("unknown offline commit ticket")
        return known[1]

    async def _current(self, ticket):
        await self.store.validate_target()
        current = await self.store.get(ticket.request.operation_key)
        self.store._matches(current, ticket.request, ticket.plan_text)
        _matches_evidence(current, ticket)
        return current

    async def prepare(self, operation_key):
        store = self.store
        await store.validate_target()
        row = await store.get(operation_key)
        if row is not None and row["state"] == "committed":
            return row
        if (
            row is None
            or row["state"] != "prepared"
            or not row.get("durable_at")
            or row["byte_length"] is None
        ):
            raise ConflictError(
                "offline commit requires a durable prepared bundle",
            )
        ticket = CommitTicket(
            _request(row),
            row["id"],
            BundleStore.validate_publication_locator(row),
            row["byte_length"],
            row["result_metadata_text"],
        )
        _allowed_plan(ticket)
        await store._validate_prepared_references(row)
        self.tickets[id(ticket)] = (ticket, deepcopy(row))
        return ticket

    async def read(self, ticket, io):
        self._ticket(ticket)
        plan = _allowed_plan(ticket)
        store = self.store

        def read_evidence():
            index = store.bundles.inspect(
                ticket.relative_path,
                expected_length=ticket.byte_length,
            )
            if (
                index.relative_path != ticket.relative_path
                or index.byte_length != ticket.byte_length
                or index.metadata.get("operation_key")
                != ticket.request.operation_key
            ):
                raise IntegrityError("offline commit bundle identity mismatch")

            def block(name, kind):
                extent = index.blocks.get(name)
                if extent is None or extent.kind != kind:
                    raise IntegrityError(
                        "offline commit evidence block missing",
                    )
                return store.bundles.read_extent(
                    index.relative_path,
                    extent.offset,
                    extent.length,
                    expected_length=index.byte_length,
                )

            imported = block("import_plan", "import_plan")
            store._validate_plan(load_metadata_json(imported))
            recovery = share = None
            if plan.recovery_block is not None:
                recovery = block(plan.recovery_block, "recovery")
                value = load_metadata_json(recovery)
                if (
                    is_projection(value)
                    or not isinstance(value, dict)
                    or set(value) != {"turn_states"}
                    or not isinstance(value["turn_states"], dict)
                    or len(value["turn_states"]) != 1
                ):
                    raise IntegrityError(
                        "recovery requires the general projection protocol",
                    )
            if plan.share is not None:
                share = block(plan.share.block_name, "share")
                try:
                    ChatShareSnapshot.model_validate(load_metadata_json(share))
                except (ValueError, TypeError) as error:
                    raise IntegrityError(
                        "share snapshot payload is invalid",
                    ) from error
            return CommitEvidence(ticket, index, imported, recovery, share)

        try:
            evidence = await io.call(read_evidence)
        except asyncio.CancelledError as error:
            raise UnknownCommit(ticket.request.operation_key) from error
        self.receipts[id(evidence)] = (evidence, deepcopy(evidence))
        return evidence

    async def commit(self, evidence):
        from .install_records import _PLAN, _LOCKED

        known = self.receipts.get(id(evidence))
        if known is None or known[0] is not evidence or known[1] != evidence:
            raise IntegrityError("unknown or altered offline commit evidence")
        ticket = evidence.ticket
        initial = self._ticket(ticket)
        frozen = known[1]
        request = ticket.request
        plan = _evidence_plan(frozen)
        store = self.store
        sources = projected = None
        plan_token = locks_token = None
        try:
            current = await self._current(ticket)
            if current["state"] == "committed":
                self.receipts.pop(id(evidence))
                self.tickets.pop(id(ticket))
                return current
            if current["state"] != "prepared":
                raise ConflictError("publication operation is aborted")
            imported = load_metadata_json(frozen.import_plan)
            store._validate_plan(imported)
            plan_token = _PLAN.set(imported)
            locks_token = _LOCKED.set(
                {key: {} for key in ("sessions", "chats", "turns")},
            )
            await store._validate_prepared_references(current)
            # The allowlist excludes message/source and projection plans. Use
            # the canonical snapshots rather than bypassing their contracts.
            sources = await snapshot_sources(store, request, plan)
            projected = await snapshot_projection(
                store,
                request,
                None,
                frozen.index,
                current_id=initial["id"],
            )
            sources.include_projection(projected)
            archive = await validate_archive_payloads(
                store,
                request,
                plan,
                frozen.index,
                sources=sources,
            )
            existing = await store._commit_install(
                request,
                initial,
                plan,
                frozen.index,
                _EvidenceSources(sources, ticket),
                archive,
            )
            result = (
                existing
                if existing is not None
                else await store.get(request.operation_key)
            )
            self.receipts.pop(id(evidence))
            self.tickets.pop(id(ticket))
            return result
        except StorageError:
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(request.operation_key, request) from error
        finally:
            try:
                if sources is not None:
                    await sources.finish(store, request.scope)
            finally:
                try:
                    if projected is not None:
                        await projected.finish(
                            RetentionStore(store.db, request.scope),
                        )
                finally:
                    if locks_token is not None:
                        _LOCKED.reset(locks_token)
                    if plan_token is not None:
                        _PLAN.reset(plan_token)
