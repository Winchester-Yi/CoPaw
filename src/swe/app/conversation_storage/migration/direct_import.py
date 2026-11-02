# -*- coding: utf-8 -*-
"""Import frozen v3 sources directly into manually prepared application tables."""

from dataclasses import dataclass
import re

from ..errors import IntegrityError
from .importer import ConversationImporter
from .install_records import OfflineImportStore
from .journal import MigrationJournal


@dataclass(frozen=True)
class DirectTarget:
    schema: str
    application_schema: str
    migration_id: str

    def __post_init__(self):
        if self.schema != self.application_schema or not re.fullmatch(
            r"[a-zA-Z0-9_]{1,64}",
            self.schema,
        ):
            raise ValueError(
                "direct migration requires the application schema",
            )
        if not self.migration_id:
            raise ValueError("migration identity is required")


class DirectImportStore(OfflineImportStore):
    def __init__(self, database, bundles, target, *, service_unit_id, journal):
        super().__init__(
            database,
            bundles,
            target,
            service_unit_id=service_unit_id,
        )
        self.journal = journal
        self.batch_positions = {}

    async def reserve_batch(self, request, publication_id, plan=None):
        assignment = self.journal.manifest.data["assignments"]["publication"]
        positions = [
            key
            for key, value in assignment.items()
            if value["target_id"] == publication_id
            and value["operation_key"] == request.operation_key
        ]
        if len(positions) != 1:
            raise IntegrityError(
                "publication reservation has no durable SQL ticket",
            )
        self.batch_positions[request.operation_key] = positions[0]
        return await super().reserve_batch(request, publication_id, plan)

    async def verify_batch(self, request, publication_id, plan, expected):
        row = await super().verify_batch(
            request,
            publication_id,
            plan,
            expected,
        )
        if plan.share is not None:
            index = await self._nas_io(
                request.operation_key,
                self.bundles.inspect,
                row["relative_path"].decode(),
                expected_length=row["byte_length"],
            )
            actual = await self.db.fetch_one(
                "SELECT * FROM swe_chat_shares WHERE token=%s",
                (plan.share.token,),
            )
            extent = index.blocks.get(plan.share.block_name)
            if extent is None or actual is None:
                raise IntegrityError(
                    "committed imported share locator is missing",
                )
            locator = (
                request.scope.sql_values[0],
                publication_id,
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
                raise IntegrityError(
                    "committed imported share locator changed",
                )
            await self.journal.record_share(plan.share.token, before, locator)
        return row

    async def _install(self, row, plan, index, **kwargs):
        await super()._install(row, plan, index, **kwargs)
        operation = row["operation_key"].decode()
        await self.journal.set_batch_state(
            self.batch_positions[operation],
            "committed",
            mirror=False,
        )

    async def _install_share(self, request, plan, row, index):
        await self._verify_share_record(plan)
        actual = await self.db.fetch_one(
            "SELECT * FROM swe_chat_shares WHERE token=%s FOR UPDATE",
            (plan.token,),
        )
        if actual is None:
            raise IntegrityError("original share disappeared")
        # Compare again after taking the row lock, including collation-equivalent tokens.
        from ..chat_mutations import utc_datetime

        for key in (
            "token",
            "chat_id",
            "creator_id",
            "tenant_id",
            "snapshot_key",
            "created_at",
            "access_count",
            "last_access_at",
        ):
            expected = getattr(plan, key)
            if (
                key in ("created_at", "last_access_at")
                and expected is not None
            ):
                expected = utc_datetime(expected)
            if actual[key] != expected:
                raise IntegrityError(
                    "original share identity or counter changed",
                )
        extent = index.blocks.get(plan.block_name)
        if extent is None:
            raise IntegrityError("share import block is missing")
        expected = (
            request.scope.sql_values[0],
            row["id"],
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
        await self.journal.record_share(plan.token, before, expected)
        if before == expected:
            return
        if any(value is not None for value in before):
            raise IntegrityError("unknown existing share extension values")
        changed = await self.db.execute(
            "UPDATE swe_chat_shares SET service_unit_key=%s,publication_id=%s,byte_offset=%s,byte_length=%s WHERE token=%s",
            (*expected, plan.token),
        )
        if changed != 1:
            raise IntegrityError("share CAS affected an unexpected row count")


class DirectImporter(ConversationImporter):
    def __init__(
        self,
        database,
        bundles,
        manifest,
        target,
        *,
        batch_size=128,
        journal=None,
    ):
        self.journal = journal or MigrationJournal(database, manifest)
        super().__init__(
            database,
            bundles,
            manifest,
            target,
            batch_size=batch_size,
            store_factory=lambda *args, **kwargs: DirectImportStore(
                *args,
                **kwargs,
                journal=self.journal,
            ),
        )

    async def _allocate(self, kind, position):
        return await self.journal.allocate(kind, position)

    async def _set_batch_state(self, position, state):
        await self.journal.set_batch_state(position, state)

    async def _batch_finished(self, position):
        # SQL completion was persisted with publication commit; mirror only now.
        await self.journal.hydrate()

    async def run(self, report):
        await self.journal.prepare()
        await self.journal.mark_state("importing")
        result = await super().run(report)
        await self.journal.capture_retentions()
        await self.journal.mark_state("imported")
        return result
