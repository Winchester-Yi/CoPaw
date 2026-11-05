# -*- coding: utf-8 -*-
"""Explicit checkpoint reads over a fixed high, with page-scoped NAS pins."""

from dataclasses import dataclass, replace

from ...agents.memory.chat_checkpoint import (
    CheckpointEvent,
    CheckpointRecord,
    validate_checkpoint_record,
)
from ...agents.memory.conversation_archive import CheckpointArchiveState
from .bundle import MAX_BUNDLE_BYTES
from .errors import ConflictError, IntegrityError, StorageUnavailable
from .retention import MAX_PIN_BATCH, ReaderPins

# Safety bounds for the compatibility API that materializes a complete array.
# Streaming verification has bounded page memory instead of a complete array.
MAX_CHECKPOINT_EVENTS = 16 * MAX_PIN_BATCH
MAX_CHECKPOINT_BYTES = MAX_BUNDLE_BYTES


@dataclass(frozen=True)
class CheckpointSnapshot:
    chat_id: str
    chat_row_id: int
    session_id: int
    epoch: int
    revision: int
    active_publication_id: int | None
    high: int
    watermark: int


@dataclass(frozen=True)
class CheckpointCursor:
    snapshot: CheckpointSnapshot
    record: CheckpointRecord
    after_sequence: int
    record_bytes: int = 0


@dataclass(frozen=True)
class CheckpointPage:
    record: CheckpointRecord
    events: tuple[CheckpointEvent, ...]
    current_epoch: int
    snapshot: CheckpointSnapshot
    has_more: bool
    next_cursor: CheckpointCursor | None
    bytes_read: int


def _decode(cls, value):
    try:
        return cls.from_dict(value)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise IntegrityError(
            "indexed checkpoint body schema is invalid",
        ) from error


def _matches(snapshot, chat):
    current = (
        chat["id"],
        chat["session_id"],
        chat["epoch"],
        chat["checkpoint_revision"],
        chat["active_checkpoint_publication_id"],
    )
    expected = (
        snapshot.chat_row_id,
        snapshot.session_id,
        snapshot.epoch,
        snapshot.revision,
        snapshot.active_publication_id,
    )
    return current == expected and chat["event_high"] >= snapshot.high


class CheckpointReads:
    """ArchiveReader mixin; its live retention adapter also supports offline use."""

    async def _checkpoint_payload(self, pins, publication, prefix):
        try:
            return await self.payload(pins, publication, prefix)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise IntegrityError(
                "indexed checkpoint JSON is invalid",
            ) from error

    @staticmethod
    def _checkpoint_association(publication, snapshot):
        if (
            publication["chat_id"] != snapshot.chat_row_id
            or publication["session_id"] != snapshot.session_id
            or publication["epoch"] != snapshot.epoch
        ):
            raise IntegrityError("checkpoint publication association mismatch")

    async def _checkpoint_current(self, snapshot):
        try:
            chat = await self.chat(snapshot.chat_id)
        except KeyError as error:
            raise ConflictError(
                "checkpoint Chat disappeared during read",
            ) from error
        if not _matches(snapshot, chat):
            raise ConflictError("checkpoint changed during read")
        return chat

    async def _checkpoint_start(
        self,
        chat_id,
        expected=None,
        *,
        max_bytes=MAX_CHECKPOINT_BYTES,
    ):
        pins = ReaderPins(self.retentions)
        try:
            async with self.db.transaction():
                chat = await self.chat(chat_id)
                if expected is not None and any(
                    chat[key] != expected[key]
                    for key in (
                        "epoch",
                        "checkpoint_revision",
                        "active_checkpoint_publication_id",
                    )
                    if key in expected
                ):
                    raise ConflictError("checkpoint snapshot changed")
                publication = None
                pub_id = chat["active_checkpoint_publication_id"]
                if pub_id is not None:
                    publication = await self.db.fetch_one(
                        "SELECT * FROM swe_conversation_publications WHERE id=%s AND service_unit_key=%s",
                        (pub_id, self.scope.sql_values[0]),
                    )
                    if publication is None:
                        raise IntegrityError(
                            "active checkpoint publication is missing",
                        )
                    if publication["checkpoint_length"] > max_bytes:
                        raise StorageUnavailable(
                            "checkpoint read exceeds byte budget",
                        )
                    await pins.pin([pub_id])
            watermark = (
                publication["checkpoint_event_watermark"] if publication else 0
            )
            if (
                type(watermark) is not int
                or not 0 <= watermark <= chat["event_high"]
            ):
                raise IntegrityError(
                    "checkpoint watermark exceeds captured high",
                )
            snapshot = CheckpointSnapshot(
                chat_id,
                chat["id"],
                chat["session_id"],
                chat["epoch"],
                chat["checkpoint_revision"],
                pub_id,
                chat["event_high"],
                watermark,
            )
            record = CheckpointRecord.new(
                chat_id=chat_id,
                epoch=snapshot.epoch,
            )
            size = 0
            if publication is not None:
                self._checkpoint_association(publication, snapshot)
                record = _decode(
                    CheckpointRecord,
                    await self._checkpoint_payload(
                        pins,
                        publication,
                        "checkpoint",
                    ),
                )
                size = publication["checkpoint_length"]
            if (
                record.chat_id != chat_id
                or record.epoch != snapshot.epoch
                or record.revision != snapshot.revision
                or record.applied_event_sequence != watermark
                or not validate_checkpoint_record(record).is_valid
            ):
                raise IntegrityError(
                    "indexed checkpoint body does not match current metadata",
                )
            await self._checkpoint_current(snapshot)
            return CheckpointCursor(snapshot, record, watermark, size)
        finally:
            await pins.finish()

    async def checkpoint_record(self, chat_id, snapshot=None):
        """Read only the active checkpoint, never event locators or bodies."""
        return (await self._checkpoint_start(chat_id, snapshot)).record

    @staticmethod
    def _checkpoint_limit(limit):
        if type(limit) is not int or not 0 < limit <= MAX_PIN_BATCH:
            raise ValueError("checkpoint page must contain 1..4096 events")

    def _checkpoint_events(self, publication, values, cursor):
        first, last = publication["event_first"], publication["event_last"]
        if not isinstance(values, list) or len(values) != last - first + 1:
            raise IntegrityError("indexed checkpoint event body range differs")
        events = tuple(_decode(CheckpointEvent, value) for value in values)
        if any(
            event.sequence != first + i or event.epoch != cursor.snapshot.epoch
            for i, event in enumerate(events)
        ):
            raise IntegrityError("indexed checkpoint event sequence differs")
        # Validate the entire extent, including facts outside the returned page.
        validation_record = replace(
            cursor.record,
            applied_event_sequence=first - 1,
        )
        if not validate_checkpoint_record(validation_record, events).is_valid:
            raise IntegrityError("checkpoint event facts are invalid")
        return events

    async def checkpoint_page(
        self,
        chat_id,
        *,
        cursor=None,
        limit=4096,
        max_bytes=MAX_CHECKPOINT_BYTES,
    ):
        """Continue a fixed epoch/revision/active/high, allowing later appends."""
        return await self._checkpoint_page(
            chat_id,
            cursor=cursor,
            limit=limit,
            max_bytes=max_bytes,
        )

    async def _checkpoint_page(
        self,
        chat_id,
        *,
        cursor=None,
        limit=4096,
        cache=None,
        max_bytes=MAX_CHECKPOINT_BYTES,
    ):
        self._checkpoint_limit(limit)
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError(
                "checkpoint byte budget must be a nonnegative integer",
            )
        initial = cursor is None
        cursor = (
            await self._checkpoint_start(chat_id, max_bytes=max_bytes)
            if initial
            else cursor
        )
        if (
            not isinstance(cursor, CheckpointCursor)
            or cursor.snapshot.chat_id != chat_id
        ):
            raise ValueError("checkpoint cursor belongs to another Chat")
        snapshot = cursor.snapshot
        after = cursor.after_sequence
        if not snapshot.watermark <= after <= snapshot.high:
            raise ValueError("checkpoint cursor is outside captured high")
        upper = min(snapshot.high, after + limit)
        size = cursor.record_bytes if initial else 0
        pins = ReaderPins(self.retentions)
        try:
            async with self.db.transaction():
                await self._checkpoint_current(snapshot)
                rows = []
                if after < upper:
                    rows = await self.db.fetch_all(
                        "SELECT * FROM swe_conversation_publications WHERE chat_id=%s AND epoch=%s AND state='committed' AND event_last>%s AND event_first<=%s ORDER BY event_last,id LIMIT %s",
                        (
                            snapshot.chat_row_id,
                            snapshot.epoch,
                            after,
                            upper,
                            limit + 1,
                        ),
                    )
                    if len(rows) > upper - after:
                        raise IntegrityError(
                            "checkpoint event publications overlap",
                        )
                    needed = sum(
                        row["event_length"]
                        for row in rows
                        if cache is None
                        or self._checkpoint_cache_key(row) not in cache
                    )
                    if size + needed > max_bytes:
                        raise StorageUnavailable(
                            "checkpoint read exceeds byte budget",
                        )
                    await pins.pin([row["id"] for row in rows])
            events = []
            expected = after + 1
            for index, row in enumerate(rows):
                self._checkpoint_association(row, snapshot)
                first, last = row["event_first"], row["event_last"]
                if (
                    first > expected
                    or last < expected
                    or last > snapshot.high
                    or (index and first != expected)
                ):
                    raise IntegrityError(
                        "indexed checkpoint event range is discontinuous",
                    )
                key = self._checkpoint_cache_key(row)
                values = cache.get(key) if cache is not None else None
                if values is None:
                    values = self._checkpoint_events(
                        row,
                        await self._checkpoint_payload(pins, row, "event"),
                        cursor,
                    )
                    size += row["event_length"]
                    if cache is not None:
                        cache.clear()
                        cache[key] = values
                events.extend(
                    event
                    for event in values
                    if after < event.sequence <= upper
                )
                expected = last + 1
            if len(events) != upper - after:
                raise IntegrityError("checkpoint event page is incomplete")
            await self._checkpoint_current(snapshot)
            more = upper < snapshot.high
            return CheckpointPage(
                cursor.record,
                tuple(events),
                snapshot.epoch,
                snapshot,
                more,
                replace(cursor, after_sequence=upper) if more else None,
                size,
            )
        finally:
            await pins.finish()

    @staticmethod
    def _checkpoint_cache_key(row):
        path = row["relative_path"]
        return (
            row["id"],
            bytes(path) if isinstance(path, bytearray) else path,
            row["event_offset"],
            row["event_length"],
            row["byte_length"],
            row["event_first"],
            row["event_last"],
        )

    async def checkpoint_prefix(self, chat_id, limit):
        """Return a bounded prefix and explicitly indicate remaining events."""
        return await self.checkpoint_page(chat_id, limit=limit)

    async def iter_checkpoint_pages(self, chat_id, *, page_size=4096):
        """Only normal exhaustion proves traversal of the complete captured high."""
        cursor, cache = None, {}
        while True:
            page = await self._checkpoint_page(
                chat_id,
                cursor=cursor,
                limit=page_size,
                cache=cache,
            )
            yield page
            if not page.has_more:
                await self._checkpoint_current(page.snapshot)
                return
            cursor = page.next_cursor

    async def checkpoint_full(
        self,
        chat_id,
        *,
        max_events=MAX_CHECKPOINT_EVENTS,
        max_bytes=MAX_CHECKPOINT_BYTES,
    ):
        """Materialize the full captured tail, or fail explicitly on a budget."""
        if any(
            type(value) is not int or value < 0
            for value in (max_events, max_bytes)
        ):
            raise ValueError(
                "checkpoint full budgets must be nonnegative integers",
            )
        cursor = await self._checkpoint_start(chat_id, max_bytes=max_bytes)
        if cursor.snapshot.high - cursor.snapshot.watermark > max_events:
            raise StorageUnavailable(
                "checkpoint full read exceeds event budget",
            )
        events, size, cache = [], cursor.record_bytes, {}
        while True:
            page = await self._checkpoint_page(
                chat_id,
                cursor=cursor,
                cache=cache,
                max_bytes=max_bytes - size,
            )
            size += page.bytes_read
            events.extend(page.events)
            if not page.has_more:
                return CheckpointArchiveState(
                    page.record,
                    tuple(events),
                    page.current_epoch,
                )
            cursor = page.next_cursor

    async def checkpoint(self, chat_id, limit=None):
        """Compatibility: no limit means complete; explicit limits expose a prefix."""
        if limit is not None:
            return await self.checkpoint_prefix(chat_id, limit)
        return await self.checkpoint_full(chat_id)
