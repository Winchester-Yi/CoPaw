# -*- coding: utf-8 -*-
"""Typed Message occurrences and Turn transitions for one publication commit."""

from dataclasses import asdict, dataclass, fields

from .runtime_ids import runtime_identity
from ...database.prepared import PreparedInsert
from .chat_mutations import load_metadata_json, utc_datetime
from .errors import ConflictError, IntegrityError
from .identity import identity_bytes

LAYERS = frozenset({"memory", "task", "error", "anchor", "archive"})
TERMINAL_STATES = {
    "completed": "succeeded",
    "cancelled": "stopped",
    "stopped": "stopped",
    "failed": "failed",
    "interrupted": "interrupted",
}


def _positive_or_none(value):
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError("association must be a positive integer")


def _from_dict(cls, value):
    if not isinstance(value, dict) or set(value) - {
        item.name for item in fields(cls)
    }:
        raise ValueError("unknown installation field")
    return cls(**value)


@dataclass(frozen=True)
class MessageInstall:
    block_name: str
    layer: str
    role: str
    source_msg_key: str | None = None
    source_occurrence_no: int = 0
    kind: str = "message"
    task_run_key: str | None = None
    origin_chat_id: int | None = None
    archive_chat_id: int | None = None
    turn_id: int | None = None
    context_epoch: int = 0
    in_context: bool = False
    current_visible: bool = True
    message_timestamp: str | None = None
    source_message_id: int | None = None

    def __post_init__(self):
        for value, limit in (
            (self.block_name, 256),
            (self.role, 32),
            (self.kind, 64),
        ):
            identity_bytes(value, limit)
            if not value:
                raise ValueError(
                    "message block, role and kind must be nonempty",
                )
        if self.layer not in LAYERS:
            raise ValueError("invalid message layer")
        for value in (self.source_occurrence_no, self.context_epoch):
            if type(value) is not int or value < 0:
                raise ValueError("message order metadata must be nonnegative")
        if (
            type(self.in_context) is not bool
            or type(self.current_visible) is not bool
        ):
            raise ValueError("message membership flags must be booleans")
        self._validate_associations()
        for value, limit in (
            (self.source_msg_key, 1024),
            (self.task_run_key, 256),
        ):
            if value is not None:
                identity_bytes(value, limit)
        if self.message_timestamp is not None:
            utc_datetime(self.message_timestamp)

    def _validate_associations(self):
        _positive_or_none(self.source_message_id)
        if self.source_message_id is not None and self.layer != "archive":
            raise ValueError(
                "only archive occurrences may reuse a source locator",
            )
        for value in (self.origin_chat_id, self.archive_chat_id, self.turn_id):
            _positive_or_none(value)
        if (self.layer == "archive") != (self.archive_chat_id is not None):
            raise ValueError(
                "archive layer requires only an archive Chat association",
            )
        if self.layer == "archive" and self.in_context:
            raise ValueError(
                "archive occurrences cannot remain in active context",
            )

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return _from_dict(cls, value)


@dataclass(frozen=True)
class TurnInstall:
    phase: str
    history_status: str | None = None
    execution_identity_text: str | None = None

    def __post_init__(self):
        if self.phase not in ("admission", "terminal"):
            raise ValueError("invalid Turn installation phase")
        if self.phase == "admission" and self.history_status is not None:
            raise ValueError("admission cannot supply a terminal status")
        if (
            self.phase == "terminal"
            and self.history_status not in TERMINAL_STATES
        ):
            raise ValueError("terminal requires an existing history status")
        if self.execution_identity_text is not None:
            value = load_metadata_json(self.execution_identity_text)
            allowed = {"kind", "expert_id", "expert_name", "binding_revision"}
            if not isinstance(value, dict) or set(value) - allowed:
                raise ValueError("execution identity contains content fields")
            if len(self.execution_identity_text.encode("utf-8")) > 16384:
                raise ValueError("execution identity exceeds byte limit")
            for key, item in value.items():
                if key == "binding_revision":
                    if type(item) is not int or item < 0:
                        raise ValueError(
                            "execution identity binding revision invalid",
                        )
                elif item is not None:
                    identity_bytes(item, 1024)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return _from_dict(cls, value)


def _message_values(message, request, row, index, order, *, sources=None):
    source = (sources or {}).get(message.source_message_id)
    if (
        source is not None
        and source["service_unit_key"] != request.scope.sql_values[0]
    ):
        raise IntegrityError("message source service unit mismatch")
    extent = index.blocks.get(message.block_name)
    if source is None and (
        extent is None
        or extent.kind
        not in (
            "message",
            "memory",
            "anchor",
            "archive",
            "task",
            "error",
        )
    ):
        raise IntegrityError(
            "message payload block is missing or has invalid kind",
        )
    if (
        message.archive_chat_id is not None
        and message.archive_chat_id != request.chat_id
    ):
        raise IntegrityError("archive message Chat association mismatch")
    if (
        message.layer != "archive"
        and message.origin_chat_id is not None
        and message.origin_chat_id != request.chat_id
    ):
        raise IntegrityError("anchor origin Chat association mismatch")
    if (
        message.layer != "archive"
        and message.turn_id is not None
        and message.turn_id != request.turn_id
    ):
        raise IntegrityError("message Turn association mismatch")
    archive = message.layer == "archive"
    return {
        "service_unit_key": request.scope.sql_values[0],
        "session_id": request.session_id,
        "origin_chat_id": message.origin_chat_id,
        "archive_chat_id": message.archive_chat_id,
        "layer": message.layer,
        "session_order": None if archive else order,
        "archive_order": order if archive else None,
        "turn_id": message.turn_id,
        "source_msg_key": (
            identity_bytes(message.source_msg_key, 1024)
            if message.source_msg_key is not None
            else None
        ),
        "source_occurrence_no": message.source_occurrence_no,
        "role_key": identity_bytes(message.role, 32),
        "kind_key": identity_bytes(message.kind, 64),
        "task_run_key": (
            identity_bytes(message.task_run_key, 256)
            if message.task_run_key is not None
            else None
        ),
        "publication_id": source["publication_id"] if source else row["id"],
        "byte_offset": source["byte_offset"] if source else extent.offset,
        "byte_length": source["byte_length"] if source else extent.length,
        "archive_publication_id": row["id"] if archive else None,
        "context_epoch": message.context_epoch,
        "in_context": int(message.in_context),
        "current_visible": int(message.current_visible),
        "message_timestamp": (
            utc_datetime(message.message_timestamp)
            if message.message_timestamp is not None
            else None
        ),
    }


MESSAGE_FIELDS = (
    "service_unit_key",
    "session_id",
    "origin_chat_id",
    "archive_chat_id",
    "layer",
    "session_order",
    "archive_order",
    "turn_id",
    "source_msg_key",
    "source_occurrence_no",
    "role_key",
    "kind_key",
    "task_run_key",
    "publication_id",
    "byte_offset",
    "byte_length",
    "archive_publication_id",
    "context_epoch",
    "in_context",
    "current_visible",
    "message_timestamp",
)
MESSAGE_INSERT = PreparedInsert(
    f"INSERT INTO swe_conversation_messages ({','.join(MESSAGE_FIELDS)}) VALUES ",
    "(" + ",".join(["%s"] * len(MESSAGE_FIELDS)) + ")",
)


async def install_messages(
    db,
    request,
    messages,
    publication,
    index,
    associations,
    *,
    sources=None,
):
    session, chat, _ = associations
    if session is None:
        raise IntegrityError("Message publication requires a Session")
    online = session["history_high"]
    archived = chat["history_high"] if chat is not None else 0

    tickets = []
    if messages:
        first = await runtime_identity(db)
        if first is not None:
            tickets = [first] + [
                await runtime_identity(db) for _ in range(len(messages) - 1)
            ]
    shared_ids = bool(tickets)
    message_insert = MESSAGE_INSERT
    if shared_ids:
        message_insert = PreparedInsert(
            f"INSERT INTO swe_conversation_messages (id,{','.join(MESSAGE_FIELDS)}) VALUES ",
            "(" + ",".join(["%s"] * (len(MESSAGE_FIELDS) + 1)) + ")",
        )

    def rows():
        nonlocal online, archived
        for ordinal, message in enumerate(messages):
            if (
                message.layer != "archive"
                and message.context_epoch != session.get("context_epoch", 0)
            ):
                raise ConflictError("message context epoch changed")
            if message.layer == "archive":
                if chat is None:
                    raise IntegrityError("archive publication requires a Chat")
                archived += 1
                order = archived
            else:
                online += 1
                order = online
            values = _message_values(
                message,
                request,
                publication,
                index,
                order,
                sources=sources,
            )
            row = tuple(values[field] for field in MESSAGE_FIELDS)
            yield (tickets[ordinal], *row) if shared_ids else row

    if len(messages) == 1:
        await db.insert(
            message_insert.prefix + message_insert.row_template,
            next(rows()),
        )
    elif messages:
        await db.insert_prepared(message_insert, rows())
    if online != session["history_high"]:
        await db.execute(
            "UPDATE swe_conversation_sessions SET history_high=%s WHERE id=%s",
            (online, request.session_id),
        )
    if chat is not None:
        await db.execute(
            "UPDATE swe_conversation_chats SET history_high=%s,history_revision=history_revision+1,updated_at=UTC_TIMESTAMP(6) WHERE id=%s",
            (archived, request.chat_id),
        )


async def install_turn(db, request, plan, publication, associations):
    _, chat, turn = associations
    if turn is None or chat is None:
        raise IntegrityError("Turn installation requires locked Chat and Turn")
    if plan.phase == "admission":
        if turn["state"] != "reserved":
            raise ConflictError("Turn admission already changed")
        await db.execute(
            "UPDATE swe_conversation_turns SET state='admitted',admission_publication_id=%s,admitted_at=UTC_TIMESTAMP(6),execution_identity_text=%s WHERE id=%s",
            (publication["id"], plan.execution_identity_text, request.turn_id),
        )
        return
    if turn["state"] not in ("admitted", "running", "stopping", "settling"):
        raise ConflictError("Turn terminal state already committed")
    if turn["state"] == "stopping" and plan.history_status == "completed":
        raise ConflictError("Stop won before natural completion")
    state = TERMINAL_STATES[plan.history_status]
    await db.execute(
        "UPDATE swe_conversation_turns SET state=%s,history_status=%s,terminal_publication_id=%s,terminal_at=UTC_TIMESTAMP(6) WHERE id=%s",
        (state, plan.history_status, publication["id"], request.turn_id),
    )
    await db.execute(
        "UPDATE swe_conversation_sessions SET active_turn_id=NULL,owner_key=NULL,lease_until=NULL WHERE id=%s",
        (request.session_id,),
    )
    await db.execute(
        "UPDATE swe_conversation_chats SET status='idle',updated_at=UTC_TIMESTAMP(6) WHERE id=%s",
        (request.chat_id,),
    )
