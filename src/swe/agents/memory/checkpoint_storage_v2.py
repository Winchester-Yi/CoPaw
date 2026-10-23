# -*- coding: utf-8 -*-
"""Versioned checkpoint roots and bounded, checksummed journal segments.

All methods are synchronous and require the caller's Chat flock. Snapshots
reference immutable metadata and committed prefixes, never directory listings.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator

from swe.performance_metrics import increment, measure_storage_operation
from .chat_checkpoint import (
    CheckpointEvent,
    CheckpointRecord,
    validate_checkpoint_record,
)

ROOT_NAME = "state-v2.json"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_directory(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        sync_directory(directory.parent)
        sync_directory(directory)


def atomic_bytes(path: Path, payload: bytes) -> None:
    make_directory(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CheckpointStorageV2:
    MAX_SEGMENT_BYTES = 1024 * 1024
    MAX_SEGMENT_EVENTS = 512
    MAX_EVENT_BYTES = 64 * 1024

    def __init__(self, chat_dir: Path):
        self.chat_dir = Path(chat_dir)
        self.root_path = self.chat_dir / ROOT_NAME
        self.quarantine_path = self.chat_dir / "journal-v2-quarantine.json"

    @property
    def exists(self) -> bool:
        return self.root_path.exists()

    def check_writable(self) -> None:
        if self.quarantine_path.exists():
            raise ValueError(
                "Checkpoint journal is quarantined; maintenance required",
            )

    def quarantine(self, kind: str, ordinal: int = -1) -> None:
        try:
            atomic_bytes(
                self.quarantine_path,
                canonical_bytes({"kind": kind, "ordinal": ordinal}),
            )
        except OSError as exc:
            raise ValueError(
                "Checkpoint journal quarantine failed; stop tenant writes",
            ) from exc

    def head(self) -> dict[str, Any]:
        try:
            payload = self.root_path.read_bytes()
            increment("checkpoint_read_calls")
            increment("checkpoint_read_bytes", len(payload))
            value = json.loads(payload)
        except (OSError, TypeError, ValueError) as exc:
            self.quarantine("invalid-root")
            raise ValueError("Checkpoint journal root is invalid") from exc
        return self.validate_head(value)

    def validate_head(self, value: Any) -> dict[str, Any]:
        """Validate roots loaded directly or through a historical reference."""
        try:
            if type(value["version"]) is not int or value["version"] != 2:
                raise ValueError("unsupported version")
            if str(uuid.UUID(value["chat_id"])) != value["chat_id"]:
                raise ValueError("invalid Chat identity")
            for key in (
                "epoch",
                "last_event_sequence",
                "active_segment_ordinal",
                "committed_bytes",
                "sealed_segment_count",
                "applied_event_sequence",
                "checkpoint_revision",
                "active_count",
                "active_first_sequence",
            ):
                if type(value[key]) is not int or value[key] < 0:
                    raise ValueError("invalid head counter")
            if (
                value["epoch"] < 1
                or value["sealed_segment_count"]
                != value["active_segment_ordinal"]
            ):
                raise ValueError("invalid epoch or segment watermark")
            if (
                value["active_first_sequence"] + value["active_count"] - 1
                != value["last_event_sequence"]
            ):
                raise ValueError("invalid active sequence watermark")
            if value["applied_event_sequence"] > value["last_event_sequence"]:
                raise ValueError("checkpoint watermark exceeds journal")
            if value["active_first_sequence"] < 1 or (
                bool(value["active_count"]) != bool(value["committed_bytes"])
            ):
                raise ValueError("invalid active segment size")
            for key in ("checkpoint", "manifest", "evidence", "epochs"):
                if not isinstance(value[key], dict):
                    raise ValueError("invalid metadata reference")
            if value["previous_epoch"] is not None and not isinstance(
                value["previous_epoch"],
                dict,
            ):
                raise ValueError("invalid epoch reference")
            return value
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            self.quarantine("invalid-root")
            raise ValueError("Checkpoint journal root is invalid") from exc

    def _object(self, value: Any) -> dict[str, Any]:
        payload = canonical_bytes(value)
        path = self.chat_dir / "objects-v2" / (uuid.uuid4().hex + ".json")
        atomic_bytes(path, payload)
        return {
            "path": str(path.relative_to(self.chat_dir)),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def read_object(self, ref: dict[str, Any]) -> Any:
        try:
            relative = Path(ref["path"])
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.parts[0] not in ("objects-v2", "epochs-v2")
            ):
                raise ValueError("unsafe object reference")
            payload = (self.chat_dir / relative).read_bytes()
            increment("checkpoint_read_bytes", len(payload))
            if (
                len(payload) != ref["bytes"]
                or hashlib.sha256(payload).hexdigest() != ref["sha256"]
            ):
                raise ValueError("object hash mismatch")
            return json.loads(payload)
        except (OSError, IndexError, KeyError, TypeError, ValueError) as exc:
            self.quarantine("invalid-metadata")
            raise ValueError("Checkpoint journal metadata is invalid") from exc

    def record(self, head: dict[str, Any]) -> CheckpointRecord:
        record = CheckpointRecord.from_dict(
            self.read_object(head["checkpoint"]),
        )
        if (
            record.chat_id != head["chat_id"]
            or record.epoch != head["epoch"]
            or record.revision != head["checkpoint_revision"]
            or record.applied_event_sequence != head["applied_event_sequence"]
            or not validate_checkpoint_record(record).is_valid
        ):
            self.quarantine("invalid-checkpoint")
            raise ValueError("Checkpoint journal record disagrees with root")
        return record

    @measure_storage_operation("checkpoint_commit")
    def initialize(
        self,
        record: CheckpointRecord,
        manifest: dict,
        evidence: dict,
        epochs: dict,
    ) -> None:
        self.check_writable()
        if self.exists:
            raise ValueError("Checkpoint journal already exists")
        head = {
            "version": 2,
            "chat_id": record.chat_id,
            "epoch": record.epoch,
            "last_event_sequence": 0,
            "active_segment_ordinal": 0,
            "sealed_segment_count": 0,
            "committed_bytes": 0,
            "active_count": 0,
            "active_first_sequence": 1,
            "applied_event_sequence": record.applied_event_sequence,
            "checkpoint_revision": record.revision,
            "checkpoint": self._object(record.to_dict()),
            "manifest": self._object(manifest),
            "evidence": self._object(evidence),
            "epochs": self._object(epochs),
            "previous_epoch": None,
        }
        make_directory(self.segment_path(head).parent)
        atomic_bytes(self.segment_path(head), b"")
        self._publish(head)

    def _publish(self, head: dict[str, Any]) -> None:
        self.check_writable()
        atomic_bytes(self.root_path, canonical_bytes(head))

    @measure_storage_operation("checkpoint_commit")
    def publish(
        self,
        *,
        record: CheckpointRecord | None = None,
        manifest: dict | None = None,
        evidence: dict | None = None,
        epochs: dict | None = None,
    ) -> dict[str, Any]:
        self.check_writable()
        head = self.head()
        if record is not None:
            if (
                record.chat_id != head["chat_id"]
                or record.epoch != head["epoch"]
                or not validate_checkpoint_record(record).is_valid
            ):
                raise ValueError("Invalid checkpoint epoch or record")
            if record.applied_event_sequence > head["last_event_sequence"]:
                raise ValueError("Checkpoint watermark exceeds journal")
            head.update(
                checkpoint=self._object(record.to_dict()),
                applied_event_sequence=record.applied_event_sequence,
                checkpoint_revision=record.revision,
            )
        for key, value in (
            ("manifest", manifest),
            ("evidence", evidence),
            ("epochs", epochs),
        ):
            if value is not None:
                head[key] = self._object(value)
        self._publish(head)
        return head

    def segment_path(
        self,
        head: dict[str, Any],
        ordinal: int | None = None,
    ) -> Path:
        if ordinal is None:
            ordinal = head["active_segment_ordinal"]
        return (
            self.chat_dir
            / "epochs-v2"
            / str(head["epoch"])
            / f"{ordinal:012d}.jsonl"
        )

    def _sidecar(self, head: dict[str, Any], ordinal: int) -> dict:
        path = self.segment_path(head, ordinal).with_suffix(".meta.json")
        try:
            value = json.loads(path.read_bytes())
            for key in (
                "ordinal",
                "count",
                "bytes",
                "first_sequence",
                "last_sequence",
            ):
                if type(value[key]) is not int or value[key] < 0:
                    raise ValueError("invalid segment metadata")
            if (
                value["ordinal"] != ordinal
                or value["count"] < 1
                or value["bytes"] < 1
                or value["first_sequence"] < 1
                or value["last_sequence"]
                != value["first_sequence"] + value["count"] - 1
            ):
                raise ValueError("invalid segment metadata")
            return value
        except (OSError, KeyError, TypeError, ValueError) as exc:
            self.quarantine("invalid-sidecar", ordinal)
            raise ValueError("Checkpoint journal sidecar is invalid") from exc

    def _segment_events(
        self,
        head: dict,
        ordinal: int,
        byte_count: int,
        expected: int,
        count: int,
        digest: str | None = None,
    ) -> Iterator[CheckpointEvent]:
        path = self.segment_path(head, ordinal)
        seen = 0
        consumed = 0
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while consumed < byte_count:
                    # Migrated oversized frames are bounded by committed segment size.
                    line = handle.readline(byte_count - consumed + 1)
                    if (
                        not line.endswith(b"\n")
                        or len(line) > byte_count - consumed
                    ):
                        raise ValueError("incomplete committed frame")
                    consumed += len(line)
                    hasher.update(line)
                    increment("journal_read_bytes", len(line))
                    frame = json.loads(line)
                    payload = canonical_bytes(frame["payload"])
                    if (
                        len(payload) != frame["length"]
                        or hashlib.sha256(payload).hexdigest()
                        != frame["sha256"]
                    ):
                        raise ValueError("frame hash mismatch")
                    event = CheckpointEvent.from_dict(frame["payload"])
                    if (
                        event.sequence != expected + seen
                        or event.epoch != head["epoch"]
                    ):
                        raise ValueError("sequence or epoch mismatch")
                    seen += 1
                    increment("journal_frames_decoded")
                    yield event
            if (
                seen != count
                or consumed != byte_count
                or (digest is not None and hasher.hexdigest() != digest)
            ):
                raise ValueError("segment count or hash mismatch")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            self.quarantine("invalid-frame", ordinal)
            raise ValueError(
                "Checkpoint journal committed segment is invalid",
            ) from exc

    def events(
        self,
        head: dict,
        after_sequence: int = 0,
        through_sequence: int | None = None,
    ) -> Iterator[CheckpointEvent]:
        end = (
            head["last_event_sequence"]
            if through_sequence is None
            else min(through_sequence, head["last_event_sequence"])
        )
        if end <= after_sequence:
            return
        low, high = 0, head["sealed_segment_count"]
        while low < high:
            middle = (low + high) // 2
            if self._sidecar(head, middle)["last_sequence"] <= after_sequence:
                low = middle + 1
            else:
                high = middle
        expected_first = (
            self._sidecar(head, low - 1)["last_sequence"] + 1 if low else 1
        )
        for ordinal in range(low, head["active_segment_ordinal"] + 1):
            if ordinal < head["sealed_segment_count"]:
                meta = self._sidecar(head, ordinal)
                size, count, first, digest = (
                    meta["bytes"],
                    meta["count"],
                    meta["first_sequence"],
                    meta["sha256"],
                )
            else:
                size, count, first, digest = (
                    head["committed_bytes"],
                    head["active_count"],
                    head["active_first_sequence"],
                    None,
                )
            if first != expected_first:
                self.quarantine("segment-sequence-gap", ordinal)
                raise ValueError("Checkpoint journal segment sequence gap")
            expected_first = first + count
            if first > end:
                break
            for event in self._segment_events(
                head,
                ordinal,
                size,
                first,
                count,
                digest,
            ):
                if event.sequence > end:
                    return
                if event.sequence > after_sequence:
                    yield event

    @measure_storage_operation("checkpoint_recovery")
    def _recover_active_segment(self, head: dict) -> Path:
        """Verify the committed prefix before truncating an unpublished suffix."""
        for _ in self._segment_events(
            head,
            head["active_segment_ordinal"],
            head["committed_bytes"],
            head["active_first_sequence"],
            head["active_count"],
        ):
            pass
        path = self.segment_path(head)
        with path.open("r+b") as handle:
            handle.truncate(head["committed_bytes"])
            handle.flush()
            os.fsync(handle.fileno())
        return path

    @measure_storage_operation("checkpoint_commit")
    def append(
        self,
        event: CheckpointEvent,
        *,
        allow_oversized: bool = False,
    ) -> tuple:
        self.check_writable()
        head = self.head()
        if event.epoch != head["epoch"]:
            raise ValueError("Checkpoint event epoch is not current")
        payload = canonical_bytes(event.to_dict())
        if len(payload) > self.MAX_EVENT_BYTES and not allow_oversized:
            raise ValueError("Checkpoint event exceeds 64 KiB")
        validation = validate_checkpoint_record(
            CheckpointRecord.new(chat_id="journal", epoch=event.epoch),
            (event,),
        )
        if not validation.is_valid:
            raise ValueError(
                "Invalid checkpoint event: " + "; ".join(validation.errors),
            )
        receipt = (
            event.epoch,
            event.sequence,
            event.id,
            hashlib.sha256(payload).hexdigest(),
        )
        if event.sequence <= head["last_event_sequence"]:
            for previous in self.events(
                head,
                event.sequence - 1,
                event.sequence,
            ):
                previous_hash = hashlib.sha256(
                    canonical_bytes(previous.to_dict()),
                ).hexdigest()
                if previous.id == event.id and previous_hash == receipt[3]:
                    return (
                        previous.epoch,
                        previous.sequence,
                        previous.id,
                        previous_hash,
                    )
            raise ValueError("Checkpoint conditional append conflict")
        if event.sequence != head["last_event_sequence"] + 1:
            raise ValueError("Checkpoint event sequence must be contiguous")
        path = self._recover_active_segment(head)
        frame = (
            canonical_bytes(
                {
                    "payload": event.to_dict(),
                    "length": len(payload),
                    "sha256": receipt[3],
                },
            )
            + b"\n"
        )
        if head["active_count"] and (
            head["committed_bytes"] + len(frame) > self.MAX_SEGMENT_BYTES
            or head["active_count"] >= self.MAX_SEGMENT_EVENTS
        ):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            meta = {
                "ordinal": head["active_segment_ordinal"],
                "bytes": head["committed_bytes"],
                "count": head["active_count"],
                "first_sequence": head["active_first_sequence"],
                "last_sequence": head["last_event_sequence"],
                "sha256": digest,
            }
            atomic_bytes(path.with_suffix(".meta.json"), canonical_bytes(meta))
            head["active_segment_ordinal"] += 1
            head["sealed_segment_count"] += 1
            head.update(
                committed_bytes=0,
                active_count=0,
                active_first_sequence=event.sequence,
            )
            path = self.segment_path(head)
            atomic_bytes(path, b"")
        # Stale unpublished sidecars must never describe this active segment.
        path.with_suffix(".meta.json").unlink(missing_ok=True)
        with path.open("r+b", buffering=0) as handle:
            handle.seek(head["committed_bytes"])
            remaining = memoryview(frame)
            while remaining:
                written = handle.write(remaining)
                if not written:
                    raise OSError("short checkpoint journal write")
                remaining = remaining[written:]
            os.fsync(handle.fileno())
        increment("journal_append_bytes", len(frame))
        head["committed_bytes"] += len(frame)
        head["active_count"] += 1
        head["last_event_sequence"] = event.sequence
        self._publish(head)
        return receipt

    @measure_storage_operation("checkpoint_commit")
    def reset(self, record: CheckpointRecord, reason: str) -> None:
        self.check_writable()
        old = self.head()
        if record.epoch != old["epoch"] + 1 or not reason:
            raise ValueError("Invalid checkpoint reset")
        frozen = (
            self.chat_dir / "epochs-v2" / str(old["epoch"]) / "final-root.json"
        )
        payload = canonical_bytes(old)
        atomic_bytes(frozen, payload)
        epochs = self.read_object(old["epochs"])
        history = list(epochs.get("history", []))
        history.append({"epoch": record.epoch, "reason": reason})
        head = dict(old)
        head.update(
            epoch=record.epoch,
            last_event_sequence=0,
            active_segment_ordinal=0,
            sealed_segment_count=0,
            committed_bytes=0,
            active_count=0,
            active_first_sequence=1,
            applied_event_sequence=record.applied_event_sequence,
            checkpoint_revision=record.revision,
            checkpoint=self._object(record.to_dict()),
            epochs=self._object(
                {"current_epoch": record.epoch, "history": history},
            ),
            previous_epoch={
                "path": str(frozen.relative_to(self.chat_dir)),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        )
        atomic_bytes(self.segment_path(head), b"")
        self._publish(head)

    def previous_head(self, head: dict) -> dict | None:
        ref = head.get("previous_epoch")
        if ref is None:
            return None
        previous = self.validate_head(self.read_object(ref))
        if (
            previous["chat_id"] != head["chat_id"]
            or previous["epoch"] != head["epoch"] - 1
        ):
            self.quarantine("epoch-chain-gap")
            raise ValueError("Checkpoint journal epoch chain is invalid")
        return previous
