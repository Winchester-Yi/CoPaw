# -*- coding: utf-8 -*-
"""Short Session authority transactions; model/tool IO never runs under SQL locks."""

from dataclasses import dataclass
import asyncio
import json
import secrets

from .bundle import BundleBlock, BundleStore
from .errors import ConflictError, IntegrityError, StorageError, UnknownCommit
from .identity import (
    SessionIdentity,
    chat_key_bytes,
    identity_bytes,
    question_msg_key_bytes,
    runtime_turn_key_bytes,
)
from .message_installs import MessageInstall, TurnInstall
from .publications import CommitPlan, PublicationRequest
from .retention import RetentionStore

IDENTITY_WHERE = (
    "tenant_key=%s AND source_key=%s AND agent_key=%s AND "
    "user_key=%s AND channel_key=%s AND external_session_key=%s"
)
LIVE_STATES = frozenset(
    {"reserved", "admitted", "running", "stopping", "settling"},
)
TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "stopped", "interrupted", "rejected"},
)


@dataclass(frozen=True)
class ExecutionAuthority:
    identity: SessionIdentity
    session_id: int
    chat_id: int
    turn_id: int
    generation: int
    owner_key: str

    def __post_init__(self):
        if not isinstance(self.identity, SessionIdentity):
            raise TypeError("execution requires a full Session identity")
        for value in (
            self.session_id,
            self.chat_id,
            self.turn_id,
            self.generation,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(
                    "execution identities must be positive integers",
                )
        identity_bytes(self.owner_key, 128)
        if not self.owner_key:
            raise ValueError("execution owner must be nonempty")


@dataclass(frozen=True)
class AdmissionResult:
    created: bool
    turn: dict
    publication: dict | None
    authority: ExecutionAuthority | None = None


def _json_bytes(value):
    if not isinstance(value, dict):
        raise ValueError("execution payload must be an object")
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ExecutionStore:
    """Database lease protects admission and publication, never resumes dead IO."""

    def __init__(
        self,
        database,
        *,
        owner_key=None,
        lease_seconds=30,
        publications=None,
    ):
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease seconds must be a positive integer")
        self.db = database
        self.owner_key = owner_key or secrets.token_hex(32)
        identity_bytes(self.owner_key, 128)
        self.lease_seconds = lease_seconds
        self.publications = publications

    async def _assert_writes_allowed(self, identity):
        await RetentionStore(
            self.db,
            identity.scope,
        ).assert_writes_allowed()

    async def reserve_admission(
        self,
        identity,
        chat_key,
        question_key,
        *,
        operation_key,
        runtime_turn_key,
        execution_identity_text=None,
        expected_state_revision=None,
    ):
        """Reserve once; retries never acquire authority to run an old question."""
        BundleStore.path_for(operation_key)
        question = question_msg_key_bytes(question_key)
        runtime = runtime_turn_key_bytes(runtime_turn_key)
        if not question or not runtime:
            raise ValueError(
                "question and runtime Turn identities must be nonempty",
            )
        if self.publications is None:
            raise RuntimeError("admission requires the publication store")
        try:
            async with self.db.transaction():
                await self._assert_writes_allowed(identity)
                session = await self._session(identity)
                chat = await self._chat_for_admission(
                    session,
                    identity,
                    chat_key,
                )
                turn = await self.db.fetch_one(
                    "SELECT * FROM swe_conversation_turns WHERE session_id=%s AND question_msg_key=%s FOR UPDATE",
                    (session["id"], question),
                )
                if turn is not None:
                    return await self._existing_admission(session, turn)
                if expected_state_revision is not None and (
                    type(expected_state_revision) is not int
                    or session["state_revision"] != expected_state_revision
                ):
                    raise ConflictError("Session changed before admission")
                if session["active_turn_id"] is not None:
                    raise ConflictError(
                        "Session is busy; active or settling Turn requires reconciliation",
                    )
                return await self._reserve_new_turn(
                    identity,
                    session,
                    chat,
                    question_key,
                    runtime,
                    operation_key,
                    execution_identity_text,
                )
        except (StorageError, ValueError, TypeError, KeyError):
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(operation_key) from error

    async def _chat_for_admission(self, session, identity, chat_key):
        chat = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_chats WHERE tenant_key=%s AND source_key=%s AND agent_key=%s AND external_chat_key=%s FOR UPDATE",
            (*identity.scope.sql_values, chat_key_bytes(chat_key)),
        )
        if chat is None or chat["deleted_at"] is not None:
            raise KeyError(chat_key)
        if chat["session_id"] != session["id"]:
            raise IntegrityError("admission Chat Session identity mismatch")
        return chat

    async def _existing_admission(self, session, turn):
        if turn["session_id"] != session["id"]:
            raise IntegrityError("admission Turn Session identity mismatch")
        publication = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_publications WHERE turn_id=%s AND kind='admission' ORDER BY id LIMIT 1 FOR UPDATE",
            (turn["id"],),
        )
        # Even the same owner must not execute twice after a lost response.
        return AdmissionResult(False, turn, publication)

    async def _reserve_new_turn(
        self,
        identity,
        session,
        chat,
        question_key,
        runtime_key,
        operation_key,
        execution_identity_text,
    ):
        generation = session["generation"] + 1
        values = {
            "session_id": session["id"],
            "chat_id": chat["id"],
            "question_msg_key": question_msg_key_bytes(question_key),
            "runtime_turn_key": runtime_key,
            "turn_order": chat["next_turn_order"],
            "context_epoch": session["context_epoch"],
            "generation": generation,
            "state": "reserved",
        }
        turn_id = await self.db.insert(
            f"INSERT INTO swe_conversation_turns ({','.join(values)}) VALUES ({','.join(['%s'] * len(values))})",
            tuple(values.values()),
        )
        if not turn_id:
            raise IntegrityError("Turn reservation returned no identity")
        await self.db.execute(
            "UPDATE swe_conversation_sessions SET generation=%s,owner_key=%s,active_turn_id=%s,lease_until=UTC_TIMESTAMP(6)+INTERVAL %s SECOND WHERE id=%s",
            (
                generation,
                self.owner_key.encode("utf-8"),
                turn_id,
                self.lease_seconds,
                session["id"],
            ),
        )
        await self.db.execute(
            "UPDATE swe_conversation_chats SET next_turn_order=next_turn_order+1 WHERE id=%s",
            (chat["id"],),
        )
        request = PublicationRequest(
            operation_key,
            identity.scope,
            "admission",
            self.owner_key,
            generation,
            session_id=session["id"],
            chat_id=chat["id"],
            turn_id=turn_id,
            base_state_revision=session["state_revision"],
            base_history_revision=chat["history_revision"],
            epoch=chat["epoch"],
        )
        plan = CommitPlan(
            recovery_block="recovery",
            messages=(
                MessageInstall(
                    "question",
                    "anchor",
                    "user",
                    source_msg_key=question_key,
                    origin_chat_id=chat["id"],
                    turn_id=turn_id,
                    context_epoch=session["context_epoch"],
                ),
            ),
            turn_install=TurnInstall(
                "admission",
                execution_identity_text=execution_identity_text,
            ),
        )
        publication = await self.publications.reserve(request, plan)
        authority = ExecutionAuthority(
            identity,
            session["id"],
            chat["id"],
            turn_id,
            generation,
            self.owner_key,
        )
        return AdmissionResult(
            True,
            {**values, "id": turn_id},
            publication,
            authority,
        )

    async def publish_admission(self, admission, question, recovery):
        if not admission.created or admission.authority is None:
            raise ConflictError(
                "original admission may only be observed, never replayed",
            )
        if question.get("role") != "user" or question.get(
            "id",
        ) != admission.turn["question_msg_key"].decode("utf-8"):
            raise IntegrityError(
                "admission question identity or role mismatch",
            )
        operation = admission.publication["operation_key"].decode("ascii")
        await self.publications.write_bundle(
            operation,
            [
                BundleBlock("question", "anchor", _json_bytes(question)),
                BundleBlock("recovery", "recovery", _json_bytes(recovery)),
            ],
        )
        return await self.publications.commit(operation)

    async def finish(
        self,
        authority,
        history_status,
        recovery,
        *,
        operation_key,
        messages=(),
        blocks=(),
    ):
        """Publish terminal state and release only through the original atomic plan."""
        turn_install = TurnInstall("terminal", history_status=history_status)
        BundleStore.path_for(operation_key)
        recovery_bytes = _json_bytes(recovery)
        try:
            async with self.db.transaction():
                await self._assert_writes_allowed(authority.identity)
                associations = await self._turn_associations(
                    authority.identity,
                    authority.chat_id,
                    authority.turn_id,
                )
                self._validate_authority(authority, *associations)
                session, chat, turn = associations
                if turn["state"] == "reserved":
                    raise ConflictError(
                        "unadmitted Turn cannot publish a terminal",
                    )
                if (
                    turn["state"] == "stopping"
                    and history_status == "completed"
                ):
                    raise ConflictError("Stop won before natural completion")
                request = PublicationRequest(
                    operation_key,
                    authority.identity.scope,
                    "terminal",
                    authority.owner_key,
                    authority.generation,
                    session_id=authority.session_id,
                    chat_id=authority.chat_id,
                    turn_id=authority.turn_id,
                    base_state_revision=session["state_revision"],
                    base_history_revision=chat["history_revision"],
                    epoch=chat["epoch"],
                )
                await self.publications.reserve(
                    request,
                    CommitPlan(
                        recovery_block="recovery",
                        messages=tuple(messages),
                        turn_install=turn_install,
                    ),
                )
            await self.publications.write_bundle(
                operation_key,
                [BundleBlock("recovery", "recovery", recovery_bytes), *blocks],
            )
            return await self.publications.commit(operation_key)
        except (StorageError, ValueError, TypeError, KeyError):
            raise
        except (Exception, asyncio.CancelledError) as error:
            raise UnknownCommit(operation_key) from error

    async def _session(self, identity):
        if not isinstance(identity, SessionIdentity):
            raise TypeError("execution requires a full Session identity")
        row = await self.db.fetch_one(
            "SELECT *,lease_until>UTC_TIMESTAMP(6) AS lease_valid FROM "
            f"swe_conversation_sessions WHERE {IDENTITY_WHERE} FOR UPDATE",
            identity.sql_values,
        )
        if row is None:
            raise KeyError(identity.session_id)
        actual = tuple(
            row[key]
            for key in (
                "tenant_key",
                "source_key",
                "agent_key",
                "user_key",
                "channel_key",
                "external_session_key",
            )
        )
        if actual != identity.sql_values:
            raise IntegrityError("Session identity mismatch")
        return row

    async def _turn_associations(self, identity, chat_id, turn_id):
        session = await self._session(identity)
        chat = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_chats WHERE id=%s FOR UPDATE",
            (chat_id,),
        )
        turn = await self.db.fetch_one(
            "SELECT * FROM swe_conversation_turns WHERE id=%s FOR UPDATE",
            (turn_id,),
        )
        if chat is None or chat["session_id"] != session["id"]:
            raise IntegrityError("execution Chat association mismatch")
        if (
            tuple(
                chat[key] for key in ("tenant_key", "source_key", "agent_key")
            )
            != identity.scope.sql_values
        ):
            raise IntegrityError("execution Chat scope mismatch")
        if (
            turn is None
            or turn["session_id"] != session["id"]
            or turn["chat_id"] != chat_id
        ):
            raise IntegrityError("execution Turn association mismatch")
        return session, chat, turn

    @staticmethod
    def _validate_authority(authority, session, chat, turn):
        current = (
            session["id"],
            session["generation"],
            session["owner_key"],
            session["active_turn_id"],
        )
        expected = (
            authority.session_id,
            authority.generation,
            authority.owner_key.encode("utf-8"),
            authority.turn_id,
        )
        if current != expected or not session["lease_valid"]:
            raise ConflictError("execution authority expired or changed")
        if (
            chat["deleted_at"] is not None
            or turn["generation"] != authority.generation
        ):
            raise ConflictError("execution authority Chat or Turn changed")
        if turn["state"] not in LIVE_STATES:
            raise ConflictError("execution authority is terminal")

    async def start(self, authority):
        async with self.db.transaction():
            await self._assert_writes_allowed(authority.identity)
            associations = await self._turn_associations(
                authority.identity,
                authority.chat_id,
                authority.turn_id,
            )
            self._validate_authority(authority, *associations)
            turn = associations[2]
            if turn["state"] != "admitted":
                raise ConflictError(
                    "Turn is not newly admitted; execution cannot replay",
                )
            await self.db.execute(
                "UPDATE swe_conversation_turns SET state=%s WHERE id=%s AND state='admitted'",
                ("running", authority.turn_id),
            )
            return {**turn, "state": "running"}

    async def renew(self, authority):
        async with self.db.transaction():
            await self._assert_writes_allowed(authority.identity)
            associations = await self._turn_associations(
                authority.identity,
                authority.chat_id,
                authority.turn_id,
            )
            self._validate_authority(authority, *associations)
            if associations[2]["state"] not in (
                "admitted",
                "running",
                "stopping",
            ):
                raise ConflictError("settling Turn cannot continue execution")
            await self.db.execute(
                "UPDATE swe_conversation_sessions SET lease_until=UTC_TIMESTAMP(6)+INTERVAL %s SECOND WHERE id=%s",
                (self.lease_seconds, authority.session_id),
            )

    async def request_stop(self, identity, question_key):
        question = question_msg_key_bytes(question_key)
        async with self.db.transaction():
            await self._assert_writes_allowed(identity)
            session = await self._session(identity)
            candidate = await self.db.fetch_one(
                "SELECT * FROM swe_conversation_turns WHERE session_id=%s AND question_msg_key=%s",
                (session["id"], question),
            )
            if candidate is None or candidate["question_msg_key"] != question:
                raise ConflictError("question is not admitted in this Session")
            _, _, turn = await self._turn_associations(
                identity,
                candidate["chat_id"],
                candidate["id"],
            )
            if turn["state"] in TERMINAL_STATES or turn["state"] in (
                "stopping",
                "settling",
            ):
                return turn
            if (
                turn["state"] not in ("admitted", "running")
                or session["active_turn_id"] != turn["id"]
            ):
                raise ConflictError("question is not active and admitted")
            await self.db.execute(
                "UPDATE swe_conversation_turns SET state=%s WHERE id=%s",
                ("stopping", turn["id"]),
            )
            return {**turn, "state": "stopping"}
