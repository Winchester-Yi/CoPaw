# -*- coding: utf-8 -*-
"""JSON-based chat repository."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from swe.runtime_workers import run_runtime_state_work

from .base import BaseChatRepository
from ...channels.schema import DEFAULT_CHANNEL
from ..models import ChatSpec, ChatsFile

_LOAD_STABLE_READ_ATTEMPTS = 3


class _UnstableChatRepositoryReadError(RuntimeError):
    """Raised when chats.json cannot be read in a stable state."""


@dataclass(frozen=True)
class _FileSignature:
    """Observable chats.json state used to validate in-process snapshots."""

    exists: bool
    mtime_ns: int | None = None
    ctime_ns: int | None = None
    size: int | None = None
    inode: int | None = None
    digest: str | None = None


@dataclass(frozen=True)
class _SnapshotState:
    """Private immutable snapshot container prepared off the event loop."""

    signature: _FileSignature
    chats_file: ChatsFile
    chat_index: dict[str, ChatSpec]
    session_index: dict[tuple[str, str], ChatSpec]
    user_session_index: dict[tuple[str, str, str], ChatSpec]


class JsonChatRepository(BaseChatRepository):
    """chats.json repository (single-file storage).

    Stores chat_id (UUID) -> session_id mappings in a JSON file.
    Similar to JsonJobRepository pattern from crons.

    Notes:
    - Session creation uses a cross-process advisory file lock.
    - Atomic write: write tmp then replace.
    """

    def __init__(self, path: Path | str):
        """Initialize JSON chat repository.

        Args:
            path: Path to chats.json file
        """
        if isinstance(path, str):
            path = Path(path)
        self._path = path.expanduser()
        self._snapshot_signature: _FileSignature | None = None
        self._snapshot: ChatsFile | None = None
        self._chat_index: dict[str, ChatSpec] = {}
        self._session_index: dict[tuple[str, str], ChatSpec] = {}
        self._user_session_index: dict[tuple[str, str, str], ChatSpec] = {}
        self._refresh_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """Get the repository file path."""
        return self._path

    def _read_file_state(self) -> tuple[_FileSignature, bytes] | None:
        try:
            before_stat = self._path.stat()
        except FileNotFoundError:
            try:
                self._path.stat()
            except FileNotFoundError:
                return _FileSignature(exists=False), b""
            return None

        try:
            contents = self._path.read_bytes()
            after_stat = self._path.stat()
        except FileNotFoundError:
            return None

        before_identity = (
            before_stat.st_size,
            before_stat.st_mtime_ns,
            before_stat.st_ctime_ns,
            before_stat.st_ino,
        )
        after_identity = (
            after_stat.st_size,
            after_stat.st_mtime_ns,
            after_stat.st_ctime_ns,
            after_stat.st_ino,
        )
        if before_identity != after_identity:
            return None

        signature = _FileSignature(
            exists=True,
            mtime_ns=after_stat.st_mtime_ns,
            ctime_ns=after_stat.st_ctime_ns,
            size=after_stat.st_size,
            inode=after_stat.st_ino,
            digest=hashlib.sha256(contents).hexdigest(),
        )
        return signature, contents

    def _file_signature(self) -> _FileSignature | None:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            return _FileSignature(exists=False)
        return _FileSignature(
            exists=True,
            mtime_ns=stat.st_mtime_ns,
            ctime_ns=stat.st_ctime_ns,
            size=stat.st_size,
            inode=stat.st_ino,
        )

    @staticmethod
    def _signature_matches(
        left: _FileSignature | None,
        right: _FileSignature | None,
    ) -> bool:
        """Compare file identity without reading its contents."""
        if left is None or right is None:
            return left is right
        return (
            left.exists,
            left.mtime_ns,
            left.ctime_ns,
            left.size,
            left.inode,
        ) == (
            right.exists,
            right.mtime_ns,
            right.ctime_ns,
            right.size,
            right.inode,
        )

    def _load_sync(self) -> tuple[_FileSignature, ChatsFile]:
        for _ in range(_LOAD_STABLE_READ_ATTEMPTS):
            state = self._read_file_state()
            if state is None:
                continue
            signature, contents = state

            if not signature.exists:
                chats_file = ChatsFile(version=1, chats=[])
            else:
                data = json.loads(contents.decode("utf-8"))
                chats_file = ChatsFile.model_validate(data)

            return signature, chats_file

        raise _UnstableChatRepositoryReadError(
            "unstable chats repository read after "
            f"{_LOAD_STABLE_READ_ATTEMPTS} attempts: {self._path}",
        )

    def _save_sync(self, chats_file: ChatsFile) -> _FileSignature | None:
        with self._write_lock_sync():
            return self._save_sync_unlocked(chats_file)

    def _save_sync_unlocked(
        self,
        chats_file: ChatsFile,
    ) -> _FileSignature | None:
        # Create parent directory if needed
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file first (atomic write)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = chats_file.model_dump(mode="json")

        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Atomic replace (shutil.move handles cross-disk on Windows)
        shutil.move(str(tmp_path), str(self._path))
        return self._file_signature()

    @contextmanager
    def _write_lock_sync(self):
        """Serialize every JSON mutation across worker processes."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(f"{self._path.suffix}.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _prepare_snapshot_sync(
        self,
        signature: _FileSignature,
        chats_file: ChatsFile,
    ) -> _SnapshotState:
        snapshot = chats_file.model_copy(deep=True)
        session_index: dict[tuple[str, str], ChatSpec] = {}
        user_session_index: dict[tuple[str, str, str], ChatSpec] = {}
        for chat in snapshot.chats:
            key = (chat.channel, chat.session_id)
            existing = session_index.get(key)
            if existing is None or chat.updated_at > existing.updated_at:
                session_index[key] = chat
            user_key = (chat.channel, chat.session_id, chat.user_id)
            existing = user_session_index.get(user_key)
            if existing is None or chat.updated_at > existing.updated_at:
                user_session_index[user_key] = chat
        return _SnapshotState(
            signature=signature,
            chats_file=snapshot,
            chat_index={chat.id: chat for chat in snapshot.chats},
            session_index=session_index,
            user_session_index=user_session_index,
        )

    def _load_and_prepare_snapshot_sync(
        self,
    ) -> tuple[_SnapshotState, ChatsFile]:
        signature, chats_file = self._load_sync()
        caller_chats_file = chats_file.model_copy(deep=True)
        return (
            self._prepare_snapshot_sync(signature, chats_file),
            caller_chats_file,
        )

    def _save_and_prepare_snapshot_sync(
        self,
        chats_file: ChatsFile,
        *,
        already_locked: bool = False,
    ) -> _SnapshotState | None:
        chats_file_to_save = chats_file.model_copy(deep=True)
        signature = (
            self._save_sync_unlocked(chats_file_to_save)
            if already_locked
            else self._save_sync(chats_file_to_save)
        )
        if signature is None:
            return None
        return self._prepare_snapshot_sync(signature, chats_file_to_save)

    @staticmethod
    def _copy_chat_sync(chat: ChatSpec | None) -> ChatSpec | None:
        if chat is None:
            return None
        return chat.model_copy(deep=True)

    @staticmethod
    def _find_chat_copy_sync(
        chats_file: ChatsFile,
        chat_id: str,
    ) -> ChatSpec | None:
        for chat in chats_file.chats:
            if chat.id == chat_id:
                return chat.model_copy(deep=True)
        return None

    def _set_snapshot(self, snapshot_state: _SnapshotState | None) -> None:
        if snapshot_state is None:
            self._snapshot_signature = None
            self._snapshot = None
            self._chat_index = {}
            self._session_index = {}
            self._user_session_index = {}
            return

        self._snapshot_signature = snapshot_state.signature
        self._snapshot = snapshot_state.chats_file
        self._chat_index = snapshot_state.chat_index
        self._session_index = snapshot_state.session_index
        self._user_session_index = snapshot_state.user_session_index

    async def load(self) -> ChatsFile:
        """Load chat specs from JSON file.

        Returns:
            ChatsFile with all chat specs
        """
        snapshot_state, chats_file = await run_runtime_state_work(
            self._load_and_prepare_snapshot_sync,
        )
        self._set_snapshot(snapshot_state)
        return chats_file

    async def _ensure_snapshot(self) -> None:
        signature = await run_runtime_state_work(self._file_signature)
        if self._snapshot is not None and self._signature_matches(
            self._snapshot_signature,
            signature,
        ):
            return
        async with self._refresh_lock:
            signature = await run_runtime_state_work(self._file_signature)
            if self._snapshot is None or not self._signature_matches(
                self._snapshot_signature,
                signature,
            ):
                await self.load()

    @staticmethod
    def _copy_filtered_chats_sync(
        chats: tuple[ChatSpec, ...],
        user_id: str | None,
        channel: str | None,
    ) -> list[ChatSpec]:
        return [
            chat.model_copy(deep=True)
            for chat in chats
            if (user_id is None or chat.user_id == user_id)
            and (channel is None or chat.channel == channel)
        ]

    async def filter_chats(
        self,
        user_id: str | None = None,
        channel: str | None = None,
    ) -> list[ChatSpec]:
        """Filter chats from the current immutable snapshot."""
        await self._ensure_snapshot()
        snapshot = self._snapshot
        if snapshot is None:
            return []
        return await run_runtime_state_work(
            self._copy_filtered_chats_sync,
            tuple(snapshot.chats),
            user_id,
            channel,
        )

    async def get_chat_by_id(
        self,
        session_id: str,
        user_id: str,
        channel: str = DEFAULT_CHANNEL,
    ) -> ChatSpec | None:
        """Find a logical session from the current immutable snapshot."""
        await self._ensure_snapshot()
        chat = self._user_session_index.get((channel, session_id, user_id))
        return await run_runtime_state_work(self._copy_chat_sync, chat)

    async def save(self, chats_file: ChatsFile) -> None:
        """Save chat specs to JSON file atomically.

        Args:
            chats_file: ChatsFile to persist
        """
        snapshot_state = await run_runtime_state_work(
            self._save_and_prepare_snapshot_sync,
            chats_file,
        )
        self._set_snapshot(snapshot_state)

    def _create_chat_if_absent_by_session_sync(
        self,
        spec: ChatSpec,
    ) -> tuple[_SnapshotState | None, ChatSpec, bool]:
        with self._write_lock_sync():
            _, chats_file = self._load_sync()
            for existing in chats_file.chats:
                if (
                    existing.session_id == spec.session_id
                    and existing.user_id == spec.user_id
                    and existing.channel == spec.channel
                ):
                    return None, existing.model_copy(deep=True), False
            chats_file.chats.append(spec.model_copy(deep=True))
            snapshot_state = self._save_and_prepare_snapshot_sync(
                chats_file,
                already_locked=True,
            )
            return snapshot_state, spec.model_copy(deep=True), True

    def _upsert_chat_sync(self, spec: ChatSpec) -> _SnapshotState | None:
        with self._write_lock_sync():
            _, chats_file = self._load_sync()
            for index, existing in enumerate(chats_file.chats):
                if existing.id == spec.id:
                    chats_file.chats[index] = spec.model_copy(deep=True)
                    break
            else:
                chats_file.chats.append(spec.model_copy(deep=True))
            return self._save_and_prepare_snapshot_sync(
                chats_file,
                already_locked=True,
            )

    def _delete_chats_sync(
        self,
        chat_ids: list[str],
    ) -> tuple[_SnapshotState | None, bool]:
        if not chat_ids:
            return None, False
        with self._write_lock_sync():
            _, chats_file = self._load_sync()
            before = len(chats_file.chats)
            chats_file.chats = [
                chat for chat in chats_file.chats if chat.id not in chat_ids
            ]
            if len(chats_file.chats) == before:
                return None, False
            return (
                self._save_and_prepare_snapshot_sync(
                    chats_file,
                    already_locked=True,
                ),
                True,
            )

    async def create_chat_if_absent_by_session(
        self,
        spec: ChatSpec,
    ) -> tuple[ChatSpec, bool]:
        """Atomically create one Chat for a logical session across workers."""
        snapshot_state, chat, created = await run_runtime_state_work(
            self._create_chat_if_absent_by_session_sync,
            spec,
        )
        self._set_snapshot(snapshot_state)
        return chat, created

    async def upsert_chat(self, spec: ChatSpec) -> None:
        snapshot_state = await run_runtime_state_work(
            self._upsert_chat_sync,
            spec,
        )
        self._set_snapshot(snapshot_state)

    async def delete_chats(self, chat_ids: list[str]) -> bool:
        snapshot_state, deleted = await run_runtime_state_work(
            self._delete_chats_sync,
            chat_ids,
        )
        self._set_snapshot(snapshot_state)
        return deleted

    async def get_chat(self, chat_id: str) -> ChatSpec | None:
        """Get chat spec by chat_id (UUID), reusing a valid snapshot index."""
        signature = await run_runtime_state_work(self._file_signature)
        if self._snapshot is not None and self._signature_matches(
            self._snapshot_signature,
            signature,
        ):
            return await run_runtime_state_work(
                self._copy_chat_sync,
                self._chat_index.get(chat_id),
            )

        chats_file = await self.load()
        if self._snapshot is not None:
            return await run_runtime_state_work(
                self._copy_chat_sync,
                self._chat_index.get(chat_id),
            )
        return await run_runtime_state_work(
            self._find_chat_copy_sync,
            chats_file,
            chat_id,
        )

    async def get_chat_id_by_session(
        self,
        session_id: str,
        channel: str,
    ) -> str | None:
        """Return a session's latest chat using the snapshot index."""
        signature = await run_runtime_state_work(self._file_signature)
        if self._snapshot is None or not self._signature_matches(
            self._snapshot_signature,
            signature,
        ):
            await self.load()
        chat = self._session_index.get((channel, session_id))
        return chat.id if chat is not None else None
