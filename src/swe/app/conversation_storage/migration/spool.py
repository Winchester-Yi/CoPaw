# -*- coding: utf-8 -*-
"""Private, bounded temporary inventory records; never a migration authority."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading

from ..chat_mutations import load_metadata_json


class MigrationCapacityError(ValueError):
    """The run cannot fit a record within its declared resource limits."""


class ByteBudget:
    """Reserve weighted in-flight bytes before allocating or submitting work."""

    def __init__(self, capacity):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("byte budget must be a positive integer")
        self.capacity = capacity
        self.used = 0
        self.peak = 0
        self._changed = asyncio.Event()

    @asynccontextmanager
    async def reserve(self, size):
        if type(size) is not int or not 0 <= size <= self.capacity:
            raise MigrationCapacityError(
                "record exceeds migration byte budget",
            )
        while self.used + size > self.capacity:
            changed = self._changed
            await changed.wait()
        self.used += size
        self.peak = max(self.peak, self.used)
        try:
            yield
        finally:
            # Synchronous release/notification cannot be interrupted by cancel.
            self.used -= size
            changed = self._changed
            self._changed = asyncio.Event()
            changed.set()


@dataclass(frozen=True)
class SpoolRecord:
    index: int
    byte_length: int
    digest: str


class InventorySpool:
    """Incrementally encode JSON records with disk and per-record limits.

    File identities are generated integers, not workspace identities. Callers use
    MigrationIO for write/read; this synchronous class owns no SQL or task state.
    It does not make the original parser's allocations bounded by itself.
    """

    def __init__(self, directory, *, max_bytes, record_limit, protected=()):
        base = Path(directory)
        if not base.is_absolute() or base.resolve() != base:
            raise ValueError("work directory must be canonical and absolute")
        if not base.is_dir() or any(
            part.is_symlink() for part in (base, *base.parents)
        ):
            raise ValueError("work directory must exist without symlinks")
        for value in protected:
            root = Path(value).resolve()
            if (
                base == root
                or base.is_relative_to(root)
                or root.is_relative_to(base)
            ):
                raise ValueError("work directory overlaps protected input")
        if (
            type(max_bytes) is not int
            or type(record_limit) is not int
            or not 0 < record_limit <= max_bytes
        ):
            raise ValueError("invalid inventory disk limits")
        self.root = Path(tempfile.mkdtemp(prefix="migration-", dir=base))
        self._max_bytes = max_bytes
        self._record_limit = record_limit
        self._used = 0
        self._records = {}
        self._closed = False
        self._lock = threading.RLock()
        self._readers = 0

    @property
    def used_bytes(self):
        return self._used

    def _encoded_chunks(self, value):
        # iterencode emits an entire string token. Reject large source strings
        # before asking it to allocate an escaped copy of an oversized value.
        remaining = self._record_limit
        pending = [iter((value,))]
        while pending:
            try:
                item = next(pending[-1])
            except StopIteration:
                pending.pop()
                continue
            if isinstance(item, str):
                remaining -= len(item)
                if remaining < 0:
                    raise MigrationCapacityError("inventory record too large")
            elif isinstance(item, dict):
                pending.append(iter(item.keys()))
                pending.append(iter(item.values()))
            elif isinstance(item, (list, tuple)):
                pending.append(iter(item))
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        for chunk in encoder.iterencode(value):
            yield chunk.encode("utf-8")

    def write(self, index, value):
        size = 0
        for raw in self._encoded_chunks(value):
            size += len(raw)
            if size > self._record_limit:
                raise MigrationCapacityError("inventory record too large")
        with self._lock:
            if self._closed:
                raise ValueError("inventory spool is closed")
            if type(index) is not int or index < 0 or index in self._records:
                raise ValueError("inventory record index must be unique")
            if self._used + size > self._max_bytes:
                raise MigrationCapacityError("inventory disk limit exceeded")
            self._records[index] = None
            self._used += size
        destination = self.root / (str(index) + ".json")
        temporary = self.root / (str(index) + ".tmp")
        digest = sha256()
        written = 0
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                for raw in self._encoded_chunks(value):
                    written += len(raw)
                    if written > size:
                        raise ValueError("inventory changed during encoding")
                    handle.write(raw)
                    digest.update(raw)
            if written != size:
                raise ValueError("inventory changed during encoding")
            os.replace(temporary, destination)
            record = SpoolRecord(index, size, digest.hexdigest())
            with self._lock:
                self._records[index] = record
            return record
        except BaseException:
            with self._lock:
                del self._records[index]
                self._used -= size
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, record):
        with self._lock:
            if self._closed or self._records.get(record.index) != record:
                raise ValueError(
                    "inventory record does not belong to this spool",
                )
            self._readers += 1
        try:
            path = self.root / (str(record.index) + ".json")
            with path.open("rb") as handle:
                raw = handle.read(record.byte_length + 1)
            if (
                len(raw) != record.byte_length
                or sha256(raw).hexdigest() != record.digest
            ):
                raise ValueError("temporary inventory record changed")
            return load_metadata_json(raw)
        finally:
            with self._lock:
                self._readers -= 1

    def close(self):
        with self._lock:
            if self._readers or any(
                value is None for value in self._records.values()
            ):
                raise RuntimeError("inventory IO must drain before close")
            if not self._closed:
                shutil.rmtree(self.root)
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
