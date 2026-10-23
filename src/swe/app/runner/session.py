# -*- coding: utf-8 -*-
"""Safe JSON session with filename sanitization for cross-platform
compatibility.

Windows filenames cannot contain: \\ / : * ? " < > |
This module wraps agentscope's SessionBase so that session_id and user_id
are sanitized before being used as filenames.
"""

import asyncio
import json
import logging
import os
import re
import threading
from pathlib import Path

from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import Any, AsyncIterator, Callable, Mapping, Sequence, Union

from agentscope.session import SessionBase

from swe.app.runner.session_lock import (
    AsyncSessionFileLock,
    SessionLockTimeout,
    get_session_lock_path,
)
from swe.runtime_workers import run_runtime_state_work
from swe.performance_metrics import measure_storage_operation
from .turn_ledger import (
    TurnLedger,
    atomic_json,
    terminal_fingerprints,
    observe_hot_turns,
)

logger = logging.getLogger(__name__)
SESSION_SKILL_SNAPSHOT_STATE_KEY = "session_skill_snapshot"
_SESSION_WRITE_LOCKS: dict[
    tuple[asyncio.AbstractEventLoop, str],
    asyncio.Lock,
] = {}
_SESSION_WRITE_LOCKS_GUARD = threading.Lock()
_EMPTY_ACTIVE_SESSION_EXECUTIONS: Mapping[str, "SessionExecution"] = (
    MappingProxyType({})
)
_ACTIVE_SESSION_EXECUTIONS: ContextVar[Mapping[str, "SessionExecution"]] = (
    ContextVar(
        "active_session_executions",
        default=_EMPTY_ACTIVE_SESSION_EXECUTIONS,
    )
)


# Characters forbidden in Windows filenames
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')
_ALLOWED_MESSAGE_ROLES = {"user", "assistant", "system"}


def sanitize_filename(name: str) -> str:
    """Replace characters that are illegal in Windows filenames with ``--``.

    >>> sanitize_filename('discord:dm:12345')
    'discord--dm--12345'
    >>> sanitize_filename('normal-name')
    'normal-name'
    """
    return _UNSAFE_FILENAME_RE.sub("--", name)


def _normalize_state_for_load(value):
    """在反序列化前把旧角色单向迁移到 AgentScope 可接受角色。"""
    if isinstance(value, list):
        return [_normalize_state_for_load(item) for item in value]

    if not isinstance(value, dict):
        return value

    normalized = {
        key: _normalize_state_for_load(item) for key, item in value.items()
    }
    role = normalized.get("role")
    if (
        isinstance(role, str)
        and role not in _ALLOWED_MESSAGE_ROLES
        and "name" in normalized
        and "content" in normalized
    ):
        normalized["role"] = "system"
    return normalized


def _get_session_write_lock(file_path: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    normalized_path = os.path.normcase(os.path.abspath(file_path))
    lock_key = (loop, normalized_path)
    with _SESSION_WRITE_LOCKS_GUARD:
        lock = _SESSION_WRITE_LOCKS.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            _SESSION_WRITE_LOCKS[lock_key] = lock
        return lock


def _get_normalized_session_path(file_path: str) -> str:
    return os.path.normcase(os.path.abspath(file_path))


def _reject_short_lock_during_execution(session_save_path: str) -> None:
    normalized_path = _get_normalized_session_path(session_save_path)
    execution = _ACTIVE_SESSION_EXECUTIONS.get().get(normalized_path)
    if execution is not None and execution.is_active:
        raise RuntimeError(
            "cannot use a short-lock API during an active session execution; "
            "use its transaction instead",
        )


@asynccontextmanager
async def _session_write_scope(
    session_save_path: str,
    *,
    timeout_seconds: float | None = None,
) -> AsyncIterator[None]:
    _reject_short_lock_during_execution(session_save_path)
    lock = _get_session_write_lock(session_save_path)
    if timeout_seconds is None:
        await lock.acquire()
    else:
        await asyncio.wait_for(lock.acquire(), timeout=timeout_seconds)
    try:
        try:
            async with AsyncSessionFileLock(
                get_session_lock_path(session_save_path),
                timeout_seconds=timeout_seconds,
            ):
                yield
        except SessionLockTimeout as exc:
            raise TimeoutError(str(exc)) from exc
    finally:
        lock.release()


def _read_json_state_sync(
    session_save_path: str,
    *,
    allow_not_exist: bool,
    allow_empty: bool = False,
) -> tuple[bool, dict[str, Any]]:
    if not os.path.exists(session_save_path):
        if allow_not_exist:
            return False, {}
        raise ValueError(
            "Failed to load session state for file "
            f"{session_save_path} because it does not exist.",
        )

    with open(
        session_save_path,
        "r",
        encoding="utf-8",
        errors="surrogatepass",
    ) as file:
        content = file.read()

    if allow_empty and not content.strip():
        return True, {}

    states = json.loads(content)
    if not isinstance(states, dict):
        raise ValueError(
            f"Session file {session_save_path} does not contain "
            "a JSON object.",
        )
    _validate_session_schema(states)
    return True, states


def _validate_session_schema(state):
    schema = state.get("schema_version", 1)
    if type(schema) is not int or schema not in (1, 2, 3):
        raise ValueError("Unsupported session schema version")


def _preserve_ledger_metadata(state, previous):
    """Payload replacements cannot downgrade or lose committed ledger roots."""
    if previous.get("schema_version") != 3:
        return state
    result = dict(state)
    for key in (
        "schema_version",
        "session_generation",
        "turn_ledger_version",
        "revision",
        "cold_turn_count",
        "next_admission_order",
        "ledger_latest",
    ):
        if key in previous:
            result[key] = previous[key]
    return result


def _read_existing_state_for_save_sync(
    session_save_path: str,
) -> dict[str, Any]:
    """Never replace unreadable committed state with an empty snapshot."""
    _, state = _read_json_state_sync(
        session_save_path,
        allow_not_exist=True,
        allow_empty=True,
    )
    return state


def _write_json_state_sync(
    session_save_path: str,
    state: dict[str, Any],
) -> None:
    atomic_json(Path(session_save_path), state)


@measure_storage_operation("session_commit")
def _commit_ledger_sync(session_save_path, state, baseline, revision):
    ledger = TurnLedger(session_save_path)
    _, durable = _read_json_state_sync(session_save_path, allow_not_exist=True)
    ledger.recover(durable)
    if durable.get("revision", 0) != state.get("revision", 0):
        raise ValueError(
            "Session revision changed; reload the durable snapshot",
        )
    state = _preserve_ledger_metadata(state, durable)
    deleted = tuple(state.pop("_deleted_turn_ids", ()))
    committed = ledger.prepare(state, baseline, revision, deleted=deleted)
    try:
        _write_json_state_sync(session_save_path, committed)
    except OSError as exc:
        _, actual = _read_json_state_sync(
            session_save_path,
            allow_not_exist=True,
        )
        if actual.get("ledger_nonce") == committed["ledger_nonce"]:
            observe_hot_turns(committed, baseline)
            raise PublishedLedgerCommitError(committed) from exc
        raise
    observe_hot_turns(committed, baseline)
    try:
        ledger.finish(committed)
    except OSError:
        logger.warning(
            "Ledger commit published; cleanup will retry on next write",
        )
    return committed


class PublishedLedgerCommitError(OSError):
    """A root was published but durability confirmation did not finish."""

    def __init__(self, state):
        super().__init__(
            "Session root published; durability confirmation failed",
        )
        self.state = state


async def _await_durable_worker(operation):
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise


async def _durable_state_write(session_path, state, previous):
    """Keep the session lock until every worker write has actually finished."""
    if previous.get("schema_version") == 3:
        state = _preserve_ledger_metadata(state, previous)
        operation = run_runtime_state_work(
            _commit_ledger_sync,
            session_path,
            state,
            terminal_fingerprints(previous),
            previous["revision"] + 1,
        )
    else:
        operation = run_runtime_state_work(
            _write_json_state_sync,
            session_path,
            state,
        )
    task = asyncio.create_task(operation)
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise
    return result if isinstance(result, dict) else state


class SessionExecution:
    """A session state snapshot protected by one file lock for its lifetime."""

    def __init__(
        self,
        session_save_path: str,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        self._session_save_path = session_save_path
        self._timeout_seconds = timeout_seconds
        self._scope = None
        self._state: dict[str, Any] = {}
        self._revision = 0
        self._schema_version = 1
        self._state_dirty = False
        self._entered = False
        self._terminal_baseline: dict[str, str] = {}
        self._active_executions_token: (
            Token[Mapping[str, "SessionExecution"]] | None
        ) = None

    @property
    def revision(self) -> int:
        """Return the revision of the latest state in this transaction."""
        return self._revision

    @property
    def schema_version(self) -> int:
        """Return the schema version of the loaded state."""
        return self._schema_version

    @property
    def state(self) -> dict[str, Any]:
        """Return the transaction-owned mutable state snapshot."""
        self._require_entered()
        return self._state

    async def __aenter__(self) -> "SessionExecution":
        if self._entered or self._scope is not None:
            raise RuntimeError("session execution is already active")
        self._scope = _session_write_scope(
            self._session_save_path,
            timeout_seconds=self._timeout_seconds,
        )
        try:
            await self._scope.__aenter__()
            _, self._state = await run_runtime_state_work(
                _read_json_state_sync,
                self._session_save_path,
                allow_not_exist=True,
                allow_empty=True,
            )
            self._schema_version = self._get_schema_version(self._state)
            self._revision = self._get_revision(self._state)
            if self._schema_version == 3:
                await _await_durable_worker(
                    run_runtime_state_work(
                        TurnLedger(self._session_save_path).recover,
                        self._state,
                    ),
                )
                self._terminal_baseline = terminal_fingerprints(self._state)
            active_executions = dict(_ACTIVE_SESSION_EXECUTIONS.get())
            active_executions[
                _get_normalized_session_path(self._session_save_path)
            ] = self
            self._active_executions_token = _ACTIVE_SESSION_EXECUTIONS.set(
                active_executions,
            )
            self._entered = True
            return self
        except BaseException:
            self._reset_active_executions()
            self._state = {}
            self._terminal_baseline = {}
            if self._scope is not None:
                await self._scope.__aexit__(None, None, None)
                self._scope = None
            raise

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._scope is not None:
                await self._scope.__aexit__(exc_type, exc, traceback)
        finally:
            self._entered = False
            self._scope = None
            self._reset_active_executions()
            self._state = {}
            self._terminal_baseline = {}

    async def close(self) -> None:
        """Release this transaction before unrelated query cleanup begins."""
        if self._scope is not None:
            await self.__aexit__(None, None, None)

    @property
    def is_active(self) -> bool:
        """Return whether this transaction still owns its session lock."""
        return self._entered

    def _reset_active_executions(self) -> None:
        if self._active_executions_token is not None:
            _ACTIVE_SESSION_EXECUTIONS.reset(self._active_executions_token)
            self._active_executions_token = None

    async def read_state(self) -> dict[str, Any]:
        """Return the single state snapshot read while acquiring the lock."""
        return self.state

    async def hydrate_turn(self, turn_id: str) -> dict | None:
        """Restore exactly one cold identity into this request's mutable state."""
        self._require_entered()
        turn = await run_runtime_state_work(
            TurnLedger(self._session_save_path).get_turn,
            self._state,
            turn_id,
        )
        if turn is not None:
            self._state.setdefault("turn_states", {})[turn_id] = turn
        return turn

    @property
    def has_uncommitted_state(self) -> bool:
        """Return whether a transaction-side mutation still needs a commit."""
        return self._state_dirty

    def mark_state_dirty(self) -> None:
        """Mark a direct transaction-state mutation for final persistence."""
        self._require_entered()
        self._state_dirty = True

    async def commit_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Replace transaction state without reacquiring its held lock."""
        self._require_entered()
        if not isinstance(state, dict):
            raise ValueError("state must be a dict")

        next_revision = self._revision + 1
        if self._schema_version != 3 and not os.path.exists(
            self._session_save_path,
        ):
            from swe.config.context import get_current_tenant_id

            enabled = {
                value.strip()
                for value in os.environ.get(
                    "SWE_SESSION_V3_TENANTS",
                    "",
                ).split(",")
                if value.strip()
            }
            if get_current_tenant_id() in enabled:
                state = await run_runtime_state_work(
                    TurnLedger(self._session_save_path).initialize,
                    state,
                )
                self._schema_version = 3
        if self._schema_version == 3:
            state = _preserve_ledger_metadata(state, self._state)
            committed_state = await self._commit_ledger(state, next_revision)
            self._set_committed_state(committed_state, next_revision)
            return self._state
        committed_state = {
            **state,
            "schema_version": 2,
            "revision": next_revision,
        }
        write_task = asyncio.create_task(
            run_runtime_state_work(
                _write_json_state_sync,
                self._session_save_path,
                committed_state,
            ),
        )
        try:
            await asyncio.shield(write_task)
        except asyncio.CancelledError:
            while not write_task.done():
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    continue
            write_task.result()
            self._set_committed_state(committed_state, next_revision)
            raise
        self._set_committed_state(committed_state, next_revision)
        return self._state

    async def _commit_ledger(self, state, revision):
        task = asyncio.create_task(
            run_runtime_state_work(
                _commit_ledger_sync,
                self._session_save_path,
                state,
                self._terminal_baseline,
                revision,
            ),
        )
        try:
            return await asyncio.shield(task)
        except PublishedLedgerCommitError as exc:
            self._set_committed_state(exc.state, revision)
            raise
        except asyncio.CancelledError:
            try:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                result = task.result()
            except PublishedLedgerCommitError as exc:
                self._set_committed_state(exc.state, revision)
                raise
            self._set_committed_state(result, revision)
            raise

    def _set_committed_state(
        self,
        committed_state: dict[str, Any],
        revision: int,
    ) -> None:
        self._state = committed_state
        self._schema_version = committed_state.get("schema_version", 2)
        self._revision = revision
        if self._schema_version == 3:
            self._terminal_baseline = terminal_fingerprints(committed_state)
        self._state_dirty = False

    def _require_entered(self) -> None:
        if not self._entered:
            raise RuntimeError("session execution is not active")

    @staticmethod
    def _get_schema_version(state: dict[str, Any]) -> int:
        schema_version = state.get("schema_version", 1)
        return schema_version if isinstance(schema_version, int) else 1

    @staticmethod
    def _get_revision(state: dict[str, Any]) -> int:
        revision = state.get("revision", 0)
        return revision if isinstance(revision, int) and revision >= 0 else 0


class SafeJSONSession(SessionBase):
    """SessionBase subclass with filename sanitization and async file I/O.

    Uses :mod:`aiofiles` for reads and worker threads for writes so that
    disk I/O does not block the event loop.
    """

    def __init__(
        self,
        save_dir: str = "./",
    ) -> None:
        """Initialize the JSON session class.

        Args:
            save_dir (`str`, defaults to `"./"):
                The directory to save the session state.
        """
        self.save_dir = save_dir

    def _get_save_path(self, session_id: str, user_id: str) -> str:
        """Return a filesystem-safe save path.

        Overrides the parent implementation to ensure the generated
        filename is valid on Windows, macOS and Linux.
        """
        os.makedirs(self.save_dir, exist_ok=True)
        safe_sid = sanitize_filename(session_id)
        safe_uid = sanitize_filename(user_id) if user_id else ""
        if safe_uid:
            file_path = f"{safe_uid}_{safe_sid}.json"
        else:
            file_path = f"{safe_sid}.json"
        return os.path.join(self.save_dir, file_path)

    async def get_turn_state(self, session_id, user_id, state, turn_id):
        return await run_runtime_state_work(
            TurnLedger(self._get_save_path(session_id, user_id)).get_turn,
            state,
            turn_id,
        )

    async def materialize_turn_states(
        self,
        session_id,
        user_id,
        state,
        chat_id=None,
    ):
        return await run_runtime_state_work(
            TurnLedger(self._get_save_path(session_id, user_id)).materialize,
            state,
            chat_id,
        )

    async def get_latest_turn_state(self, session_id, user_id, state, chat_id):
        return await run_runtime_state_work(
            TurnLedger(self._get_save_path(session_id, user_id)).latest,
            state,
            chat_id,
        )

    @measure_storage_operation("session_migration", dry_run_default=True)
    async def migrate_terminal_turns(
        self,
        session_id,
        user_id="",
        *,
        dry_run=True,
    ):
        if dry_run:
            path = self._get_save_path(session_id, user_id)
            async with self.session_write_lock(session_id, user_id):
                _, state = await run_runtime_state_work(
                    _read_json_state_sync,
                    path,
                    allow_not_exist=False,
                )
                ledger = TurnLedger(path)
                pending = False
                total = len(state.get("turn_states", {}))
                if state.get("schema_version") == 3:
                    pending = (
                        ledger.directory(state) / "pending.json"
                    ).exists()
                    total += await run_runtime_state_work(
                        lambda: sum(1 for _ in ledger.iter_cold(state)),
                    )
                return {
                    "total": total,
                    "format": state.get("schema_version", 1),
                    "pending": pending,
                }
        async with self.execution(session_id, user_id) as tx:
            report = {
                "total": len(tx.state.get("turn_states", {})),
                "format": tx.schema_version,
            }
            if tx.schema_version != 3:
                state = await _await_durable_worker(
                    run_runtime_state_work(
                        TurnLedger(tx._session_save_path).initialize,
                        tx.state,
                    ),
                )
                tx._state = state
                tx._schema_version = 3
                tx._terminal_baseline = terminal_fingerprints(state)
            # Every commit is bounded; reruns continue an interrupted migration.
            while True:
                before = len(tx.state.get("turn_states", {}))
                await tx.commit_state(tx.state)
                if len(tx.state.get("turn_states", {})) == before:
                    break
            return report

    async def export_legacy_state(self, session_id, user_id=""):
        async with self.execution(session_id, user_id) as tx:
            state = await self.materialize_turn_states(
                session_id,
                user_id,
                tx.state,
            )
            for turn in state.get("turn_states", {}).values():
                turn.pop("admission_order", None)
            for key in (
                "session_generation",
                "turn_ledger_version",
                "ledger_nonce",
                "next_admission_order",
                "cold_turn_count",
                "ledger_latest",
            ):
                state.pop(key, None)
            state["schema_version"] = 2
            return state

    async def delete_turn_state(self, session_id, turn_id, user_id=""):
        """Publish a deletion version; older root snapshots remain readable."""
        async with self.execution(session_id, user_id) as tx:
            if tx.schema_version == 3:
                tx.state["_deleted_turn_ids"] = [turn_id]
            else:
                tx.state.get("turn_states", {}).pop(turn_id, None)
            await tx.commit_state(tx.state)

    @asynccontextmanager
    async def session_write_lock(
        self,
        session_id: str,
        user_id: str = "",
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[None]:
        """按会话文件串行化读改写，避免清理和运行结束互相覆盖。"""
        session_save_path = self._get_save_path(session_id, user_id=user_id)
        async with _session_write_scope(
            session_save_path,
            timeout_seconds=timeout_seconds,
        ):
            yield

    @asynccontextmanager
    async def execution(
        self,
        session_id: str,
        user_id: str = "",
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[SessionExecution]:
        """Hold one session lock while reading and committing a snapshot."""
        session_save_path = self._get_save_path(session_id, user_id=user_id)
        transaction = SessionExecution(
            session_save_path,
            timeout_seconds=timeout_seconds,
        )
        async with transaction:
            yield transaction

    async def _read_session_state_file(
        self,
        session_save_path: str,
        *,
        allow_not_exist: bool,
    ) -> tuple[bool, dict[str, Any]]:
        _reject_short_lock_during_execution(session_save_path)
        async with _get_session_write_lock(session_save_path):
            return await run_runtime_state_work(
                _read_json_state_sync,
                session_save_path,
                allow_not_exist=allow_not_exist,
            )

    async def _read_existing_state_for_save(
        self,
        session_save_path: str,
    ) -> dict[str, Any]:
        """Read committed state without silently replacing unreadable roots."""
        return await run_runtime_state_work(
            _read_existing_state_for_save_sync,
            session_save_path,
        )

    async def save_session_state(
        self,
        session_id: str,
        user_id: str = "",
        **state_modules_mapping,
    ) -> None:
        """Save state modules to a JSON file using async I/O."""
        session_save_path = self._get_save_path(session_id, user_id=user_id)
        async with _session_write_scope(session_save_path):
            existing_state = await self._read_existing_state_for_save(
                session_save_path,
            )
            state_dicts = {
                name: state_module.state_dict()
                for name, state_module in state_modules_mapping.items()
            }
            state_dicts = {**existing_state, **state_dicts}
            await _durable_state_write(
                session_save_path,
                state_dicts,
                existing_state,
            )

        logger.info(
            "Saved session state to %s successfully.",
            session_save_path,
        )

    async def load_session_state(
        self,
        session_id: str,
        user_id: str = "",
        allow_not_exist: bool = True,
        **state_modules_mapping,
    ) -> None:
        """Load state modules from a JSON file using async I/O."""
        session_save_path = self._get_save_path(session_id, user_id=user_id)
        exists, states = await self._read_session_state_file(
            session_save_path,
            allow_not_exist=allow_not_exist,
        )
        if exists:
            for name, state_module in state_modules_mapping.items():
                if name in states:
                    normalized_state = _normalize_state_for_load(
                        states[name],
                    )
                    state_module.load_state_dict(normalized_state)
            logger.info(
                "Load session state from %s successfully.",
                session_save_path,
            )
        else:
            logger.info(
                "Session file %s does not exist. Skip loading session state.",
                session_save_path,
            )

    async def update_session_state(
        self,
        session_id: str,
        key: Union[str, Sequence[str]],
        value,
        user_id: str = "",
        create_if_not_exist: bool = True,
    ) -> None:
        path = key.split(".") if isinstance(key, str) else list(key)
        if not path:
            raise ValueError("key path is empty")

        def _mutate(states: dict[str, Any]) -> dict[str, Any]:
            cur = states
            for k in path[:-1]:
                if k not in cur or not isinstance(cur[k], dict):
                    cur[k] = {}
                cur = cur[k]

            cur[path[-1]] = value
            return states

        await self.mutate_session_state(
            session_id=session_id,
            mutator=_mutate,
            user_id=user_id,
            create_if_not_exist=create_if_not_exist,
        )

        logger.info(
            "Updated session state key '%s' in %s successfully.",
            key,
            self._get_save_path(session_id, user_id=user_id),
        )

    async def get_session_state_dict(
        self,
        session_id: str,
        user_id: str = "",
        allow_not_exist: bool = True,
    ) -> dict:
        """Return the session state dict from the JSON file.

        Args:
            session_id (`str`):
                The session id.
            user_id (`str`, default to `""`):
                The user ID for the storage.
            allow_not_exist (`bool`, defaults to `True`):
                Whether to allow the session to not exist. If `False`, raises
                an error if the session does not exist.

        Returns:
            `dict`:
                The session state dict loaded from the JSON file. Returns an
                empty dict if the file does not exist and
                `allow_not_exist=True`.
        """
        session_save_path = self._get_save_path(session_id, user_id=user_id)
        exists, states = await self._read_session_state_file(
            session_save_path,
            allow_not_exist=allow_not_exist,
        )
        if exists:
            logger.info(
                "Get session state dict from %s successfully.",
                session_save_path,
            )
            return states
        logger.info(
            "Session file %s does not exist. Return empty state dict.",
            session_save_path,
        )
        return {}

    async def get_persisted_session_state_dict(
        self,
        session_id: str,
        user_id: str = "",
        allow_not_exist: bool = True,
    ) -> dict:
        """Read the latest atomically committed session snapshot without waiting.

        Session writers replace the JSON file atomically.  A history observer can
        therefore safely consume the last committed file while an execution owns
        the transaction lock, rather than waiting for model generation to end.
        """
        session_save_path = self._get_save_path(session_id, user_id=user_id)
        exists, states = await run_runtime_state_work(
            _read_json_state_sync,
            session_save_path,
            allow_not_exist=allow_not_exist,
        )
        return states if exists else {}

    async def get_session_skill_snapshot(
        self,
        session_id: str,
        user_id: str = "",
        allow_not_exist: bool = True,
    ) -> dict[str, dict]:
        """Return the persisted session skill snapshot."""
        state = await self.get_session_state_dict(
            session_id=session_id,
            user_id=user_id,
            allow_not_exist=allow_not_exist,
        )
        raw_snapshot = (
            state.get(SESSION_SKILL_SNAPSHOT_STATE_KEY)
            if isinstance(state, dict)
            else None
        )
        if not isinstance(raw_snapshot, dict):
            return {}

        snapshot: dict[str, dict] = {}
        for skill_name, entry in raw_snapshot.items():
            if isinstance(skill_name, str) and isinstance(entry, dict):
                snapshot[skill_name] = dict(entry)
        return snapshot

    async def save_session_skill_snapshot(
        self,
        session_id: str,
        snapshot: dict[str, dict],
        user_id: str = "",
    ) -> None:
        """Persist the top-level session skill snapshot."""
        await self.update_session_state(
            session_id=session_id,
            key=SESSION_SKILL_SNAPSHOT_STATE_KEY,
            value=snapshot,
            user_id=user_id,
        )

    async def save_merged_state(
        self,
        session_id: str,
        user_id: str = "",
        state: dict = None,
    ) -> None:
        """Save merged state dict to JSON file.

        Used by cron tasks to preserve existing session history while
        saving new state without overwriting.

        Args:
            session_id: The session id
            user_id: The user ID for the storage
            state: The merged state dict to save
        """
        if state is None:
            state = {}
        session_save_path = self._get_save_path(session_id, user_id=user_id)
        async with _session_write_scope(session_save_path):
            previous = await self._read_existing_state_for_save(
                session_save_path,
            )
            await _durable_state_write(session_save_path, state, previous)
        logger.info(
            "Saved merged session state to %s (keys=%d)",
            session_save_path,
            len(state),
        )

    async def mutate_session_state(
        self,
        session_id: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
        user_id: str = "",
        create_if_not_exist: bool = True,
        timeout_seconds: float | None = None,
        hydrate_turn_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Atomically read, merge and write session state under one lock."""
        session_save_path = self._get_save_path(session_id, user_id=user_id)
        async with _session_write_scope(
            session_save_path,
            timeout_seconds=timeout_seconds,
        ):
            exists, states = await run_runtime_state_work(
                _read_json_state_sync,
                session_save_path,
                allow_not_exist=True,
                allow_empty=True,
            )
            if not exists and not create_if_not_exist:
                raise ValueError(
                    f"Session file {session_save_path} does not exist.",
                )

            if states.get("schema_version") == 3:
                await _await_durable_worker(
                    run_runtime_state_work(
                        TurnLedger(session_save_path).recover,
                        states,
                    ),
                )
            baseline = terminal_fingerprints(states)
            for turn_id in hydrate_turn_ids:
                turn = await run_runtime_state_work(
                    TurnLedger(session_save_path).get_turn,
                    states,
                    turn_id,
                )
                if turn is not None:
                    states.setdefault("turn_states", {})[turn_id] = turn

            previous = dict(states)
            updated_state = mutator(states)
            if updated_state is None:
                updated_state = states
            if not isinstance(updated_state, dict):
                raise ValueError("mutator must return a dict or None")

            if previous.get("schema_version") == 3:
                updated_state = _preserve_ledger_metadata(
                    updated_state,
                    previous,
                )
                # Mutators may edit in place; preserve the pre-mutation baseline.
                # The durable worker uses this already captured baseline.
                task = asyncio.create_task(
                    run_runtime_state_work(
                        _commit_ledger_sync,
                        session_save_path,
                        updated_state,
                        baseline,
                        previous["revision"] + 1,
                    ),
                )
                try:
                    updated_state = await asyncio.shield(task)
                except asyncio.CancelledError:
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                    task.result()
                    raise
            else:
                await _durable_state_write(
                    session_save_path,
                    updated_state,
                    states,
                )

        logger.info(
            "Mutated session state in %s successfully.",
            session_save_path,
        )
        assert isinstance(updated_state, dict)
        return updated_state
