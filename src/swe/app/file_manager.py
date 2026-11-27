# -*- coding: utf-8 -*-
"""Controlled, tenant-workspace directory access for the chat file manager.

This module deliberately contains no FastAPI routes.  Routers provide tenant
authentication and translate its small, typed surface into HTTP responses.
"""

from __future__ import annotations

import base64
import binascii
import codecs
from collections import OrderedDict
import heapq
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import stat as stat_module
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import cache, wraps
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Mapping

from pydantic import BaseModel

from ..constant import SECRET_DIR
from .file_governance.archive_maintenance import ARCHIVE_FILES_DIR

logger = logging.getLogger(__name__)
_WORKSPACE_LOCKS_GUARD = threading.Lock()
_WORKSPACE_LOCKS: dict[str, threading.RLock] = {}

FILE_MANAGER_PAGE_SIZE = 100
TEXT_PREVIEW_LIMIT_BYTES = 1024 * 1024
_FILE_READ_CHUNK_BYTES = 64 * 1024
_FILE_MANAGER_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_LARGE_PREVIEW_PROBE_BYTES = 4
_WORKING_HIDDEN_TOP_LEVEL = frozenset({"sessions", "governance"})
_WORKING_BACKING_TOP_LEVEL = frozenset({"media", "static"})
_NATURAL_PARTS = re.compile(r"(\d+)")
_CURSOR_SECRET_ENV_VAR = "SWE_FILE_MANAGER_CURSOR_SECRET"
_CURSOR_SECRET_FILE_NAME = "file-manager-cursor-secret"


class FileManagerPathError(ValueError):
    """A requested path is outside the controlled directory contract."""


class FileManagerNotFoundError(FileManagerPathError):
    """A valid controlled relative path does not currently exist."""


class FileManagerConflictError(FileManagerPathError):
    """A mutation would replace a newer or already-existing file."""


class FileManagerUploadTooLargeError(FileManagerPathError):
    """A streamed upload exceeded the File Manager byte limit."""


class FileManagerOutcomeUncertainError(FileManagerPathError):
    """A mutation committed a rename/unlink but durability confirmation failed."""


class FileManagerRoot(str, Enum):
    WORKING = "working"
    SOURCE_SCOPE = "source_scope"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    CONVERSATION = "conversation"
    RECYCLE = "recycle"


class FileManagerItemKind(str, Enum):
    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"
    SPECIAL = "special"


class FileManagerCapabilities(BaseModel):
    browse: bool = False
    read: bool = False
    upload: bool = False
    edit: bool = False
    download: bool = False
    archive: bool = False


class FileManagerItem(BaseModel):
    name: str
    path: str
    kind: FileManagerItemKind
    size_bytes: int | None = None
    modified_at: datetime | None = None
    capabilities: FileManagerCapabilities
    archive_item_id: str | None = None
    original_path: str | None = None
    archived_at: datetime | None = None


class FileManagerDirectoryListing(BaseModel):
    root: FileManagerRoot
    path: str
    items: list[FileManagerItem]
    next_cursor: str | None = None
    has_child_directory: bool = False
    first_child_directory: str | None = None
    capabilities: FileManagerCapabilities


class FileManagerTextPreview(BaseModel):
    path: str
    size_bytes: int
    is_text: bool
    content: str | None = None
    is_truncated: bool = False
    editable: bool = False
    revision: str


@dataclass(frozen=True)
class FileManagerReadSnapshot:
    """A bounded read result tied to one safely opened file descriptor."""

    path: str
    size_bytes: int
    modified_at: datetime
    revision: str
    is_text: bool
    is_truncated: bool
    preview_bytes: bytes


@dataclass(frozen=True)
class FileManagerDownload:
    """A regular file descriptor that remains safe until its stream closes."""

    file_descriptor: int
    filename: str
    size_bytes: int


@dataclass(frozen=True)
class FileManagerRecycleMutation:
    """The public identity of one archived file-manager item."""

    archive_item_id: str
    original_path: str


@dataclass(frozen=True)
class _DirectoryCandidate:
    name: str
    entry_stat: os.stat_result
    sort_key: tuple[int, tuple[tuple[int, int | str], ...], str]


@dataclass(frozen=True)
class _DirectorySnapshot:
    version: str
    identity: tuple[int, int, int]
    items: tuple[FileManagerItem, ...]
    expires_at: float


_DIRECTORY_SNAPSHOT_TTL_SECONDS = 30.0
_DIRECTORY_SNAPSHOT_CAPACITY = 128
_DIRECTORY_SNAPSHOT_WORKSPACE_CAPACITY = 16
_DIRECTORY_SNAPSHOTS: OrderedDict[
    tuple[str, str, str, str],
    _DirectorySnapshot,
] = OrderedDict()
_DIRECTORY_SNAPSHOTS_LOCK = threading.Lock()


def root_capabilities(root: FileManagerRoot | str) -> FileManagerCapabilities:
    try:
        root = FileManagerRoot(root)
    except (TypeError, ValueError) as exc:
        raise FileManagerPathError("Unknown file manager root") from exc
    if root is FileManagerRoot.CONVERSATION:
        return FileManagerCapabilities(browse=True, read=True, download=True)
    if root is FileManagerRoot.RECYCLE:
        return FileManagerCapabilities(browse=True)
    return FileManagerCapabilities(
        browse=True,
        read=True,
        upload=True,
        edit=True,
        download=True,
        archive=True,
    )


@cache
def _load_or_create_cursor_secret() -> bytes:
    """Return the process-cached cursor HMAC secret."""
    return _load_or_create_cursor_secret_uncached()


def _load_or_create_cursor_secret_uncached() -> bytes:
    """Return one process-independent cursor HMAC secret.

    The environment value supports managed deployments.  Local deployments
    share a private, atomically-created secret beneath ``SECRET_DIR`` so a
    cursor signed by one worker remains valid after a restart or on another
    worker.  Tenant workspaces must never hold this signing material.
    """

    configured_secret = os.environ.get(_CURSOR_SECRET_ENV_VAR)
    if configured_secret is not None:
        if not configured_secret:
            raise ValueError(
                "SWE_FILE_MANAGER_CURSOR_SECRET must not be empty",
            )
        return configured_secret.encode("utf-8")

    SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_path = SECRET_DIR / _CURSOR_SECRET_FILE_NAME

    try:
        return _read_cursor_secret(secret_path)
    except FileNotFoundError:
        pass

    secret = os.urandom(48)
    temporary_path = SECRET_DIR / (
        f".{_CURSOR_SECRET_FILE_NAME}.{os.getpid()}.{os.urandom(12).hex()}"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        _write_all(descriptor, secret)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary_path, secret_path, follow_symlinks=False)
    except FileExistsError:
        return _read_cursor_secret(secret_path)
    except OSError as exc:
        raise RuntimeError(
            "Unable to create file-manager cursor secret",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return secret


def _read_cursor_secret(secret_path: Path) -> bytes:
    """Read one fully-published, private fallback secret."""

    descriptor = os.open(
        secret_path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        entry_stat = os.fstat(descriptor)
        if not stat_module.S_ISREG(entry_stat.st_mode):
            raise RuntimeError(
                "File-manager cursor secret must be a regular file",
            )
        secret = os.read(descriptor, 1024)
    finally:
        os.close(descriptor)
    if len(secret) != 48:
        raise RuntimeError("File-manager cursor secret is invalid")
    return secret


def _write_all(descriptor: int, data: bytes) -> None:
    """Write the fallback secret completely before publishing it by link."""

    offset = 0
    while offset < len(data):
        offset += os.write(descriptor, data[offset:])


def _isoformat_utc(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def _parse_archive_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Missing archived time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Archived time must include a timezone")
    return parsed.astimezone(timezone.utc)


def _workspace_lock(workspace_dir: Path) -> threading.RLock:
    """Return the process-local serialization lock for one tenant workspace."""

    key = str(workspace_dir.resolve())
    with _WORKSPACE_LOCKS_GUARD:
        lock = _WORKSPACE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _WORKSPACE_LOCKS[key] = lock
        return lock


def _serialized_mutation(method):
    """Serialize mutation validation and publication within one workspace."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._workspace_lock:
            return method(self, *args, **kwargs)

    return wrapped


def get_file_manager_service(
    workspace_dir: Path,
    *,
    source_scope_base_dir: Path | None = None,
    source_scope_component: str | None = None,
) -> "FileManagerService":
    """Create the only service route handlers may use for a tenant workspace."""

    return FileManagerService(
        workspace_dir,
        cursor_secret=_load_or_create_cursor_secret(),
        source_scope_base_dir=source_scope_base_dir,
        source_scope_component=source_scope_component,
    )


class FileManagerService:
    """Confines operations to the roots exposed by the chat file manager."""

    def __init__(
        self,
        workspace_dir: Path,
        *,
        cursor_secret: bytes | str,
        source_scope_base_dir: Path | None = None,
        source_scope_component: str | None = None,
    ) -> None:
        self._workspace_dir = workspace_dir.resolve()
        self._source_scope_base_dir = source_scope_base_dir
        self._source_scope_component = source_scope_component
        self._workspace_lock = _workspace_lock(self._workspace_dir)
        self._cursor_secret = (
            cursor_secret.encode("utf-8")
            if isinstance(cursor_secret, str)
            else cursor_secret
        )
        if not self._cursor_secret:
            raise ValueError("cursor_secret must not be empty")

    def resolve_path(
        self,
        root: FileManagerRoot | str,
        relative_path: str = "",
    ) -> tuple[str, Path]:
        """Return a safe, non-symlink path under one controlled root.

        The empty path is the root itself.  All non-empty values must use the
        POSIX relative representation returned to API consumers.
        """

        resolved_root, normalised_path = self._validate_root_and_path(
            root,
            relative_path,
        )
        if normalised_path:
            self._reject_symlink_path(resolved_root, normalised_path)
        # This display path is not an access grant.  All filesystem operations
        # below use _open_directory_fd/_open_file_fd with O_NOFOLLOW.
        return normalised_path, self._root_path(resolved_root).joinpath(
            *filter(None, normalised_path.split("/")),
        )

    def list_directory(
        self,
        root: FileManagerRoot | str,
        relative_path: str = "",
        *,
        cursor: str | None = None,
        query: str | None = None,
    ) -> FileManagerDirectoryListing:
        try:
            requested_root = FileManagerRoot(root)
        except (TypeError, ValueError) as exc:
            raise FileManagerPathError("Unknown file manager root") from exc
        if requested_root is FileManagerRoot.RECYCLE:
            if relative_path or query:
                raise FileManagerPathError(
                    "Recycle does not support directory paths or queries",
                )
            with self._workspace_lock:
                return self._list_recycle_items(cursor)
        resolved_root, normalised_path = self._validate_root_and_path(
            root,
            relative_path,
        )
        cursor_state = self._validate_cursor_context(
            cursor,
            resolved_root,
            normalised_path,
            query,
        )
        snapshot_key = (
            str(self._workspace_dir),
            resolved_root.value,
            normalised_path,
            query or "",
        )
        directory_fd = self._open_directory_fd(
            resolved_root,
            normalised_path,
            allow_missing_root=True,
        )
        if directory_fd is None:
            if cursor_state is not None:
                raise FileManagerConflictError(
                    "Directory listing changed; refresh and retry",
                )
            return self._listing(resolved_root, normalised_path, [])
        try:
            directory_stat = os.fstat(directory_fd)
            directory_identity = (
                directory_stat.st_dev,
                directory_stat.st_ino,
                directory_stat.st_mtime_ns,
            )
            if cursor_state is not None:
                last_path, last_kind, snapshot_version = cursor_state
                if snapshot_version is None:
                    raise FileManagerConflictError(
                        "Directory listing changed; refresh and retry",
                    )
                snapshot = self._get_directory_snapshot(
                    snapshot_key,
                    snapshot_version,
                    directory_identity,
                )
                if snapshot is None:
                    raise FileManagerConflictError(
                        "Directory listing changed; refresh and retry",
                    )
                items = list(snapshot.items)
                try:
                    start = next(
                        index + 1
                        for index, item in enumerate(items)
                        if item.path == last_path and item.kind is last_kind
                    )
                except StopIteration as exc:
                    raise FileManagerConflictError(
                        "Directory listing changed; refresh and retry",
                    ) from exc
                snapshot_version = snapshot.version
            else:
                candidates = self._directory_candidates(
                    resolved_root,
                    normalised_path,
                    directory_fd,
                    query,
                    None,
                )
                items = [
                    self._item_for_entry(
                        resolved_root,
                        normalised_path,
                        candidate.name,
                        candidate.entry_stat,
                    )
                    for candidate in candidates
                ]
                snapshot_version = secrets.token_urlsafe(18)
                self._put_directory_snapshot(
                    snapshot_key,
                    _DirectorySnapshot(
                        version=snapshot_version,
                        identity=directory_identity,
                        items=tuple(items),
                        expires_at=time.monotonic()
                        + _DIRECTORY_SNAPSHOT_TTL_SECONDS,
                    ),
                )
                start = 0
        finally:
            os.close(directory_fd)
        page = items[start : start + FILE_MANAGER_PAGE_SIZE]
        next_cursor = None
        if start + FILE_MANAGER_PAGE_SIZE < len(items):
            next_cursor = self._encode_cursor(
                resolved_root,
                normalised_path,
                query,
                page[-1].path,
                page[-1].kind,
                snapshot_version,
            )
        return self._listing(resolved_root, normalised_path, page, next_cursor)

    def read_text_preview(
        self,
        root: FileManagerRoot | str,
        relative_path: str,
    ) -> FileManagerTextPreview:
        snapshot = self.read_file_snapshot(root, relative_path)
        resolved_root, _ = self._validate_root_and_path(root, relative_path)
        if not snapshot.is_text:
            return FileManagerTextPreview(
                path=snapshot.path,
                size_bytes=snapshot.size_bytes,
                is_text=False,
                is_truncated=snapshot.is_truncated,
                revision=snapshot.revision,
            )
        content = self._decode_preview_bytes(snapshot.preview_bytes)
        return FileManagerTextPreview(
            path=snapshot.path,
            size_bytes=snapshot.size_bytes,
            is_text=True,
            content=content,
            is_truncated=snapshot.is_truncated,
            editable=(
                not snapshot.is_truncated
                and root_capabilities(resolved_root).edit
            ),
            revision=snapshot.revision,
        )

    def open_file_for_download(
        self,
        root: FileManagerRoot | str,
        relative_path: str,
    ) -> FileManagerDownload:
        """Open one regular, non-symlink file for attachment streaming."""

        resolved_root, normalised_path = self._validate_root_and_path(
            root,
            relative_path,
        )
        if not root_capabilities(resolved_root).download:
            raise FileManagerPathError(
                "Downloads are not available for this root",
            )
        file_fd = self._open_file_fd(resolved_root, normalised_path)
        try:
            entry_stat = os.fstat(file_fd)
            if not stat_module.S_ISREG(entry_stat.st_mode):
                raise FileManagerPathError("Path is not a regular file")
            return FileManagerDownload(
                file_descriptor=file_fd,
                filename=normalised_path.rsplit("/", maxsplit=1)[-1],
                size_bytes=entry_stat.st_size,
            )
        except Exception:
            os.close(file_fd)
            raise

    def read_file_snapshot(
        self,
        root: FileManagerRoot | str,
        relative_path: str,
    ) -> FileManagerReadSnapshot:
        """Read a bounded preview and validate the complete file on one fd.

        The snapshot supplies a stable revision hook for subsequent save
        operations without exposing the full file body to callers.
        """

        resolved_root, normalised_path = self._validate_root_and_path(
            root,
            relative_path,
        )
        file_fd = self._open_file_fd(resolved_root, normalised_path)
        try:
            initial_stat = os.fstat(file_fd)
            if not stat_module.S_ISREG(initial_stat.st_mode):
                raise FileManagerPathError("Path is not a regular file")
            is_truncated = initial_stat.st_size > TEXT_PREVIEW_LIMIT_BYTES
            if is_truncated:
                with os.fdopen(file_fd, "rb", closefd=False) as handle:
                    sampled = handle.read(
                        TEXT_PREVIEW_LIMIT_BYTES + _LARGE_PREVIEW_PROBE_BYTES,
                    )
                decoded_sample = self._decode_text_sample(
                    sampled,
                    sample_is_truncated=True,
                )
                decoded = self._decode_text_sample(
                    sampled[:TEXT_PREVIEW_LIMIT_BYTES],
                    sample_is_truncated=True,
                )
                is_text = (
                    decoded_sample is not None
                    and not self._contains_disallowed_control_character(
                        decoded_sample,
                    )
                )
                preview = (
                    decoded.encode("utf-8") if decoded is not None else sampled
                )
                revision = (
                    f"stat:{initial_stat.st_dev}:{initial_stat.st_ino}:"
                    f"{initial_stat.st_size}:{initial_stat.st_mtime_ns}"
                )
            else:
                preview = bytearray()
                content_digest = hashlib.sha256()
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                is_text = True
                has_control_character = False
                with os.fdopen(file_fd, "rb", closefd=False) as handle:
                    while chunk := handle.read(_FILE_READ_CHUNK_BYTES):
                        content_digest.update(chunk)
                        preview.extend(chunk)
                        if not is_text:
                            continue
                        try:
                            decoded_chunk = decoder.decode(chunk, final=False)
                        except UnicodeDecodeError:
                            is_text = False
                            continue
                        if self._contains_disallowed_control_character(
                            decoded_chunk,
                        ):
                            has_control_character = True
                    if is_text:
                        try:
                            tail = decoder.decode(b"", final=True)
                        except UnicodeDecodeError:
                            is_text = False
                        else:
                            has_control_character = (
                                has_control_character
                                or self._contains_disallowed_control_character(
                                    tail,
                                )
                            )
                is_text = is_text and not has_control_character
                revision = content_digest.hexdigest()
            final_stat = os.fstat(file_fd)
        except OSError as exc:
            raise FileManagerPathError("Unable to read file") from exc
        finally:
            os.close(file_fd)

        if self._stat_identity(initial_stat) != self._stat_identity(
            final_stat,
        ):
            raise FileManagerPathError("File changed while being read")
        return FileManagerReadSnapshot(
            path=normalised_path,
            size_bytes=initial_stat.st_size,
            modified_at=datetime.fromtimestamp(
                initial_stat.st_mtime,
                tz=timezone.utc,
            ),
            revision=revision,
            is_text=is_text,
            is_truncated=is_truncated,
            preview_bytes=bytes(preview),
        )

    @_serialized_mutation
    def save_text(
        self,
        root: FileManagerRoot | str,
        relative_path: str,
        content: str,
        revision: str,
    ) -> FileManagerTextPreview:
        """Atomically save one current, small UTF-8 text-file snapshot.

        The revision is checked against an opened no-follow descriptor and is
        checked again immediately before replacement.  This deliberately
        rejects concurrent updates rather than silently overwriting them.
        """

        resolved_root, normalised_path = self._validate_root_and_path(
            root,
            relative_path,
        )
        if not root_capabilities(resolved_root).edit:
            raise FileManagerPathError(
                "Text editing is not available for this root",
            )
        if not isinstance(content, str) or not isinstance(revision, str):
            raise FileManagerPathError("Invalid text save payload")
        encoded_content = content.encode("utf-8")
        if len(encoded_content) > TEXT_PREVIEW_LIMIT_BYTES:
            raise FileManagerPathError(
                "Text content exceeds the editable limit",
            )

        snapshot = self.read_file_snapshot(resolved_root, normalised_path)
        if (
            not snapshot.is_text
            or snapshot.is_truncated
            or snapshot.revision != revision
        ):
            if snapshot.revision != revision:
                raise FileManagerConflictError(
                    "File revision no longer matches",
                )
            raise FileManagerPathError("File is not an editable text file")

        parts = tuple(filter(None, normalised_path.split("/")))
        parent_fd = self._open_directory_fd(
            resolved_root,
            "/".join(parts[:-1]),
        )
        assert parent_fd is not None
        temporary_name = self._temporary_name(parts[-1])
        temporary_fd: int | None = None
        published = False
        try:
            if (
                self._content_revision_for_entry(parent_fd, parts[-1])
                != revision
            ):
                raise FileManagerConflictError(
                    "File revision no longer matches",
                )
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=parent_fd,
            )
            _write_all(temporary_fd, encoded_content)
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            # Replacement never follows an existing destination symlink.  The
            # preceding no-follow stat binds the revision to the source seen
            # by the editor.
            if (
                self._content_revision_for_entry(parent_fd, parts[-1])
                != revision
            ):
                raise FileManagerConflictError(
                    "File revision no longer matches",
                )
            os.replace(
                temporary_name,
                parts[-1],
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            published = True
            os.fsync(parent_fd)
        except FileExistsError as exc:
            raise FileManagerConflictError(
                "File revision no longer matches",
            ) from exc
        except OSError as exc:
            if published:
                raise FileManagerOutcomeUncertainError(
                    "File-manager mutation outcome is uncertain",
                ) from exc
            raise FileManagerPathError("Unable to save text file") from exc
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(parent_fd)
        return FileManagerTextPreview(
            path=normalised_path,
            size_bytes=len(encoded_content),
            is_text=True,
            content=content,
            editable=True,
            revision=hashlib.sha256(encoded_content).hexdigest(),
        )

    def upload_bytes(
        self,
        root: FileManagerRoot | str,
        directory_path: str,
        filename: str,
        content: bytes,
    ) -> FileManagerItem:
        """Publish a new upload without following links or replacing names."""

        if not isinstance(content, bytes):
            raise FileManagerPathError("Upload content must be bytes")
        return self.upload_stream(
            root,
            directory_path,
            filename,
            io.BytesIO(content),
        )

    def upload_stream(
        self,
        root: FileManagerRoot | str,
        directory_path: str,
        filename: str,
        source: BinaryIO,
    ) -> FileManagerItem:
        """Publish a bounded file-like upload without retaining its body."""

        resolved_root, normalised_directory = self._validate_root_and_path(
            root,
            directory_path,
        )
        if not root_capabilities(resolved_root).upload:
            raise FileManagerPathError(
                "Uploads are not available for this root",
            )
        safe_filename = self._validate_upload_filename(filename)
        directory_fd = self._open_directory_fd(
            resolved_root,
            normalised_directory,
            create_source_scope_root=(
                resolved_root is FileManagerRoot.SOURCE_SCOPE
            ),
        )
        assert directory_fd is not None
        temporary_name = self._temporary_name(safe_filename)
        temporary_fd: int | None = None
        published = False
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
            copied_bytes = 0
            while chunk := source.read(_FILE_READ_CHUNK_BYTES):
                copied_bytes += len(chunk)
                if copied_bytes > _FILE_MANAGER_MAX_UPLOAD_BYTES:
                    raise FileManagerUploadTooLargeError(
                        "File too large (max 10 MB)",
                    )
                _write_all(temporary_fd, chunk)
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            try:
                os.link(
                    temporary_name,
                    safe_filename,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                published = True
            except FileExistsError as exc:
                raise FileManagerConflictError(
                    "A file with this name already exists",
                ) from exc
            os.unlink(temporary_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
            entry_stat = self._stat_entry(directory_fd, safe_filename)
        except FileManagerPathError:
            raise
        except OSError as exc:
            if published:
                raise FileManagerOutcomeUncertainError(
                    "File-manager mutation outcome is uncertain",
                ) from exc
            raise FileManagerPathError("Unable to upload file") from exc
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory_fd)
        return self._item_for_entry(
            resolved_root,
            normalised_directory,
            safe_filename,
            entry_stat,
        )

    @_serialized_mutation
    def archive_file(
        self,
        root: FileManagerRoot | str,
        relative_path: str,
        *,
        actor: str,
    ) -> FileManagerRecycleMutation:
        """Copy one safely opened regular file into the governance archive.

        Unlike governance maintenance, the file manager intentionally permits
        visible working-root files such as MEMORY.md.  Only sessions and
        governance stay hidden through the controlled-root policy.
        """

        resolved_root, normalised_path = self._validate_root_and_path(
            root,
            relative_path,
        )
        if not root_capabilities(resolved_root).archive:
            raise FileManagerPathError(
                "Archiving is not available for this root",
            )
        source_fd = self._open_file_fd(resolved_root, normalised_path)
        archive_directory_fd: int | None = None
        archive_item_id: str | None = None
        try:
            source_stat = os.fstat(source_fd)
            if not stat_module.S_ISREG(source_stat.st_mode):
                raise FileManagerPathError(
                    "Only regular files can be archived",
                )
            archive_item_id = os.urandom(16).hex()
            original_path = self._archive_display_path(
                resolved_root,
                normalised_path,
            )
            item = {
                "id": archive_item_id,
                "root": resolved_root.value,
                "relative_path": normalised_path,
                "original_path": original_path,
                "archive_path": f"{ARCHIVE_FILES_DIR}/{archive_item_id}",
                "size_bytes": source_stat.st_size,
                "mtime": _isoformat_utc(source_stat.st_mtime),
                "archived_at": _isoformat_utc(
                    datetime.now(timezone.utc).timestamp(),
                ),
                "archived_by": actor,
                "archive_reason": "file_manager_delete",
            }
            index = self._load_recovered_archive_index()
            prepared_index = dict(index)
            prepared_index["transition"] = {
                "operation": "archive",
                "item": item,
            }
            self._save_archive_index(prepared_index)
            archive_directory_fd = self._open_archive_files_directory_fd()
            self._copy_fd_to_new_file(
                source_fd,
                archive_directory_fd,
                archive_item_id,
            )
            updated_index = dict(prepared_index)
            existing_items = index.get("items")
            updated_index["items"] = [
                *(existing_items if isinstance(existing_items, list) else []),
                item,
            ]
            self._save_archive_index(updated_index)
            self._unlink_if_same_file(
                resolved_root,
                normalised_path,
                source_stat,
            )
            final_index = dict(updated_index)
            final_index.pop("transition", None)
            self._save_archive_index(final_index)
        except FileManagerOutcomeUncertainError:
            raise
        except Exception:
            # A persisted transition is intentionally left for safe recovery.
            raise
        finally:
            os.close(source_fd)
            if archive_directory_fd is not None:
                os.close(archive_directory_fd)
        assert archive_item_id is not None
        return FileManagerRecycleMutation(archive_item_id, original_path)

    @_serialized_mutation
    def delete_directory(
        self,
        root: FileManagerRoot | str,
        relative_path: str,
    ) -> None:
        """Permanently delete one controlled directory without following links."""

        resolved_root, normalised_path = self._validate_root_and_path(
            root,
            relative_path,
        )
        if not normalised_path:
            raise FileManagerPathError("Path is not a directory")
        if not root_capabilities(resolved_root).archive:
            raise FileManagerPathError(
                "Directory deletion is not available for this root",
            )
        parts = tuple(filter(None, normalised_path.split("/")))
        if (
            resolved_root is FileManagerRoot.WORKING
            and len(parts) == 1
            and parts[0] in _WORKING_BACKING_TOP_LEVEL
        ):
            raise FileManagerPathError("Directory is managed by file manager")
        parent_fd = self._open_directory_fd(
            resolved_root,
            "/".join(parts[:-1]),
        )
        assert parent_fd is not None
        try:
            directory_fd = os.open(
                parts[-1],
                self._directory_open_flags(),
                dir_fd=parent_fd,
            )
            try:
                self._delete_directory_contents(directory_fd)
            finally:
                os.close(directory_fd)
            os.rmdir(parts[-1], dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileNotFoundError as exc:
            raise FileManagerNotFoundError("Directory was not found") from exc
        except OSError as exc:
            raise FileManagerPathError("Unable to delete directory") from exc
        finally:
            os.close(parent_fd)

    def _delete_directory_contents(self, directory_fd: int) -> None:
        """Remove one opened directory's entries without traversing links."""

        try:
            with os.scandir(directory_fd) as entries:
                names = [entry.name for entry in entries]
            for name in names:
                entry_stat = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if stat_module.S_ISDIR(entry_stat.st_mode):
                    child_fd = os.open(
                        name,
                        self._directory_open_flags(),
                        dir_fd=directory_fd,
                    )
                    try:
                        self._delete_directory_contents(child_fd)
                    finally:
                        os.close(child_fd)
                    os.rmdir(name, dir_fd=directory_fd)
                else:
                    os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError as exc:
            raise FileManagerPathError("Unable to delete directory") from exc

    # The staged archive transition intentionally keeps all rollback-free
    # boundaries in one auditable method.
    # pylint: disable=too-many-statements
    @_serialized_mutation
    def restore_recycle_item(
        self,
        archive_item_id: str,
        *,
        actor: str,
    ) -> FileManagerRecycleMutation:
        """Restore exactly one archived payload to its original safe path."""

        index, item = self._recycle_index_item(archive_item_id)
        root, relative_path, original_path = self._archive_item_location(item)
        parts = tuple(filter(None, relative_path.split("/")))
        target_parent_fd = self._open_directory_fd(root, "/".join(parts[:-1]))
        assert target_parent_fd is not None
        try:
            try:
                self._stat_entry(target_parent_fd, parts[-1])
            except FileManagerNotFoundError:
                pass
            else:
                raise FileManagerConflictError("Restore target already exists")
            prepared_index = dict(index)
            prepared_index["transition"] = {
                "operation": "restore",
                "item": item,
            }
            self._save_archive_index(prepared_index)
            archive_fd, archive_parent_fd = self._open_archive_payload_fd(
                archive_item_id,
            )
            try:
                source_stat = os.fstat(archive_fd)
                if not stat_module.S_ISREG(source_stat.st_mode):
                    raise FileManagerPathError(
                        "Archived payload is not a regular file",
                    )
                self._copy_fd_to_new_file(
                    archive_fd,
                    target_parent_fd,
                    parts[-1],
                )
                target_identity = self._root_file_identity(root, relative_path)
                if target_identity is None:
                    raise FileManagerConflictError(
                        "Restored target disappeared during recovery",
                    )
                updated_index = dict(prepared_index)
                prepared_transition = prepared_index.get("transition")
                assert isinstance(prepared_transition, dict)
                transition = dict(prepared_transition)
                transition["target_identity"] = target_identity
                updated_index["transition"] = transition
                updated_index["items"] = [
                    row
                    for row in index.get("items", [])
                    if str(row.get("id") or "") != archive_item_id
                ]
                try:
                    self._save_archive_index(updated_index)
                except FileManagerOutcomeUncertainError:
                    raise
                except Exception:
                    raise
                try:
                    self._unlink_archive_payload_confirmed(
                        archive_parent_fd,
                        archive_item_id,
                        source_stat,
                    )
                except FileManagerOutcomeUncertainError:
                    raise
                except (FileManagerPathError, OSError):
                    raise
            finally:
                os.close(archive_fd)
                os.close(archive_parent_fd)
        finally:
            os.close(target_parent_fd)
        final_index = dict(updated_index)
        final_index.pop("transition", None)
        self._save_archive_index(final_index)
        _ = actor
        return FileManagerRecycleMutation(archive_item_id, original_path)

    @_serialized_mutation
    def purge_recycle_item(
        self,
        archive_item_id: str,
        *,
        actor: str,
    ) -> FileManagerRecycleMutation:
        """Permanently remove one archive payload and its index record."""

        index, item = self._recycle_index_item(archive_item_id)
        original_path = self._validate_archive_original_path(item)
        prepared_index = dict(index)
        prepared_index["transition"] = {
            "operation": "purge",
            "item": item,
        }
        self._save_archive_index(prepared_index)
        archive_fd, archive_parent_fd = self._open_archive_payload_fd(
            archive_item_id,
        )
        try:
            if not stat_module.S_ISREG(os.fstat(archive_fd).st_mode):
                raise FileManagerPathError(
                    "Archived payload is not a regular file",
                )
            updated_index = dict(prepared_index)
            updated_index["items"] = [
                row
                for row in index.get("items", [])
                if str(row.get("id") or "") != archive_item_id
            ]
            self._save_archive_index(updated_index)
            try:
                self._unlink_archive_payload_confirmed(
                    archive_parent_fd,
                    archive_item_id,
                    os.fstat(archive_fd),
                )
            except FileManagerOutcomeUncertainError:
                raise
            except OSError:
                raise
        finally:
            os.close(archive_fd)
            os.close(archive_parent_fd)
        final_index = dict(updated_index)
        final_index.pop("transition", None)
        self._save_archive_index(final_index)
        _ = actor
        return FileManagerRecycleMutation(archive_item_id, original_path)

    def _root_path(self, root: FileManagerRoot) -> Path:
        if root is FileManagerRoot.SOURCE_SCOPE:
            if (
                self._source_scope_base_dir is None
                or self._source_scope_component is None
            ):
                raise FileManagerPathError("Source-scope root is unavailable")
            return self._source_scope_base_dir / self._source_scope_component
        suffixes = {
            FileManagerRoot.WORKING: (),
            FileManagerRoot.UPLOAD: ("media",),
            FileManagerRoot.DOWNLOAD: ("static",),
            FileManagerRoot.CONVERSATION: ("sessions",),
        }
        return self._workspace_dir.joinpath(*suffixes[root])

    @staticmethod
    def _root_components(root: FileManagerRoot) -> tuple[str, ...]:
        suffixes = {
            FileManagerRoot.WORKING: (),
            FileManagerRoot.SOURCE_SCOPE: (),
            FileManagerRoot.UPLOAD: ("media",),
            FileManagerRoot.DOWNLOAD: ("static",),
            FileManagerRoot.CONVERSATION: ("sessions",),
        }
        return suffixes[root]

    def _validate_root_and_path(
        self,
        root: FileManagerRoot | str,
        relative_path: str,
    ) -> tuple[FileManagerRoot, str]:
        try:
            resolved_root = FileManagerRoot(root)
        except (TypeError, ValueError) as exc:
            raise FileManagerPathError("Unknown file manager root") from exc
        if resolved_root is FileManagerRoot.RECYCLE:
            raise FileManagerPathError("Recycle paths are not available here")
        normalised_path = self._normalise_relative_path(relative_path)
        if (
            resolved_root is FileManagerRoot.WORKING
            and normalised_path.split("/", 1)[0] in _WORKING_HIDDEN_TOP_LEVEL
        ):
            raise FileManagerPathError("Working path is hidden")
        return resolved_root, normalised_path

    @staticmethod
    def _directory_open_flags() -> int:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if nofollow is None or directory is None:
            raise FileManagerPathError("Safe directory access is unavailable")
        return os.O_RDONLY | os.O_CLOEXEC | nofollow | directory

    @staticmethod
    def _file_open_flags() -> int:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise FileManagerPathError("Safe file access is unavailable")
        return os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | nofollow

    def _open_directory_fd(
        self,
        root: FileManagerRoot,
        relative_path: str,
        *,
        allow_missing_root: bool = False,
        create_source_scope_root: bool = False,
    ) -> int | None:
        parts = self._root_components(root) + tuple(
            filter(None, relative_path.split("/")),
        )
        directory_fd = self._open_root_fd(
            root,
            create_source_scope_root=create_source_scope_root,
        )
        if directory_fd is None:
            if allow_missing_root:
                return None
            raise FileManagerNotFoundError("Directory was not found")
        try:
            for index, part in enumerate(parts):
                try:
                    child_fd = os.open(
                        part,
                        self._directory_open_flags(),
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError as exc:
                    if (
                        allow_missing_root
                        and relative_path == ""
                        and index == len(parts) - 1
                    ):
                        os.close(directory_fd)
                        return None
                    raise FileManagerNotFoundError(
                        "Directory was not found",
                    ) from exc
                except OSError as exc:
                    raise FileManagerPathError(
                        "Unable to open directory",
                    ) from exc
                os.close(directory_fd)
                directory_fd = child_fd
            return directory_fd
        except Exception:
            try:
                os.close(directory_fd)
            except OSError:
                pass
            raise

    def _open_root_fd(
        self,
        root: FileManagerRoot,
        *,
        create_source_scope_root: bool,
    ) -> int | None:
        if root is not FileManagerRoot.SOURCE_SCOPE:
            try:
                return os.open(
                    self._workspace_dir,
                    self._directory_open_flags(),
                )
            except OSError as exc:
                raise FileManagerPathError("Unable to open workspace") from exc
        if (
            self._source_scope_base_dir is None
            or self._source_scope_component is None
        ):
            raise FileManagerPathError("Source-scope root is unavailable")
        try:
            base_fd = os.open(
                self._source_scope_base_dir,
                self._directory_open_flags(),
            )
        except OSError as exc:
            raise FileManagerPathError(
                "Unable to open source-scope root",
            ) from exc
        try:
            if create_source_scope_root:
                try:
                    os.mkdir(
                        self._source_scope_component,
                        0o700,
                        dir_fd=base_fd,
                    )
                except FileExistsError:
                    pass
            try:
                return os.open(
                    self._source_scope_component,
                    self._directory_open_flags(),
                    dir_fd=base_fd,
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise FileManagerPathError(
                    "Unable to open directory",
                ) from exc
        finally:
            os.close(base_fd)

    def _open_file_fd(self, root: FileManagerRoot, relative_path: str) -> int:
        parts = tuple(filter(None, relative_path.split("/")))
        if not parts:
            raise FileManagerPathError("Path is not a file")
        parent_path = "/".join(parts[:-1])
        parent_fd = self._open_directory_fd(root, parent_path)
        assert parent_fd is not None
        try:
            entry_stat = self._stat_entry(parent_fd, parts[-1])
            if not stat_module.S_ISREG(entry_stat.st_mode):
                raise FileManagerPathError("Path is not a regular file")
            return os.open(
                parts[-1],
                self._file_open_flags(),
                dir_fd=parent_fd,
            )
        except FileNotFoundError as exc:
            raise FileManagerNotFoundError("File was not found") from exc
        except OSError as exc:
            raise FileManagerPathError("Unable to open file") from exc
        finally:
            os.close(parent_fd)

    def _reject_symlink_path(
        self,
        root: FileManagerRoot,
        relative_path: str,
    ) -> None:
        parts = tuple(filter(None, relative_path.split("/")))
        parent_fd = self._open_directory_fd(root, "/".join(parts[:-1]))
        assert parent_fd is not None
        try:
            try:
                entry_stat = os.stat(
                    parts[-1],
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if stat_module.S_ISLNK(entry_stat.st_mode):
                raise FileManagerPathError("Symbolic links cannot be followed")
        except OSError as exc:
            raise FileManagerPathError("Unable to inspect path") from exc
        finally:
            os.close(parent_fd)

    def _list_recycle_items(
        self,
        cursor: str | None = None,
    ) -> FileManagerDirectoryListing:
        """Adapt archive metadata without exposing its control-file paths."""

        key = (str(self._workspace_dir), FileManagerRoot.RECYCLE.value, "", "")
        cursor_state = self._validate_cursor_context(
            cursor,
            FileManagerRoot.RECYCLE,
            "",
            None,
        )
        if cursor_state is not None:
            return self._recycle_snapshot_page(
                key,
                self._recycle_index_identity(),
                cursor_state,
            )

        items = [
            listing_item
            for item in self._load_recovered_archive_index().get("items", [])
            if (listing_item := self._recycle_listing_item(item)) is not None
        ]
        items.sort(
            key=lambda item: (
                item.archived_at or datetime.min.replace(tzinfo=timezone.utc),
                item.archive_item_id or "",
            ),
            reverse=True,
        )
        return self._recycle_snapshot_page(
            key,
            self._recycle_index_identity(),
            None,
            items,
        )

    def _recycle_index_identity(self) -> tuple[int, int, int]:
        """Return the archive index identity used to validate a snapshot."""

        index_path = (
            self._workspace_dir / "governance" / "archive" / "index.json"
        )
        try:
            index_stat = index_path.stat()
        except FileNotFoundError:
            return (0, 0, 0)
        return (
            index_stat.st_dev,
            index_stat.st_ino,
            index_stat.st_mtime_ns,
        )

    def _recycle_listing_item(
        self,
        item: object,
    ) -> FileManagerItem | None:
        """Convert one valid archive-index row into a public listing item."""

        if not isinstance(item, dict):
            return None
        try:
            archive_item_id = self._validate_archive_item_id(
                str(item.get("id") or ""),
            )
            original_path = self._validate_archive_original_path(item)
            archived_at = _parse_archive_datetime(item.get("archived_at"))
            size_bytes = int(item.get("size_bytes") or 0)
        except (TypeError, ValueError, FileManagerPathError):
            return None
        return FileManagerItem(
            name=original_path.rsplit("/", maxsplit=1)[-1],
            path=original_path,
            kind=FileManagerItemKind.FILE,
            size_bytes=max(0, size_bytes),
            modified_at=archived_at,
            capabilities=FileManagerCapabilities(),
            archive_item_id=archive_item_id,
            original_path=original_path,
            archived_at=archived_at,
        )

    def _recycle_snapshot_page(
        self,
        key: tuple[str, str, str, str],
        identity: tuple[int, int, int],
        cursor_state: tuple[str, FileManagerItemKind, str | None] | None,
        initial_items: list[FileManagerItem] | None = None,
    ) -> FileManagerDirectoryListing:
        """Build one recycle listing page from a new or validated snapshot."""

        if cursor_state is None:
            snapshot_version = secrets.token_urlsafe(18)
            items = initial_items or []
            snapshot = _DirectorySnapshot(
                snapshot_version,
                identity,
                tuple(items),
                time.monotonic() + _DIRECTORY_SNAPSHOT_TTL_SECONDS,
            )
            self._put_directory_snapshot(key, snapshot)
            start = 0
        else:
            last_path, last_kind, snapshot_version = cursor_state
            if snapshot_version is None:
                raise FileManagerConflictError(
                    "Directory listing changed; refresh and retry",
                )
            snapshot = self._get_directory_snapshot(
                key,
                snapshot_version,
                identity,
            )
            if snapshot is None:
                raise FileManagerConflictError(
                    "Directory listing changed; refresh and retry",
                )
            items = list(snapshot.items)
            start = next(
                (
                    index + 1
                    for index, item in enumerate(items)
                    if item.path == last_path and item.kind is last_kind
                ),
                -1,
            )
            if start < 0:
                raise FileManagerConflictError(
                    "Directory listing changed; refresh and retry",
                )
        page = items[start : start + FILE_MANAGER_PAGE_SIZE]
        next_cursor = None
        if start + FILE_MANAGER_PAGE_SIZE < len(items):
            next_cursor = self._encode_cursor(
                FileManagerRoot.RECYCLE,
                "",
                None,
                page[-1].path,
                page[-1].kind,
                snapshot_version,
            )
        return self._listing(FileManagerRoot.RECYCLE, "", page, next_cursor)

    def _recycle_index_item(
        self,
        archive_item_id: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        archive_item_id = self._validate_archive_item_id(archive_item_id)
        index = self._load_recovered_archive_index()
        for item in index.get("items", []):
            if (
                isinstance(item, dict)
                and str(item.get("id") or "") == archive_item_id
            ):
                return index, item
        raise FileManagerNotFoundError("Recycle item was not found")

    def _load_recovered_archive_index(self) -> dict[str, object]:
        """Recover an interrupted archive transition before exposing metadata."""

        index = self._load_archive_index()
        transition = index.get("transition")
        if not isinstance(transition, dict):
            return index
        operation, item, archive_item_id, original_path = (
            self._archive_transition_details(transition)
        )
        items = self._archive_index_items(index)
        items = self._recover_archive_transition_items(
            operation,
            transition,
            item,
            archive_item_id,
            original_path,
            items,
        )
        recovered = {"version": 1, "items": items}
        self._save_archive_index(recovered)
        return recovered

    def _archive_transition_details(
        self,
        transition: Mapping[str, object],
    ) -> tuple[str, dict[str, object], str, str]:
        """Validate and extract the metadata needed to recover a transition."""
        operation = transition.get("operation")
        item = transition.get("item")
        if not isinstance(operation, str) or not isinstance(item, dict):
            raise FileManagerPathError("Invalid archive transition")
        archive_item_id = self._validate_archive_item_id(
            str(item.get("id") or ""),
        )
        return (
            operation,
            item,
            archive_item_id,
            self._validate_archive_original_path(item),
        )

    @staticmethod
    def _archive_index_items(index: Mapping[str, object]) -> list[object]:
        """Copy the index's item list, tolerating a missing list."""
        existing_items = index.get("items")
        return list(existing_items) if isinstance(existing_items, list) else []

    def _recover_archive_transition_items(
        self,
        operation: str,
        transition: Mapping[str, object],
        item: dict[str, object],
        archive_item_id: str,
        original_path: str,
        items: list[object],
    ) -> list[object]:
        """Apply recovery rules for one interrupted archive operation."""
        payload_exists = self._archive_payload_exists(archive_item_id)
        if operation == "archive":
            return self._add_archive_item_if_missing(
                items,
                item,
                archive_item_id,
                payload_exists,
            )
        if operation == "restore":
            return self._recover_restore_transition_items(
                items,
                transition,
                item,
                archive_item_id,
                original_path,
                payload_exists,
            )
        if operation == "purge":
            return self._recover_purge_transition_items(
                items,
                item,
                archive_item_id,
                payload_exists,
            )
        raise FileManagerPathError("Invalid archive transition")

    def _recover_restore_transition_items(
        self,
        items: list[object],
        transition: Mapping[str, object],
        item: dict[str, object],
        archive_item_id: str,
        original_path: str,
        payload_exists: bool,
    ) -> list[object]:
        """Retain restore metadata unless its payload was safely consumed."""
        recovered = self._add_archive_item_if_missing(
            items,
            item,
            archive_item_id,
            payload_exists,
        )
        root, relative_path, _ = self._archive_item_location(item)
        target_identity = transition.get("target_identity")
        target_matches = isinstance(target_identity, dict) and (
            target_identity == self._root_file_identity(root, relative_path)
        )
        if target_matches and not payload_exists:
            return self._remove_archive_item(recovered, archive_item_id)
        return recovered

    def _recover_purge_transition_items(
        self,
        items: list[object],
        item: dict[str, object],
        archive_item_id: str,
        payload_exists: bool,
    ) -> list[object]:
        """Restore purge metadata only while the archived payload remains."""
        if payload_exists:
            return self._add_archive_item_if_missing(
                items,
                item,
                archive_item_id,
                payload_exists=True,
            )
        return self._remove_archive_item(items, archive_item_id)

    @staticmethod
    def _add_archive_item_if_missing(
        items: list[object],
        item: dict[str, object],
        archive_item_id: str,
        payload_exists: bool,
    ) -> list[object]:
        """Add one recovered item when its payload is still present."""
        item_ids = {
            str(row.get("id") or "") for row in items if isinstance(row, dict)
        }
        if payload_exists and archive_item_id not in item_ids:
            return [*items, item]
        return items

    @staticmethod
    def _remove_archive_item(
        items: list[object],
        archive_item_id: str,
    ) -> list[object]:
        """Remove a recovered archive item without changing malformed rows."""
        return [
            row
            for row in items
            if not isinstance(row, dict)
            or str(row.get("id") or "") != archive_item_id
        ]

    def _archive_payload_exists(self, archive_item_id: str) -> bool:
        try:
            payload_fd, payload_parent_fd = self._open_archive_payload_fd(
                archive_item_id,
            )
        except FileManagerNotFoundError:
            return False
        try:
            return stat_module.S_ISREG(os.fstat(payload_fd).st_mode)
        finally:
            os.close(payload_fd)
            os.close(payload_parent_fd)

    def _workspace_file_exists(self, original_path: str) -> bool:
        root, relative_path = self._root_from_workspace_relative(original_path)
        try:
            file_fd = self._open_file_fd(root, relative_path)
        except FileManagerNotFoundError:
            return False
        try:
            return stat_module.S_ISREG(os.fstat(file_fd).st_mode)
        finally:
            os.close(file_fd)

    def _workspace_file_identity(
        self,
        original_path: str,
    ) -> dict[str, int | str] | None:
        """Return a content-bound identity for one safely opened workspace file."""

        root, relative_path = self._root_from_workspace_relative(original_path)
        return self._root_file_identity(root, relative_path)

    def _root_file_identity(
        self,
        root: FileManagerRoot,
        relative_path: str,
    ) -> dict[str, int | str] | None:
        """Return a content-bound identity for one safely opened root file."""

        try:
            file_fd = self._open_file_fd(root, relative_path)
        except FileManagerNotFoundError:
            return None
        try:
            initial_stat = os.fstat(file_fd)
            digest = hashlib.sha256()
            while chunk := os.read(file_fd, _FILE_READ_CHUNK_BYTES):
                digest.update(chunk)
            final_stat = os.fstat(file_fd)
        finally:
            os.close(file_fd)
        if self._stat_identity(initial_stat) != self._stat_identity(
            final_stat,
        ):
            return None
        return {
            "device": initial_stat.st_dev,
            "inode": initial_stat.st_ino,
            "size": initial_stat.st_size,
            "mtime_ns": initial_stat.st_mtime_ns,
            "sha256": digest.hexdigest(),
        }

    @staticmethod
    def _validate_archive_item_id(archive_item_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", archive_item_id):
            raise FileManagerPathError("Invalid recycle item")
        return archive_item_id

    def _validate_archive_original_path(
        self,
        item: Mapping[str, object],
    ) -> str:
        return self._archive_item_location(item)[2]

    def _archive_item_location(
        self,
        item: Mapping[str, object],
    ) -> tuple[FileManagerRoot, str, str]:
        expected_archive_path = f"{ARCHIVE_FILES_DIR}/{self._validate_archive_item_id(str(item.get('id') or ''))}"
        if str(item.get("archive_path") or "") != expected_archive_path:
            raise FileManagerPathError("Invalid archived payload")
        if "root" in item or "relative_path" in item:
            try:
                root = FileManagerRoot(str(item.get("root") or ""))
            except ValueError as exc:
                raise FileManagerPathError(
                    "Invalid archived original path",
                ) from exc
            relative_path = self._normalise_relative_path(
                str(item.get("relative_path") or ""),
            )
            self._validate_root_and_path(root, relative_path)
            if not relative_path:
                raise FileManagerPathError(
                    "Archived original path must be a file",
                )
            return (
                root,
                relative_path,
                self._archive_display_path(
                    root,
                    relative_path,
                ),
            )
        original_path = self._normalise_relative_path(
            str(item.get("original_path") or ""),
        )
        root, relative_path = self._root_from_workspace_relative(original_path)
        return root, relative_path, original_path

    def _root_from_workspace_relative(
        self,
        workspace_relative_path: str,
    ) -> tuple[FileManagerRoot, str]:
        parts = tuple(filter(None, workspace_relative_path.split("/")))
        if not parts:
            raise FileManagerPathError("Invalid archived original path")
        if parts[0] == "media":
            root, relative_path = FileManagerRoot.UPLOAD, "/".join(parts[1:])
        elif parts[0] == "static":
            root, relative_path = FileManagerRoot.DOWNLOAD, "/".join(parts[1:])
        else:
            root, relative_path = (
                FileManagerRoot.WORKING,
                workspace_relative_path,
            )
        self._validate_root_and_path(root, relative_path)
        if not relative_path:
            raise FileManagerPathError("Archived original path must be a file")
        return root, relative_path

    def _workspace_relative_path(
        self,
        root: FileManagerRoot,
        relative_path: str,
    ) -> str:
        return "/".join((*self._root_components(root), relative_path))

    def _archive_display_path(
        self,
        root: FileManagerRoot,
        relative_path: str,
    ) -> str:
        if root is FileManagerRoot.SOURCE_SCOPE:
            return f"{root.value}/{relative_path}"
        return self._workspace_relative_path(root, relative_path)

    @staticmethod
    def _validate_upload_filename(filename: str) -> str:
        if not isinstance(filename, str) or not filename:
            raise FileManagerPathError("Invalid upload filename")
        if filename in {".", ".."}:
            raise FileManagerPathError("Invalid upload filename")
        if any(character in filename for character in ("/", "\\", "\x00")):
            raise FileManagerPathError("Invalid upload filename")
        return filename

    @staticmethod
    def _temporary_name(basename: str) -> str:
        return f".{basename}.file-manager-{os.getpid()}-{os.urandom(12).hex()}"

    @staticmethod
    def _stat_entry(directory_fd: int, name: str) -> os.stat_result:
        try:
            entry_stat = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise FileManagerNotFoundError("File was not found") from exc
        except OSError as exc:
            raise FileManagerPathError("Unable to inspect file") from exc
        if stat_module.S_ISLNK(entry_stat.st_mode):
            raise FileManagerPathError("Symbolic links cannot be followed")
        return entry_stat

    def _content_revision_for_entry(
        self,
        directory_fd: int,
        name: str,
    ) -> str:
        """Hash one safely opened stable regular-file snapshot."""

        file_fd = os.open(name, self._file_open_flags(), dir_fd=directory_fd)
        try:
            initial_stat = os.fstat(file_fd)
            if not stat_module.S_ISREG(initial_stat.st_mode):
                raise FileManagerPathError("Path is not a regular file")
            digest = hashlib.sha256()
            while chunk := os.read(file_fd, _FILE_READ_CHUNK_BYTES):
                digest.update(chunk)
            final_stat = os.fstat(file_fd)
        except OSError as exc:
            raise FileManagerPathError("Unable to read file revision") from exc
        finally:
            os.close(file_fd)
        if self._stat_identity(initial_stat) != self._stat_identity(
            final_stat,
        ):
            raise FileManagerConflictError("File changed while being read")
        return digest.hexdigest()

    def _open_archive_files_directory_fd(self) -> int:
        archive_fd = self._open_archive_directory_fd()
        try:
            try:
                os.mkdir("files", mode=0o700, dir_fd=archive_fd)
            except FileExistsError:
                pass
            try:
                files_fd = os.open(
                    "files",
                    self._directory_open_flags(),
                    dir_fd=archive_fd,
                )
            except OSError as exc:
                raise FileManagerPathError(
                    "Unable to open file-manager archive",
                ) from exc
            return files_fd
        finally:
            os.close(archive_fd)

    def _open_archive_directory_fd(self) -> int:
        workspace_fd = os.open(
            self._workspace_dir,
            self._directory_open_flags(),
        )
        current_fd = workspace_fd
        try:
            for component in ("governance", "archive"):
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(
                        component,
                        self._directory_open_flags(),
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    raise FileManagerPathError(
                        "Unable to open file-manager archive",
                    ) from exc
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            try:
                os.close(current_fd)
            except OSError:
                pass
            raise

    def _load_archive_index(self) -> dict[str, object]:
        """Read archive metadata through a no-follow descriptor only."""

        archive_fd = self._open_archive_directory_fd()
        try:
            try:
                index_fd = os.open(
                    "index.json",
                    self._file_open_flags(),
                    dir_fd=archive_fd,
                )
            except FileNotFoundError:
                return {"version": 1, "items": []}
            except OSError as exc:
                raise FileManagerPathError(
                    "Unable to open archive index",
                ) from exc
            try:
                index_stat = os.fstat(index_fd)
                if not stat_module.S_ISREG(index_stat.st_mode):
                    raise FileManagerPathError(
                        "Archive index is not a regular file",
                    )
                chunks: list[bytes] = []
                while chunk := os.read(index_fd, _FILE_READ_CHUNK_BYTES):
                    chunks.append(chunk)
            finally:
                os.close(index_fd)
        finally:
            os.close(archive_fd)
        try:
            data = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Ignoring malformed file-manager archive index")
            return {"version": 1, "items": []}
        if not isinstance(data, dict):
            return {"version": 1, "items": []}
        items = data.get("items")
        transition = data.get("transition")
        return {
            "version": 1,
            "items": items if isinstance(items, list) else [],
            "transition": transition if isinstance(transition, dict) else None,
        }

    def _save_archive_index(self, data: Mapping[str, object]) -> None:
        """Atomically publish a no-follow archive index beneath governance."""

        items = data.get("items")
        if not isinstance(items, list):
            items = []
        payload: dict[str, object] = {"version": 1, "items": items}
        if isinstance(data.get("transition"), dict):
            payload["transition"] = data["transition"]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        archive_fd = self._open_archive_directory_fd()
        temporary_name = self._temporary_name("index.json")
        temporary_fd: int | None = None
        published = False
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=archive_fd,
            )
            _write_all(temporary_fd, encoded)
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            os.replace(
                temporary_name,
                "index.json",
                src_dir_fd=archive_fd,
                dst_dir_fd=archive_fd,
            )
            published = True
            try:
                os.fsync(archive_fd)
            except OSError as exc:
                raise FileManagerOutcomeUncertainError(
                    "File-manager mutation outcome is uncertain",
                ) from exc
        except FileManagerOutcomeUncertainError:
            raise
        except OSError as exc:
            raise FileManagerPathError("Unable to save archive index") from exc
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if not published:
                try:
                    os.unlink(temporary_name, dir_fd=archive_fd)
                except FileNotFoundError:
                    pass
            os.close(archive_fd)

    def _open_archive_payload_fd(
        self,
        archive_item_id: str,
    ) -> tuple[int, int]:
        archive_item_id = self._validate_archive_item_id(archive_item_id)
        archive_parent_fd = self._open_archive_files_directory_fd()
        try:
            archive_fd = os.open(
                archive_item_id,
                self._file_open_flags(),
                dir_fd=archive_parent_fd,
            )
            return archive_fd, archive_parent_fd
        except FileNotFoundError as exc:
            os.close(archive_parent_fd)
            raise FileManagerNotFoundError(
                "Archived payload was not found",
            ) from exc
        except OSError as exc:
            os.close(archive_parent_fd)
            raise FileManagerPathError(
                "Unable to open archived payload",
            ) from exc

    def _copy_fd_to_new_file(
        self,
        source_fd: int,
        destination_directory_fd: int,
        destination_name: str,
    ) -> None:
        temporary_name = self._temporary_name(destination_name)
        temporary_fd: int | None = None
        published = False
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=destination_directory_fd,
            )
            os.lseek(source_fd, 0, os.SEEK_SET)
            while chunk := os.read(source_fd, _FILE_READ_CHUNK_BYTES):
                _write_all(temporary_fd, chunk)
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            try:
                os.link(
                    temporary_name,
                    destination_name,
                    src_dir_fd=destination_directory_fd,
                    dst_dir_fd=destination_directory_fd,
                    follow_symlinks=False,
                )
                published = True
            except FileExistsError as exc:
                raise FileManagerConflictError(
                    "Destination already exists",
                ) from exc
            os.unlink(temporary_name, dir_fd=destination_directory_fd)
            os.fsync(destination_directory_fd)
        except OSError as exc:
            if published:
                raise FileManagerOutcomeUncertainError(
                    "File-manager mutation outcome is uncertain",
                ) from exc
            raise FileManagerPathError(
                "Unable to write file-manager payload",
            ) from exc
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=destination_directory_fd)
            except FileNotFoundError:
                pass

    def _unlink_archive_payload_if_present(
        self,
        archive_directory_fd: int,
        archive_item_id: str,
    ) -> None:
        try:
            entry_stat = self._stat_entry(
                archive_directory_fd,
                archive_item_id,
            )
            if stat_module.S_ISREG(entry_stat.st_mode):
                os.unlink(archive_item_id, dir_fd=archive_directory_fd)
                os.fsync(archive_directory_fd)
        except FileManagerNotFoundError:
            pass
        except OSError:
            logger.exception("Unable to remove unindexed archive payload")

    def _unlink_archive_payload_confirmed(
        self,
        archive_directory_fd: int,
        archive_item_id: str,
        expected_stat: os.stat_result,
    ) -> None:
        current_stat = self._stat_entry(archive_directory_fd, archive_item_id)
        if self._stat_identity(current_stat) != self._stat_identity(
            expected_stat,
        ):
            raise FileManagerConflictError("Archived payload changed")
        os.unlink(archive_item_id, dir_fd=archive_directory_fd)
        try:
            os.fsync(archive_directory_fd)
        except OSError as exc:
            raise FileManagerOutcomeUncertainError(
                "File-manager mutation outcome is uncertain",
            ) from exc

    def _unlink_if_same_file(
        self,
        root: FileManagerRoot,
        relative_path: str,
        expected_stat: os.stat_result,
    ) -> None:
        parts = tuple(filter(None, relative_path.split("/")))
        parent_fd = self._open_directory_fd(root, "/".join(parts[:-1]))
        assert parent_fd is not None
        try:
            current_stat = self._stat_entry(parent_fd, parts[-1])
            if self._stat_identity(current_stat) != self._stat_identity(
                expected_stat,
            ):
                raise FileManagerConflictError(
                    "File changed while being archived",
                )
            os.unlink(parts[-1], dir_fd=parent_fd)
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise FileManagerOutcomeUncertainError(
                    "File-manager mutation outcome is uncertain",
                ) from exc
        except OSError as exc:
            raise FileManagerPathError("Unable to archive file") from exc
        finally:
            os.close(parent_fd)

    @staticmethod
    def _normalise_relative_path(relative_path: str) -> str:
        if not isinstance(relative_path, str) or "\\" in relative_path:
            raise FileManagerPathError("Path must be a relative POSIX path")
        if not relative_path:
            return ""
        raw_path = PurePosixPath(relative_path)
        if raw_path.is_absolute() or relative_path in {".", ".."}:
            raise FileManagerPathError("Path must be relative")
        parts = raw_path.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise FileManagerPathError("Path must not escape its root")
        return "/".join(parts)

    def _directory_candidates(
        self,
        root: FileManagerRoot,
        parent_path: str,
        directory_fd: int,
        query: str | None,
        after_sort_key: (
            tuple[
                int,
                tuple[tuple[int, int | str], ...],
                str,
            ]
            | None
        ),
    ) -> list[_DirectoryCandidate]:
        query_value = query.casefold() if query else None

        def candidates():
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    if (
                        root is FileManagerRoot.WORKING
                        and parent_path == ""
                        and entry.name in _WORKING_HIDDEN_TOP_LEVEL
                    ):
                        continue
                    if (
                        query_value
                        and query_value not in entry.name.casefold()
                    ):
                        continue
                    try:
                        entry_stat = os.stat(
                            entry.name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        # An entry removed after scandir is safe to omit.
                        continue
                    except OSError as exc:
                        raise FileManagerPathError(
                            "Unable to inspect directory",
                        ) from exc
                    sort_key = self._entry_sort_key(entry.name, entry_stat)
                    if (
                        after_sort_key is not None
                        and sort_key <= after_sort_key
                    ):
                        continue
                    yield _DirectoryCandidate(
                        name=entry.name,
                        entry_stat=entry_stat,
                        sort_key=sort_key,
                    )

        try:
            return sorted(
                candidates(),
                key=lambda candidate: candidate.sort_key,
            )
        except FileManagerPathError:
            raise
        except OSError as exc:
            raise FileManagerPathError("Unable to list directory") from exc

    @staticmethod
    def _get_directory_snapshot(
        key: tuple[str, str, str, str],
        version: str,
        identity: tuple[int, int, int],
    ) -> _DirectorySnapshot | None:
        with _DIRECTORY_SNAPSHOTS_LOCK:
            snapshot = _DIRECTORY_SNAPSHOTS.get(key)
            if (
                snapshot is None
                or snapshot.version != version
                or snapshot.identity != identity
                or snapshot.expires_at <= time.monotonic()
            ):
                _DIRECTORY_SNAPSHOTS.pop(key, None)
                return None
            _DIRECTORY_SNAPSHOTS.move_to_end(key)
            return snapshot

    @staticmethod
    def _put_directory_snapshot(
        key: tuple[str, str, str, str],
        snapshot: _DirectorySnapshot,
    ) -> None:
        with _DIRECTORY_SNAPSHOTS_LOCK:
            _DIRECTORY_SNAPSHOTS.pop(key, None)
            workspace_key = key[0]
            while (
                sum(
                    existing_key[0] == workspace_key
                    for existing_key in _DIRECTORY_SNAPSHOTS
                )
                >= _DIRECTORY_SNAPSHOT_WORKSPACE_CAPACITY
            ):
                oldest_workspace_key = next(
                    existing_key
                    for existing_key in _DIRECTORY_SNAPSHOTS
                    if existing_key[0] == workspace_key
                )
                _DIRECTORY_SNAPSHOTS.pop(oldest_workspace_key)
            _DIRECTORY_SNAPSHOTS[key] = snapshot
            _DIRECTORY_SNAPSHOTS.move_to_end(key)
            while len(_DIRECTORY_SNAPSHOTS) > _DIRECTORY_SNAPSHOT_CAPACITY:
                _DIRECTORY_SNAPSHOTS.popitem(last=False)

    def _item_for_entry(
        self,
        root: FileManagerRoot,
        parent_path: str,
        name: str,
        entry_stat: os.stat_result,
    ) -> FileManagerItem:
        path = f"{parent_path}/{name}" if parent_path else name
        if stat_module.S_ISLNK(entry_stat.st_mode):
            return FileManagerItem(
                name=name,
                path=path,
                kind=FileManagerItemKind.SYMLINK,
                capabilities=FileManagerCapabilities(),
            )

        if stat_module.S_ISDIR(entry_stat.st_mode):
            kind = FileManagerItemKind.DIRECTORY
        elif stat_module.S_ISREG(entry_stat.st_mode):
            kind = FileManagerItemKind.FILE
        else:
            kind = FileManagerItemKind.SPECIAL
        capabilities = root_capabilities(root)
        if kind is FileManagerItemKind.FILE:
            capabilities = capabilities.model_copy(
                update={"browse": False, "upload": False},
            )
        elif (
            kind is FileManagerItemKind.DIRECTORY
            and root is FileManagerRoot.WORKING
            and not parent_path
            and name in _WORKING_BACKING_TOP_LEVEL
        ):
            capabilities = capabilities.model_copy(update={"archive": False})
        elif kind is FileManagerItemKind.SPECIAL:
            capabilities = FileManagerCapabilities()
        return FileManagerItem(
            name=name,
            path=path,
            kind=kind,
            size_bytes=(
                None
                if kind is FileManagerItemKind.DIRECTORY
                else entry_stat.st_size
            ),
            modified_at=datetime.fromtimestamp(
                entry_stat.st_mtime,
                tz=timezone.utc,
            ),
            capabilities=capabilities,
        )

    @staticmethod
    def _contains_disallowed_control_character(text: str) -> bool:
        return any(
            unicodedata.category(character) == "Cc"
            and character not in {"\t", "\n", "\r"}
            for character in text
        )

    @staticmethod
    def _decode_preview_bytes(preview_bytes: bytes) -> str:
        try:
            return preview_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Complete-file validation succeeded, so only the final preview
            # boundary may cut one otherwise-valid multi-byte code point.
            if exc.reason != "unexpected end of data" or exc.end != len(
                preview_bytes,
            ):
                raise FileManagerPathError(
                    "Invalid UTF-8 preview boundary",
                ) from exc
            return preview_bytes[: exc.start].decode("utf-8")

    @staticmethod
    def _stat_identity(
        entry_stat: os.stat_result,
    ) -> tuple[int, int, int, int]:
        return (
            entry_stat.st_dev,
            entry_stat.st_ino,
            entry_stat.st_size,
            entry_stat.st_mtime_ns,
        )

    @staticmethod
    def _revision_for_stat(entry_stat: os.stat_result) -> str:
        return ":".join(
            str(value)
            for value in (
                entry_stat.st_dev,
                entry_stat.st_ino,
                entry_stat.st_mtime_ns,
                entry_stat.st_size,
            )
        )

    @staticmethod
    def _entry_sort_key(
        name: str,
        entry_stat: os.stat_result,
    ) -> tuple[int, tuple[tuple[int, int | str], ...], str]:
        natural_name = tuple(
            (0, int(part)) if part.isdecimal() else (1, part.casefold())
            for part in _NATURAL_PARTS.split(name)
            if part != ""
        )
        return -entry_stat.st_mtime_ns, natural_name, name

    @staticmethod
    def _sort_key_for_cursor(
        last_path: str,
        last_kind: FileManagerItemKind,
    ) -> tuple[int, tuple[tuple[int, int | str], ...], str]:
        name = last_path.rsplit("/", maxsplit=1)[-1]
        kind_rank = 0 if last_kind is FileManagerItemKind.DIRECTORY else 1
        natural_name = tuple(
            (0, int(part)) if part.isdecimal() else (1, part.casefold())
            for part in _NATURAL_PARTS.split(name)
            if part != ""
        )
        return kind_rank, natural_name, name

    def _listing(
        self,
        root: FileManagerRoot,
        path: str,
        items: list[FileManagerItem],
        next_cursor: str | None = None,
    ) -> FileManagerDirectoryListing:
        first_directory = next(
            (
                item.path
                for item in items
                if item.kind is FileManagerItemKind.DIRECTORY
            ),
            None,
        )
        return FileManagerDirectoryListing(
            root=root,
            path=path,
            items=items,
            next_cursor=next_cursor,
            has_child_directory=first_directory is not None,
            first_child_directory=first_directory,
            capabilities=root_capabilities(root),
        )

    def _cursor_sort_key(
        self,
        cursor: str | None,
        root: FileManagerRoot,
        path: str,
        query: str | None,
    ) -> tuple[int, tuple[tuple[int, int | str], ...], str] | None:
        cursor_state = self._validate_cursor_context(cursor, root, path, query)
        if cursor_state is None:
            return None
        last_path, last_kind, _ = cursor_state
        return self._sort_key_for_cursor(last_path, last_kind)

    def _validate_cursor_context(
        self,
        cursor: str | None,
        root: FileManagerRoot,
        path: str,
        query: str | None,
    ) -> tuple[str, FileManagerItemKind, str | None] | None:
        if cursor is None:
            return None
        try:
            encoded_payload = base64.b64decode(
                cursor.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            cursor_payload = json.loads(encoded_payload)
            signature = cursor_payload.pop("signature")
            if not isinstance(signature, str) or not hmac.compare_digest(
                signature,
                self._cursor_signature(cursor_payload),
            ):
                raise ValueError
            if cursor_payload.get("version") == 2:
                expected_payload = {
                    "root": root.value,
                    "path": path,
                    "query": query or "",
                    "version": 2,
                    "last_path": cursor_payload["last_path"],
                    "last_kind": cursor_payload["last_kind"],
                }
                snapshot_version = None
            else:
                expected_payload = {
                    "root": root.value,
                    "path": path,
                    "query": query or "",
                    "version": 3,
                    "last_path": cursor_payload["last_path"],
                    "last_kind": cursor_payload["last_kind"],
                    "snapshot": cursor_payload["snapshot"],
                }
                snapshot_version = cursor_payload["snapshot"]
            if cursor_payload != expected_payload:
                raise ValueError
            last_path = cursor_payload["last_path"]
            last_kind = FileManagerItemKind(cursor_payload["last_kind"])
            parent_path = last_path.rpartition("/")[0]
            if (
                not isinstance(last_path, str)
                or self._normalise_relative_path(last_path) != last_path
                or parent_path != path
            ):
                raise ValueError
            if snapshot_version is not None and not isinstance(
                snapshot_version,
                str,
            ):
                raise ValueError
            return last_path, last_kind, snapshot_version
        except (
            AttributeError,
            binascii.Error,
            KeyError,
            StopIteration,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise FileManagerPathError("Invalid directory cursor") from exc

    @staticmethod
    def _decode_text_sample(
        sample: bytes,
        *,
        sample_is_truncated: bool,
    ) -> str | None:
        try:
            return sample.decode("utf-8")
        except UnicodeDecodeError as exc:
            if (
                not sample_is_truncated
                or exc.reason != "unexpected end of data"
                or exc.end != len(sample)
            ):
                return None
            return sample[: exc.start].decode("utf-8")

    def _encode_cursor(
        self,
        root: FileManagerRoot,
        path: str,
        query: str | None,
        last_path: str,
        last_kind: FileManagerItemKind,
        snapshot_version: str,
    ) -> str:
        payload: dict[str, str | int] = {
            "root": root.value,
            "path": path,
            "query": query or "",
            "version": 3,
            "last_path": last_path,
            "last_kind": last_kind.value,
            "snapshot": snapshot_version,
        }
        payload["signature"] = self._cursor_signature(payload)
        return base64.urlsafe_b64encode(
            json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        ).decode("ascii")

    def _cursor_signature(self, payload: Mapping[str, object]) -> str:
        canonical_payload = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hmac.new(
            self._cursor_secret,
            canonical_payload,
            hashlib.sha256,
        ).hexdigest()
