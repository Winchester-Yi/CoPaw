# -*- coding: utf-8 -*-
"""Immutable versioned bundles with bounded indexes and streamed payloads.

All nested directories and files are opened relative to pinned directory FDs
with O_NOFOLLOW. An exclusive hard link publishes the fsynced temporary file;
the final name is never overwritten or removed on an uncertain result.
"""

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import io
import errno
import json
import os
from pathlib import Path
import re
import secrets
import struct
import stat
from typing import BinaryIO, Iterator

from .errors import IntegrityError
from .identity import identity_bytes

MAGIC = b"SWECONV\x00"
VERSION = 1
HEADER = struct.Struct(">8sIQQQ")
CHUNK_BYTES = 65536
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_BLOCKS = 4096
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024
_METADATA_FIELDS = frozenset(
    {
        "operation_key",
        "kind",
        "session_id",
        "chat_id",
        "turn_id",
        "generation",
        "base_state_revision",
        "base_history_revision",
        "epoch",
        "boundary_id",
        "boundary_order",
        "event_first",
        "event_last",
        "checkpoint_revision",
        "checkpoint_watermark",
        "revision",
    },
)
_OPERATION = re.compile(r"[0-9a-f]{64}\Z")
_PATH = re.compile(r"bundles/([0-9a-f]{2})/([0-9a-f]{64})\.bundle\Z")


def new_operation_key() -> str:
    return secrets.token_hex(32)


@dataclass(frozen=True)
class BundleBlock:
    name: str
    kind: str
    content: bytes | BinaryIO
    length: int | None = None

    def byte_length(self) -> int:
        size = (
            len(self.content)
            if isinstance(self.content, bytes)
            else self.length
        )
        if type(size) is not int or size < 0:
            raise ValueError("stream blocks require a nonnegative byte length")
        if isinstance(self.content, bytes) and self.length not in (None, size):
            raise ValueError("block byte length disagrees with content")
        return size


@dataclass(frozen=True)
class BlockExtent:
    kind: str
    offset: int
    length: int


@dataclass(frozen=True)
class BundleIndex:
    relative_path: str
    byte_length: int
    metadata: dict
    blocks: dict[str, BlockExtent]


def _json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"nonfinite JSON number: {value}")


def _load_json(content):
    return json.loads(
        content,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _validate_metadata(metadata):
    if not isinstance(metadata, dict) or len(metadata) > 64:
        raise ValueError("bundle metadata must be a bounded object")
    for key, value in metadata.items():
        if key not in _METADATA_FIELDS:
            raise ValueError("bundle metadata contains a non-structural field")
        if isinstance(value, str):
            identity_bytes(value, 1024)
        elif type(value) is not int or value < 0:
            raise ValueError(
                "bundle metadata values must be identities or counters",
            )


def _valid_name(value, byte_limit):
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= byte_limit
    )


def _encode_index(blocks: list[BundleBlock]):
    if not blocks or len(blocks) > MAX_BLOCKS:
        raise ValueError("bundle block count is out of bounds")
    table = {}
    position = 0
    for block in blocks:
        if (
            not _valid_name(block.name, 256)
            or not _valid_name(block.kind, 64)
            or block.name in table
        ):
            raise ValueError("invalid or duplicate bundle block identity")
        size = block.byte_length()
        table[block.name] = [block.kind, position, size]
        position += size
    return _json_bytes(table), position


def _copy_block(target, block):
    remaining = block.byte_length()
    source = (
        io.BytesIO(block.content)
        if isinstance(block.content, bytes)
        else block.content
    )
    while remaining:
        data = source.read(min(CHUNK_BYTES, remaining))
        if not isinstance(data, bytes) or not data or len(data) > remaining:
            raise IntegrityError(
                "bundle source is shorter than its declared extent",
            )
        target.write(data)
        remaining -= len(data)
    if source.read(1):
        raise IntegrityError("bundle source exceeds its declared extent")


class BundleStore:
    """The configured root is trusted; every descendant is server generated."""

    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()

    @staticmethod
    def path_for(operation_key: str) -> str:
        if not isinstance(operation_key, str) or not _OPERATION.fullmatch(
            operation_key,
        ):
            raise ValueError("invalid conversation operation key")
        return f"bundles/{operation_key[:2]}/{operation_key}.bundle"

    @staticmethod
    def _parts(relative_path):
        match = (
            _PATH.fullmatch(relative_path)
            if isinstance(relative_path, str)
            else None
        )
        if match is None or match[1] != match[2][:2]:
            raise IntegrityError(
                "bundle path is not a generated relative path",
            )
        return relative_path.split("/")

    @contextmanager
    def _directory(self, relative_path, create=False):
        parts = self._parts(relative_path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors = []
        try:
            try:
                descriptors.append(os.open(self.root, flags))
            except FileNotFoundError as error:
                raise IntegrityError("bundle root is unavailable") from error
            for part in parts[:-1]:
                parent = descriptors[-1]
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    os.fsync(parent)
                descriptors.append(os.open(part, flags, dir_fd=parent))
            yield descriptors[-1], parts[-1]
        except OSError as error:
            if error.errno in (errno.ENOTDIR, errno.ELOOP):
                raise IntegrityError(
                    "bundle path contains a symlink or invalid directory",
                ) from error
            raise
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def prepare_directory(self, operation_key):
        with self._directory(self.path_for(operation_key), create=True):
            pass

    def reclaim(self, operation_key: str) -> bool:
        """Unlink only one server-generated regular bundle, then fsync its dir."""
        relative_path = self.path_for(operation_key)
        try:
            with self._directory(relative_path) as (directory, name):
                try:
                    details = os.stat(
                        name,
                        dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    os.fsync(directory)
                    return False
                if not stat.S_ISREG(details.st_mode):
                    raise IntegrityError(
                        "reclaimed bundle must be a regular file",
                    )
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
                return True
        except FileNotFoundError:
            return False

    def confirm_durable(self, relative_path: str) -> BundleIndex:
        """Re-fsync an existing exclusive result after a lost publish response."""
        index = self.inspect(relative_path)
        with self._directory(relative_path, create=True) as (directory, name):
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise IntegrityError("bundle must be a regular file")
                os.fsync(descriptor)
                os.fsync(directory)
            finally:
                os.close(descriptor)
        return self.inspect(relative_path, expected_length=index.byte_length)

    def publish(
        self,
        operation_key: str,
        blocks: list[BundleBlock],
        *,
        metadata: dict | None = None,
    ) -> BundleIndex:
        relative_path = self.path_for(operation_key)
        metadata_value = metadata if metadata is not None else {}
        _validate_metadata(metadata_value)
        metadata_bytes = _json_bytes(metadata_value)
        table, payload_length = _encode_index(blocks)
        if (
            len(metadata_bytes) > MAX_INDEX_BYTES
            or len(table) > MAX_INDEX_BYTES
        ):
            raise ValueError("bundle metadata exceeds index limit")
        byte_length = (
            HEADER.size + len(metadata_bytes) + len(table) + payload_length
        )
        if byte_length > MAX_BUNDLE_BYTES:
            raise ValueError(
                "bundle exceeds byte limit; split the publication",
            )
        with self._directory(relative_path, create=True) as (directory, name):
            temporary = f".{name}.{secrets.token_hex(16)}.tmp"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as target:
                    target.write(
                        HEADER.pack(
                            MAGIC,
                            VERSION,
                            len(metadata_bytes),
                            len(table),
                            payload_length,
                        ),
                    )
                    target.write(metadata_bytes)
                    target.write(table)
                    for block in blocks:
                        _copy_block(target, block)
                    target.flush()
                    os.fsync(target.fileno())
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                os.fsync(directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
        return self.inspect(relative_path, expected_length=byte_length)

    @contextmanager
    def _open(self, relative_path):
        with self._directory(relative_path) as (directory, name):
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise IntegrityError("bundle file is a symlink") from error
                raise
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise IntegrityError("bundle must be a regular file")
                yield source

    def inspect(
        self,
        relative_path: str,
        *,
        expected_length: int | None = None,
    ) -> BundleIndex:
        with self._open(relative_path) as source:
            size = os.fstat(source.fileno()).st_size
            header = source.read(HEADER.size)
            if len(header) != HEADER.size:
                raise IntegrityError("truncated bundle header")
            magic, version, meta_size, table_size, payload_size = (
                HEADER.unpack(header)
            )
            if magic != MAGIC or version != VERSION:
                raise IntegrityError("unknown bundle format version")
            if (
                meta_size > MAX_INDEX_BYTES
                or table_size > MAX_INDEX_BYTES
                or size != HEADER.size + meta_size + table_size + payload_size
                or size > MAX_BUNDLE_BYTES
            ):
                raise IntegrityError(
                    "bundle length or index bounds are invalid",
                )
            if expected_length is not None and size != expected_length:
                raise IntegrityError(
                    "bundle disagrees with published byte length",
                )
            try:
                metadata = _load_json(source.read(meta_size))
                _validate_metadata(metadata)
                table = _load_json(source.read(table_size))
                if (
                    not isinstance(metadata, dict)
                    or not isinstance(table, dict)
                    or not 0 < len(table) <= MAX_BLOCKS
                ):
                    raise ValueError("invalid index")
                blocks = self._decode_table(table, source.tell(), payload_size)
            except (ValueError, TypeError, KeyError, RecursionError) as error:
                raise IntegrityError("bundle index is invalid") from error
            return BundleIndex(relative_path, size, metadata, blocks)

    @staticmethod
    def _decode_table(table, start, payload_size):
        blocks = {}
        position = 0
        for name, value in table.items():
            if not isinstance(value, list) or len(value) != 3:
                raise ValueError("invalid block")
            kind, offset, length = value
            if (
                not _valid_name(name, 256)
                or not _valid_name(kind, 64)
                or type(offset) is not int
                or type(length) is not int
            ):
                raise ValueError("invalid block identity or byte types")
            if (
                offset != position
                or length < 0
                or offset + length > payload_size
            ):
                raise ValueError("invalid block extent")
            blocks[name] = BlockExtent(kind, start + offset, length)
            position += length
        if position != payload_size:
            raise ValueError("unindexed payload bytes")
        return blocks

    def iter_extent(
        self,
        relative_path: str,
        offset: int,
        length: int,
        *,
        expected_length: int | None = None,
    ) -> Iterator[bytes]:
        if (
            type(offset) is not int
            or type(length) is not int
            or offset < 0
            or length < 0
        ):
            raise IntegrityError("bundle extent is invalid")
        index = self.inspect(relative_path, expected_length=expected_length)
        if not any(
            offset >= block.offset
            and offset + length <= block.offset + block.length
            for block in index.blocks.values()
        ):
            raise IntegrityError("bundle extent is outside a payload block")
        with self._open(relative_path) as source:
            if os.fstat(source.fileno()).st_size != index.byte_length:
                raise IntegrityError("bundle length changed during read")
            source.seek(offset)
            remaining = length
            while remaining:
                chunk = source.read(min(remaining, CHUNK_BYTES))
                if not chunk:
                    raise IntegrityError("truncated bundle extent")
                remaining -= len(chunk)
                yield chunk

    def read_extent(
        self,
        relative_path: str,
        offset: int,
        length: int,
        *,
        expected_length: int | None = None,
    ) -> bytes:
        if type(length) is not int or length > MAX_READ_BYTES:
            raise IntegrityError("extent requires bounded streaming read")
        return b"".join(
            self.iter_extent(
                relative_path,
                offset,
                length,
                expected_length=expected_length,
            ),
        )
