# -*- coding: utf-8 -*-
"""Disposable disk indexes and incremental JSON for large direct manifests."""

from collections.abc import MutableMapping, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import weakref
import json
import sqlite3
import tempfile
import threading

from ..chat_mutations import _unique_fields, _reject_nonfinite
from .spool import MigrationCapacityError

_INDEX_POLICY = ContextVar("migration_disk_index_policy", default=None)


class IndexPolicy:
    """Aggregate SQLite page budget; private indexes share one storage policy."""

    def __init__(self, directory, max_bytes):
        root = Path(directory)
        if (
            not root.is_absolute()
            or root.resolve() != root
            or not root.is_dir()
        ):
            raise ValueError(
                "index directory must be canonical, absolute and existing",
            )
        if any(part.is_symlink() for part in (root, *root.parents)):
            raise ValueError("index directory cannot traverse symlinks")
        if type(max_bytes) is not int or max_bytes < 65536:
            raise ValueError("index disk budget must be at least 65536 bytes")
        self.directory, self.max_bytes = root, max_bytes
        self._maps = {}
        self.lock = threading.RLock()
        self.peak_bytes = 0

    def register(self, mapping):
        self._maps[id(mapping)] = weakref.ref(mapping)

    def used_bytes(self, excluding=None):
        total = 0
        for reference in tuple(self._maps.values()):
            mapping = reference()
            if (
                mapping is not None
                and mapping is not excluding
                and mapping._db is not None
            ):
                total += (
                    mapping._db.execute("PRAGMA page_count").fetchone()[0]
                    * 4096
                )
        return total

    def limit(self, mapping):
        available = self.max_bytes - self.used_bytes(excluding=mapping)
        pages = available // 4096
        if pages < 3:
            raise MigrationCapacityError(
                "migration control index disk budget exceeded",
            )
        mapping._db.execute("PRAGMA max_page_count=" + str(pages))

    def close(self):
        for reference in tuple(self._maps.values()):
            mapping = reference()
            if mapping is not None:
                mapping.close()
        self._maps.clear()


@contextmanager
def disk_index_policy(directory, max_bytes):
    """Call after protected-root validation; serialize reports before exiting."""
    policy = IndexPolicy(directory, max_bytes)
    token = _INDEX_POLICY.set(policy)
    try:
        yield policy
    finally:
        _INDEX_POLICY.reset(token)
        policy.close()


class DiskMap(MutableMapping):
    """JSON-valued mapping whose decoded values live only for each lookup."""

    def __init__(self):
        self._policy = _INDEX_POLICY.get()
        # The private directory lifetime follows this mapping, not the reader.
        # pylint: disable-next=consider-using-with
        self._directory = tempfile.TemporaryDirectory(
            prefix="migration-index-",
            dir=self._policy.directory if self._policy is not None else None,
        )
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            self._directory.name + "/index.sqlite",
            check_same_thread=False,
        )
        self._db.execute("PRAGMA page_size=4096")
        self._db.execute("PRAGMA journal_mode=OFF")
        self._db.execute("PRAGMA cache_size=-64")
        self._db.execute("PRAGMA synchronous=OFF")
        if self._policy is not None:
            self._policy.register(self)
            self._policy.limit(self)
        self._db.execute(
            "CREATE TABLE entries (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        )

    def __getitem__(self, key):
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM entries WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def __setitem__(self, key, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with (
            self._lock,
            self._policy.lock if self._policy is not None else self._lock,
        ):
            if self._policy is not None:
                self._policy.limit(self)
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO entries VALUES (?,?)",
                    (key, raw),
                )
            except sqlite3.OperationalError as error:
                if "full" in str(error).lower():
                    raise MigrationCapacityError(
                        "migration control index disk budget exceeded",
                    ) from error
                raise
            if self._policy is not None:
                self._policy.peak_bytes = max(
                    self._policy.peak_bytes,
                    self._policy.used_bytes(),
                )

    def __delitem__(self, key):
        with self._lock:
            result = self._db.execute(
                "DELETE FROM entries WHERE key=?",
                (key,),
            )
        if not result.rowcount:
            raise KeyError(key)

    def __iter__(self):
        last = None
        while True:
            with self._lock:
                if last is None:
                    rows = self._db.execute(
                        "SELECT key FROM entries ORDER BY key LIMIT 1000",
                    ).fetchall()
                else:
                    rows = self._db.execute(
                        "SELECT key FROM entries WHERE key>? ORDER BY key LIMIT 1000",
                        (last,),
                    ).fetchall()
            if not rows:
                return
            for row in rows:
                yield row[0]
            last = rows[-1][0]

    def __len__(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[
                0
            ]

    def __eq__(self, other):
        if not isinstance(other, Mapping) or len(self) != len(other):
            return False
        return all(key in other and self[key] == other[key] for key in self)

    def close(self):
        if getattr(self, "_db", None) is not None:
            self._db.close()
            self._db = None
            self._directory.cleanup()

    def __del__(self):
        self.close()


class _Reader:
    def __init__(self, stream, max_value_bytes):
        self.stream = stream
        self.max_value_bytes = max_value_bytes
        self.buffer = ""
        self.decoder = json.JSONDecoder(
            object_pairs_hook=_unique_fields,
            parse_constant=_reject_nonfinite,
        )
        self.eof = False

    def _fill(self):
        chunk = self.stream.read(65536)
        self.eof = not chunk
        self.buffer += chunk
        if len(self.buffer.encode("utf-8")) > self.max_value_bytes:
            raise MigrationCapacityError(
                "manifest JSON value exceeds the reader memory limit",
            )

    def token(self, expected=None):
        while not self.buffer.lstrip() and not self.eof:
            self._fill()
        self.buffer = self.buffer.lstrip()
        if not self.buffer:
            raise ValueError("unexpected end of manifest")
        char = self.buffer[0]
        if expected is not None and char != expected:
            raise ValueError("invalid manifest JSON delimiter")
        self.buffer = self.buffer[1:]
        return char

    def value(self):
        while True:
            self.buffer = self.buffer.lstrip()
            try:
                value, end = self.decoder.raw_decode(self.buffer)
                # Numeric tokens may continue in the next chunk.
                if end == len(self.buffer) and not self.eof:
                    self._fill()
                    continue
                self.buffer = self.buffer[end:]
                return value
            except json.JSONDecodeError:
                if self.eof:
                    raise ValueError("invalid manifest JSON") from None
                self._fill()

    def object(self, value_reader):
        self.token("{")
        first = True
        seen = DiskMap()
        while True:
            while not self.buffer.lstrip() and not self.eof:
                self._fill()
            if self.buffer.lstrip().startswith("}"):
                self.token("}")
                return
            if not first:
                self.token(",")
            key = self.value()
            if not isinstance(key, str) or key in seen:
                raise ValueError("invalid or duplicate manifest key")
            seen[key] = True
            self.token(":")
            yield key, value_reader(key)
            first = False


def load_disk_manifest(
    path,
    *,
    retain_mirrors=True,
    max_value_bytes=64 * 1024 * 1024,
):
    """Keep progress mirrors on disk while retaining immutable source indexes."""
    if type(max_value_bytes) is not int or max_value_bytes < 65536:
        raise ValueError("manifest reader limit must be at least 65536 bytes")
    with open(path, encoding="utf-8") as stream:
        reader = _Reader(stream, max_value_bytes)

        def read_map(_key):
            result = DiskMap()
            for key, value in reader.object(lambda _name: reader.value()):
                if retain_mirrors:
                    result[key] = value
            return result

        def read_value(key):
            if key == "assignments":
                return dict(reader.object(read_map))
            if key == "batches":
                return read_map(key)
            return reader.value()

        result = dict(reader.object(read_value))
        while not reader.eof:
            reader._fill()
        if reader.buffer.strip():
            raise ValueError("trailing manifest JSON data")
        return result


def iter_json(value, *, default=None):
    """Yield JSON fragments, retaining only one decoded mapping entry at once."""
    if isinstance(value, Mapping):
        yield "{"
        for index, (key, child) in enumerate(value.items()):
            if index:
                yield ","
            yield json.dumps(key, ensure_ascii=False)
            yield ":"
            yield from iter_json(child, default=default)
        yield "}"
    elif isinstance(value, (list, tuple)):
        yield "["
        for index, child in enumerate(value):
            if index:
                yield ","
            yield from iter_json(child, default=default)
        yield "]"
    else:
        yield json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            default=default,
        )


def write_json(value, stream):
    """Write incrementally without constructing a full mirror."""
    for chunk in iter_json(value):
        stream.write(chunk)


class DiskSet:
    """Disposable identity uniqueness index used while validating mirrors."""

    def __init__(self):
        self.values = DiskMap()

    def __contains__(self, value):
        return str(value) in self.values

    def add(self, value):
        self.values[str(value)] = True


def read_manifest_header(path, *, max_value_bytes=64 * 1024 * 1024):
    """Parse bindings without retaining progress; JSON field order is arbitrary."""
    with disk_index_policy(
        Path(tempfile.gettempdir()).resolve(),
        max_bytes=64 * 1024 * 1024,
    ):
        result = load_disk_manifest(
            path,
            retain_mirrors=False,
            max_value_bytes=max_value_bytes,
        )
        result.pop("assignments", None)
        result.pop("batches", None)
    return result
