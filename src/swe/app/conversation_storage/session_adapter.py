# -*- coding: utf-8 -*-
"""Complete recovery roots with SQL CAS and explicit execution authority."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import asyncio
import json
import time

from .bundle import BundleBlock, new_operation_key
from .chat_mutations import load_metadata_json
from .errors import ConflictError, IntegrityError, StorageError, UnknownCommit
from .execution import IDENTITY_WHERE, ExecutionAuthority
from .identity import SessionIdentity
from .publications import CommitPlan, PublicationRequest, PublicationStore
from .retention import RetentionStore
from .projection_references import hydrate_projection

_AUTHORITIES: ContextVar[dict | None] = ContextVar(
    "mysql_session_authorities",
    default=None,
)


@dataclass
class SemanticBatch:
    """Injected bounds; callers publish immediately at semantic boundaries."""

    max_bytes: int
    max_events: int
    max_seconds: float
    clock: object = time.monotonic

    def __post_init__(self):
        if min(self.max_bytes, self.max_events, self.max_seconds) <= 0:
            raise ValueError("batch limits must be positive")
        self.reset()

    def reset(self):
        self.byte_count = 0
        self.event_count = 0
        self.started_at = self.clock()

    def add(self, text, *, semantic=False):
        self.byte_count += len(text.encode("utf-8"))
        self.event_count += 1
        return (
            semantic
            or self.byte_count >= self.max_bytes
            or self.event_count >= self.max_events
            or self.clock() - self.started_at >= self.max_seconds
        )


class DatabaseSessionExecution:
    """Request-local snapshot with publication CAS; no long SQL transaction."""

    def __init__(self, adapter, identity):
        self.adapter = adapter
        self.identity = identity
        self._state = {}
        self._active = False
        self._dirty = False
        self.pending_operation = None
        self.pending_request = None
        self.defer_close = False
        self._projection_cache = None
        self.mutation_lock = asyncio.Lock()

    @property
    def is_active(self):
        return self._active

    @property
    def state(self):
        if not self._active:
            raise RuntimeError("Session execution is not active")
        return self._state

    @property
    def revision(self):
        return self.state.get("revision", 0)

    @property
    def schema_version(self):
        return self.state.get("schema_version", 2)

    @property
    def has_uncommitted_state(self):
        return self._dirty

    def mark_state_dirty(self):
        _ = self.state
        self._dirty = True

    async def __aenter__(self):
        if self._active:
            raise RuntimeError("Session execution is already active")
        from .projection_cache import cache_scope

        self._projection_cache = cache_scope()
        self._projection_cache.__enter__()
        try:
            self._state = await self.adapter.get_session_state_dict(
                self.identity.session_id,
                self.identity.user_id,
            )
        except BaseException:
            self._projection_cache.__exit__(None, None, None)
            self._projection_cache = None
            raise
        self._active = True
        return self

    async def __aexit__(self, *args):
        self._active = False
        self._state = {}
        if self._projection_cache is not None:
            self._projection_cache.__exit__(None, None, None)
            self._projection_cache = None

    async def close(self):
        if self.defer_close:
            return
        await self.__aexit__(None, None, None)

    async def read_state(self):
        return self.state

    async def hydrate_turn(self, turn_id):
        turn = await self.adapter.get_turn_state(
            self.identity.session_id,
            self.identity.user_id,
            self.state,
            turn_id,
        )
        if turn is not None:
            self.state.setdefault("turn_states", {})[turn_id] = turn
        return turn

    async def commit_state(self, state):
        _ = self.state
        if self.pending_operation is not None:
            raise UnknownCommit(self.pending_operation)
        try:
            self._state = await self.adapter._save_state(self.identity, state)
        except UnknownCommit as error:
            self.pending_operation = error.operation_key
            self.pending_request = error.publication_request
            raise
        self._dirty = False
        return self._state

    def accept_committed_state(self, state):
        """Accept an independently confirmed atomic archive/reset publication."""
        _ = self.state
        if not isinstance(state, dict):
            raise ValueError("committed recovery root must be an object")
        revision = state.get("revision")
        if type(revision) is not int or revision < self.revision:
            raise ValueError("committed recovery revision cannot regress")
        self._state = deepcopy(state)
        self._dirty = False


class MySQLSessionAdapter:
    """One trusted channel/workspace; no file reader or fallback is reachable."""

    def __init__(self, database, scope, bundles, *, channel):
        self.db = database
        self.scope = scope
        self.channel = channel
        self.bundles = bundles
        self.publications = PublicationStore(database, bundles)
        self.retentions = RetentionStore(database, scope)
        self.owner_key = new_operation_key()

    def identity(self, session_id, user_id=""):
        return SessionIdentity(self.scope, user_id, self.channel, session_id)

    @asynccontextmanager
    async def execution(self, session_id, user_id="", timeout_seconds=None):
        async with DatabaseSessionExecution(
            self,
            self.identity(session_id, user_id),
        ) as execution:
            yield execution

    @asynccontextmanager
    async def session_write_lock(
        self,
        session_id,
        user_id="",
        timeout_seconds=None,
    ):
        self._reject_raw_lock()
        yield

    @staticmethod
    def _reject_raw_lock():
        raise ConflictError(
            "SQL Session writes require snapshot CAS or execution authority",
        )

    @asynccontextmanager
    async def bind_authority(self, authority: ExecutionAuthority):
        if (
            authority.identity.scope != self.scope
            or authority.identity.channel != self.channel
        ):
            raise IntegrityError("session adapter authority scope mismatch")
        current = dict(_AUTHORITIES.get() or {})
        current[authority.identity] = authority
        token = _AUTHORITIES.set(current)
        try:
            yield
        finally:
            _AUTHORITIES.reset(token)

    async def _row(self, identity, *, lock=False):
        return await self.db.fetch_one(
            f"SELECT * FROM swe_conversation_sessions WHERE {IDENTITY_WHERE}"
            + (" FOR UPDATE" if lock else ""),
            identity.sql_values,
        )

    @staticmethod
    def _recovery_identity(row):
        return tuple(
            row[key]
            for key in (
                "id",
                "state_revision",
                "recovery_publication_id",
                "recovery_offset",
                "recovery_length",
            )
        )

    @staticmethod
    def _publication_session(publication, session):
        if publication["session_id"] != session["id"]:
            raise IntegrityError("recovery publication Session mismatch")

    async def get_session_state_dict(
        self,
        session_id,
        user_id="",
        allow_not_exist=True,
    ):
        identity = self.identity(session_id, user_id)
        batch = None
        async with self.db.transaction():
            row = await self._row(identity)
            if row is None:
                if allow_not_exist:
                    return {}
                raise ValueError("Session does not exist")
            publication_id = row["recovery_publication_id"]
            if publication_id is not None:
                batch = await self.retentions.pin(
                    [publication_id],
                    kind="reader",
                )
        try:
            state = {}
            if batch is not None:
                publication = batch.publications[0]
                self._publication_session(publication, row)
                payload = await batch.read(
                    self.bundles.read_extent,
                    publication["relative_path"].decode("utf-8"),
                    row["recovery_offset"],
                    row["recovery_length"],
                    expected_length=publication["byte_length"],
                )
                try:
                    state = load_metadata_json(payload)
                    if not isinstance(state, dict):
                        raise ValueError("recovery root must be an object")
                    state = await hydrate_projection(
                        self,
                        state,
                        row,
                        publication,
                        batch,
                    )
                except (ValueError, TypeError) as error:
                    raise IntegrityError(
                        "Session recovery root is invalid",
                    ) from error
            current = await self._row(identity)
            if current is None or self._recovery_identity(
                current,
            ) != self._recovery_identity(row):
                raise ConflictError("Session recovery changed during read")
            return {**state, "revision": row["state_revision"]}
        finally:
            if batch is not None:
                await self.retentions.finish_reader(batch)

    async def get_persisted_session_state_dict(
        self,
        session_id,
        user_id="",
        allow_not_exist=True,
    ):
        return await self.get_session_state_dict(
            session_id,
            user_id,
            allow_not_exist,
        )

    async def _ensure_session(self, identity):
        await self.db.insert(
            "INSERT INTO swe_conversation_sessions (tenant_key,source_key,agent_key,user_key,channel_key,external_session_key,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)",
            identity.sql_values,
        )
        return await self._row(identity, lock=True)

    async def _save_state(self, identity, state):
        if not isinstance(state, dict):
            raise ValueError("state must be a dict")
        revision = state.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise ValueError("state revision must be nonnegative")
        authority = (_AUTHORITIES.get() or {}).get(identity)
        operation = new_operation_key()
        committed_state = {**state, "revision": revision + 1}
        payload = json.dumps(
            committed_state,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = None
        try:
            async with self.db.transaction():
                await self.retentions.assert_writes_allowed()
                row = await self._ensure_session(identity)
                if row["state_revision"] != revision:
                    raise ConflictError("Session state revision changed")
                if row["active_turn_id"] is not None and authority is None:
                    raise ConflictError(
                        "Session is busy; execution authority is required",
                    )
                request = PublicationRequest(
                    operation,
                    identity.scope,
                    "state",
                    authority.owner_key if authority else self.owner_key,
                    authority.generation if authority else row["generation"],
                    session_id=row["id"],
                    chat_id=authority.chat_id if authority else None,
                    turn_id=authority.turn_id if authority else None,
                    base_state_revision=revision,
                )
                await self.publications.reserve(
                    request,
                    CommitPlan(recovery_block="recovery"),
                )
            await self.publications.write_bundle(
                operation,
                [BundleBlock("recovery", "recovery", payload)],
            )
            await self.publications.commit(operation)
            return committed_state
        except StorageError as error:
            if (
                isinstance(error, UnknownCommit)
                and error.publication_request is None
            ):
                error.publication_request = request
            raise
        except (ValueError, TypeError, KeyError):
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(operation, request) from error

    async def save_session_state(
        self,
        session_id,
        user_id="",
        **state_modules_mapping,
    ):
        state = await self.get_session_state_dict(session_id, user_id)
        state.update(
            {
                name: module.state_dict()
                for name, module in state_modules_mapping.items()
            },
        )
        return await self._save_state(
            self.identity(session_id, user_id),
            state,
        )

    async def load_session_state(
        self,
        session_id,
        user_id="",
        allow_not_exist=True,
        **state_modules_mapping,
    ):
        from ..runner.session import _normalize_state_for_load

        state = await self.get_session_state_dict(
            session_id,
            user_id,
            allow_not_exist,
        )
        for name, module in state_modules_mapping.items():
            if name in state:
                module.load_state_dict(_normalize_state_for_load(state[name]))

    async def save_merged_state(self, session_id, user_id="", state=None):
        previous = await self.get_session_state_dict(session_id, user_id)
        return await self._save_state(
            self.identity(session_id, user_id),
            {**(state or {}), "revision": previous.get("revision", 0)},
        )

    async def mutate_session_state(
        self,
        session_id,
        mutator,
        user_id="",
        create_if_not_exist=True,
        timeout_seconds=None,
        hydrate_turn_ids=(),
    ):
        state = await self.get_session_state_dict(
            session_id,
            user_id,
            create_if_not_exist,
        )
        for turn_id in hydrate_turn_ids:
            turn = await self.get_turn_state(
                session_id,
                user_id,
                state,
                turn_id,
            )
            if turn is not None:
                state.setdefault("turn_states", {})[turn_id] = turn
        updated = mutator(state)
        if updated is None:
            updated = state
        if not isinstance(updated, dict):
            raise ValueError("mutator must return dict or None")
        return await self._save_state(
            self.identity(session_id, user_id),
            updated,
        )

    async def update_session_state(
        self,
        session_id,
        key,
        value,
        user_id="",
        create_if_not_exist=True,
    ):
        path = key.split(".") if isinstance(key, str) else list(key)
        if not path:
            raise ValueError("key path is empty")

        def mutate(state):
            current = state
            for part in path[:-1]:
                if not isinstance(current.get(part), dict):
                    current[part] = {}
                current = current[part]
            current[path[-1]] = value

        return await self.mutate_session_state(
            session_id,
            mutate,
            user_id,
            create_if_not_exist,
        )

    async def get_session_skill_snapshot(
        self,
        session_id,
        user_id="",
        allow_not_exist=True,
    ):
        state = await self.get_session_state_dict(
            session_id,
            user_id,
            allow_not_exist,
        )
        value = state.get("session_skill_snapshot", {})
        return (
            {
                name: dict(item)
                for name, item in value.items()
                if isinstance(name, str) and isinstance(item, dict)
            }
            if isinstance(value, dict)
            else {}
        )

    async def save_session_skill_snapshot(
        self,
        session_id,
        snapshot,
        user_id="",
    ):
        return await self.update_session_state(
            session_id,
            "session_skill_snapshot",
            snapshot,
            user_id,
        )

    async def get_turn_state(self, session_id, user_id, state, turn_id):
        identity = self.identity(session_id, user_id)
        batches = []
        async with self.db.transaction():
            session = await self._row(identity)
            if session is None:
                return None
            turn = await self.db.fetch_one(
                "SELECT * FROM swe_conversation_turns WHERE session_id=%s AND (question_msg_key=%s OR runtime_turn_key=%s) LIMIT 1",
                (
                    session["id"],
                    turn_id.encode("utf-8"),
                    turn_id.encode("utf-8"),
                ),
            )
            if turn is None or turn["state"] == "rejected":
                return None
            chat = await self._turn_chat(turn, session)
            publication_id = (
                turn["terminal_publication_id"]
                or turn["admission_publication_id"]
            )
            if publication_id is None:
                return None
            batch = await self.retentions.pin([publication_id], kind="reader")
            batches.append(batch)
        try:
            publication = batch.publications[0]
            self._publication_session(publication, session)
            if (
                publication["chat_id"] != turn["chat_id"]
                or publication["turn_id"] != turn["id"]
            ):
                raise IntegrityError("Turn publication association mismatch")
            await self._revalidate_turn(identity, turn, chat)
            relative_path = publication["relative_path"].decode("utf-8")
            index = await batch.read(
                self.bundles.inspect,
                relative_path,
                expected_length=publication["byte_length"],
            )
            root = index.blocks.get("recovery")
            if root is None or root.kind != "recovery":
                raise IntegrityError("Turn publication has no recovery root")
            payload = await batch.read(
                self.bundles.read_extent,
                relative_path,
                root.offset,
                root.length,
                expected_length=publication["byte_length"],
            )
            persisted = load_metadata_json(payload)
            persisted = await hydrate_projection(
                self,
                persisted,
                session,
                publication,
                batch,
            )
            value = self._turn_value(json.dumps(persisted), turn, chat)
            await self._revalidate_turn(identity, turn, chat)
            return deepcopy(value)
        finally:
            for pinned in batches:
                await self.retentions.finish_reader(pinned)

    @staticmethod
    def _turn_value(payload, turn, chat):
        persisted = load_metadata_json(payload)
        if not isinstance(persisted, dict):
            raise IntegrityError("Turn recovery root is invalid")
        question = turn["question_msg_key"].decode("utf-8")
        runtime = (
            turn["runtime_turn_key"].decode("utf-8")
            if turn["runtime_turn_key"]
            else None
        )
        states = persisted.get("turn_states", {})
        if not isinstance(states, dict):
            raise IntegrityError("Turn recovery states must be an object")
        value = states.get(question) or states.get(runtime)
        if not isinstance(value, dict):
            raise IntegrityError(
                "Turn recovery root is missing its complete state",
            )
        if value.get("chat_id") is not None and value["chat_id"] != chat[
            "external_chat_key"
        ].decode("utf-8"):
            raise IntegrityError("Turn recovery Chat identity mismatch")
        if (
            turn["history_status"] is not None
            and value.get("status") != turn["history_status"]
        ):
            raise IntegrityError("Turn recovery status mismatch")
        return value

    async def _turn_chat(self, turn, session):
        chat = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_chats WHERE id=%s",
            (turn["chat_id"],),
        )
        if chat is None or chat["deleted_at"] is not None:
            raise ConflictError("Turn Chat was deleted")
        if (
            chat["session_id"] != session["id"]
            or turn["session_id"] != session["id"]
        ):
            raise IntegrityError("Turn Chat Session identity mismatch")
        if (
            tuple(
                chat[key] for key in ("tenant_key", "source_key", "agent_key")
            )
            != self.scope.sql_values
        ):
            raise IntegrityError("Turn Chat scope mismatch")
        return chat

    async def _revalidate_turn(self, identity, turn, chat):
        session = await self._row(identity)
        current = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_turns WHERE id=%s",
            (turn["id"],),
        )
        fields = (
            "session_id",
            "chat_id",
            "question_msg_key",
            "runtime_turn_key",
            "admission_publication_id",
            "terminal_publication_id",
            "state",
            "history_status",
        )
        if (
            session is None
            or current is None
            or any(current[key] != turn[key] for key in fields)
        ):
            raise ConflictError("Turn identity or state changed during read")
        current_chat = await self._turn_chat(current, session)
        if (
            current_chat["id"] != chat["id"]
            or current_chat["external_chat_key"] != chat["external_chat_key"]
        ):
            raise ConflictError("Turn Chat identity changed during read")

    async def materialize_turn_states(
        self,
        session_id,
        user_id,
        state,
        chat_id=None,
    ):
        identity = self.identity(session_id, user_id)
        session = await self._row(identity)
        result = deepcopy(state)
        if session is None:
            return result
        params = [session["id"]]
        where = "session_id=%s AND admission_publication_id IS NOT NULL"
        if chat_id is not None:
            where += " AND chat_id=(SELECT id FROM swe_conversation_chats WHERE tenant_key=%s AND source_key=%s AND agent_key=%s AND external_chat_key=%s)"
            params.extend((*self.scope.sql_values, chat_id.encode("utf-8")))
        turns = await self.db.fetch_all(
            f"SELECT question_msg_key FROM swe_conversation_turns WHERE {where} ORDER BY id",
            tuple(params),
        )
        for turn in turns:
            key = turn["question_msg_key"].decode("utf-8")
            value = await self.get_turn_state(session_id, user_id, state, key)
            if value is not None:
                result.setdefault("turn_states", {})[key] = value
        return result

    async def get_latest_turn_state(self, session_id, user_id, state, chat_id):
        identity = self.identity(session_id, user_id)
        session = await self._row(identity)
        if session is None:
            return None
        latest = await self.db.fetch_one(
            "SELECT t.question_msg_key FROM swe_conversation_turns t JOIN swe_conversation_chats c ON c.id=t.chat_id WHERE t.session_id=%s AND c.external_chat_key=%s AND t.admission_publication_id IS NOT NULL ORDER BY t.turn_order DESC LIMIT 1",
            (session["id"], chat_id.encode("utf-8")),
        )
        if latest is None:
            return None
        key = latest["question_msg_key"].decode("utf-8")
        value = await self.get_turn_state(session_id, user_id, state, key)
        return (key, value) if value is not None else None

    async def delete_turn_state(self, session_id, turn_id, user_id=""):
        raise ConflictError(
            "Turn deletion must use a typed history publication",
        )
