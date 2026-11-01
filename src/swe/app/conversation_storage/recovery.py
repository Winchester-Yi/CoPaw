# -*- coding: utf-8 -*-
"""Settle persisted publications after verified owner exit, never resume Agent IO."""

from dataclasses import dataclass
import inspect

from .bundle import new_operation_key
from .errors import ConflictError, IntegrityError
from .execution import ExecutionAuthority, TERMINAL_STATES
from .identity import SessionIdentity, identity_bytes
from .publications import PublicationStore, _request


def _text(value):
    return (
        bytes(value).decode("utf-8")
        if isinstance(value, (bytes, bytearray))
        else value
    )


@dataclass(frozen=True)
class OwnerExitProof:
    owner_key: str
    generation: int
    evidence_ref: str
    operator: str
    owner_stopped: bool
    io_drained: bool

    def __post_init__(self):
        identity_bytes(self.owner_key, 128)
        for value in (self.owner_key, self.evidence_ref, self.operator):
            identity_bytes(value, 1024)
            if not value:
                raise ValueError("owner stop evidence fields must be nonempty")
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("owner generation must be positive")


class EndedOwnerRecovery:
    """Explicit maintenance path usable by a new process after owner death."""

    def __init__(self, storage, *, verify_stopped):
        self.storage = storage
        self.verify_stopped = verify_stopped

    async def _verify(self, proof):
        if not isinstance(proof, OwnerExitProof) or (
            proof.owner_stopped is not True or proof.io_drained is not True
        ):
            raise ConflictError(
                "ended owner and drained IO evidence is required",
            )
        accepted = self.verify_stopped(proof)
        if inspect.isawaitable(accepted):
            accepted = await accepted
        if accepted is not True:
            raise ConflictError("owner stop evidence was not verified")

    async def recover(self, identity, turn_id, proof):
        await self._verify(proof)
        if (
            not isinstance(identity, SessionIdentity)
            or identity.scope != self.storage.scope
        ):
            raise IntegrityError(
                "recovery requires the exact trusted Session scope",
            )
        if type(turn_id) is not int or turn_id <= 0:
            raise ValueError("recovery Turn identity must be positive")
        authority, turn = await self._fence(identity, turn_id, proof)
        operations = await self.storage.db.fetch_all(
            "SELECT * FROM swe_conversation_publications WHERE turn_id=%s ORDER BY id",
            (turn_id,),
        )
        if not operations or not any(
            row["kind"] == "admission" for row in operations
        ):
            raise IntegrityError(
                "recovery Turn has no persisted admission operation",
            )
        for row in operations:
            request = _request(row)
            self._matches(authority, request)
            if row["state"] == "prepared":
                await self._settle_operation(authority, row)
        turn = await self.storage.db.fetch_one(
            "SELECT * FROM swe_conversation_turns WHERE id=%s",
            (turn_id,),
        )
        if turn["state"] not in TERMINAL_STATES:
            admission = next(
                row for row in operations if row["kind"] == "admission"
            )
            admission = await self.storage.publications.get(
                _text(admission["operation_key"]),
            )
            if admission["state"] == "aborted" and turn["state"] == "reserved":
                await self.storage.executions.reject_ended_admission(
                    authority,
                    _request(admission),
                    owner_stopped=True,
                )
            else:
                state = await self.storage.session_for(
                    identity.channel,
                ).get_session_state_dict(
                    identity.session_id,
                    identity.user_id,
                )
                await self.storage.executions.settle_ended_terminal(
                    authority,
                    state,
                    operation_key=new_operation_key(),
                    owner_stopped=True,
                )
        turn = await self.storage.db.fetch_one(
            "SELECT state,history_status FROM swe_conversation_turns WHERE id=%s",
            (turn_id,),
        )
        return {
            "turn_id": turn_id,
            "state": turn["state"],
            "history_status": turn["history_status"],
            "owner_key": proof.owner_key,
            "original_generation": proof.generation,
            "evidence_ref": proof.evidence_ref,
            "operator": proof.operator,
        }

    async def _fence(self, identity, turn_id, proof):
        service = self.storage.executions
        async with self.storage.db.transaction():
            await service._assert_writes_allowed(identity)
            session = await service._session(identity)
            candidate = await self.storage.db.fetch_one(
                "SELECT chat_id FROM swe_conversation_turns WHERE id=%s",
                (turn_id,),
            )
            if candidate is None:
                raise KeyError(turn_id)
            associations = await service._turn_associations(
                identity,
                candidate["chat_id"],
                turn_id,
            )
            session, chat, turn = associations
            admission = await self.storage.db.fetch_one(
                "SELECT * FROM swe_conversation_publications WHERE turn_id=%s AND kind='admission' ORDER BY id LIMIT 1 FOR UPDATE",
                (turn_id,),
            )
            if admission is None or (
                turn["generation"],
                _text(admission["owner_key"]),
                admission["generation"],
            ) != (proof.generation, proof.owner_key, proof.generation):
                raise ConflictError(
                    "recovery proof differs from original owner incarnation",
                )
            authority = ExecutionAuthority(
                identity,
                session["id"],
                chat["id"],
                turn_id,
                proof.generation,
                proof.owner_key,
            )
            self._matches(authority, _request(admission))
            if turn["state"] not in TERMINAL_STATES:
                PublicationStore._validate_reconciliation(
                    (
                        _request(admission)
                        if turn["state"] == "reserved"
                        else self._current_request(authority, session, chat)
                    ),
                    associations,
                    session["generation"],
                )
                await self.storage.db.execute(
                    "UPDATE swe_conversation_sessions SET generation=generation+1,owner_key=NULL,lease_until=NULL WHERE id=%s",
                    (session["id"],),
                )
            return authority, turn

    @staticmethod
    def _current_request(authority, session, chat):
        from .publications import PublicationRequest

        return PublicationRequest(
            new_operation_key(),
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

    @staticmethod
    def _matches(authority, request):
        if request.scope != authority.identity.scope or (
            request.session_id,
            request.chat_id,
            request.turn_id,
            request.generation,
            request.owner_key,
        ) != (
            authority.session_id,
            authority.chat_id,
            authority.turn_id,
            authority.generation,
            authority.owner_key,
        ):
            raise IntegrityError(
                "recovery publication differs from exact execution identity",
            )

    async def _settle_operation(self, authority, row):
        request = _request(row)
        publications = self.storage.publications
        if not row.get("durable_at"):
            try:
                await publications.confirm_bundle(request.operation_key)
            except FileNotFoundError:
                await publications.abort(request, owner_stopped=True)
                return
        session = await self.storage.session_for(
            authority.identity.channel,
        )._row(authority.identity)
        try:
            await publications.reconcile(
                request.operation_key,
                owner_stopped=True,
                expected_generation=session["generation"],
            )
        except ConflictError:
            await publications.abort(request, owner_stopped=True)
