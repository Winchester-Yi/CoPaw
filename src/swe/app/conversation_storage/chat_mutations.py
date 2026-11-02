# -*- coding: utf-8 -*-
"""Typed body-free Chat mutations installed with their NAS publication."""

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import json

from .errors import ConflictError, IntegrityError
from .identity import chat_key_bytes, identity_bytes

LIGHT_METADATA_TYPES = {
    "plan_mode_enabled": bool,
    "goal_mode_enabled": bool,
    "session_title_generated": bool,
    "session_kind": str,
    "task_job_id": str,
    "management_owner": str,
    "creator_user_id": str,
    "source_id": str,
    "agent_id": str,
}
MAX_LIGHT_METADATA_BYTES = 16384


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metadata field")
        result[key] = value
    return result


def _reject_nonfinite(value):
    raise ValueError("metadata cannot contain a nonfinite number")


def load_metadata_json(text):
    return json.loads(
        text,
        object_pairs_hook=_unique_fields,
        parse_constant=_reject_nonfinite,
    )


def split_metadata(metadata):
    """Unknown fields stay recoverable on NAS; binding has typed authority."""
    if not isinstance(metadata, dict):
        raise ValueError("Chat metadata must be an object")
    light, content = {}, {}
    for key, value in metadata.items():
        if key == "expert_main_binding":
            continue
        expected = LIGHT_METADATA_TYPES.get(key)
        if expected is not None and type(value) is expected:
            if isinstance(value, str) and len(value.encode("utf-8")) > 1024:
                content[key] = value
            else:
                light[key] = value
        else:
            content[key] = value
    return light, content


def validate_light_metadata(text):
    try:
        if (
            not isinstance(text, str)
            or len(text.encode("utf-8")) > MAX_LIGHT_METADATA_BYTES
        ):
            raise ValueError("Chat metadata exceeds light metadata limit")
        value = load_metadata_json(text)
        light, content = split_metadata(value)
        if content or value != light:
            raise ValueError(
                "Chat metadata contains content or server binding",
            )
        return light
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Chat metadata is invalid") from error


def utc_datetime(value):
    if not isinstance(value, str):
        raise ValueError("Chat timestamp must be an ISO string")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Chat timestamp requires timezone")
    return stamp.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class ChatMetadataInstall:
    external_chat_key: str
    user_id: str
    channel: str
    title: str
    metadata_text: str
    created_at: str
    updated_at: str
    content_meta_block: str | None = None
    expected_metadata_revision: int | None = None
    expert_id: str | None = None
    expert_binding_revision: int = 0
    status: str = "idle"
    create_if_absent: bool = False
    target_session_id: int | None = None

    def __post_init__(self):
        chat_key_bytes(self.external_chat_key)
        identity_bytes(self.user_id, 256)
        identity_bytes(self.channel, 64)
        if not isinstance(self.title, str):
            raise ValueError("Chat title must be a string")
        validate_light_metadata(self.metadata_text)
        utc_datetime(self.created_at)
        utc_datetime(self.updated_at)
        self._validate_optional_fields()

    def _validate_optional_fields(self):
        if self.content_meta_block is not None:
            identity_bytes(self.content_meta_block, 256)
            if not self.content_meta_block:
                raise ValueError("content metadata block must be nonempty")
        if self.expert_id is not None:
            identity_bytes(self.expert_id, 256)
        for value in (
            self.expert_binding_revision,
            self.expected_metadata_revision,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(
                    "Chat metadata revision must be a nonnegative integer",
                )
        if self.status not in ("idle", "running", "stopping"):
            raise ValueError("invalid Chat status")
        if type(self.create_if_absent) is not bool:
            raise ValueError("invalid draft creation flag")
        if self.target_session_id is not None and (
            type(self.target_session_id) is not int
            or self.target_session_id <= 0
        ):
            raise ValueError("invalid target Session identity")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {
            item.name for item in fields(cls)
        }:
            raise ValueError("unknown Chat metadata mutation field")
        try:
            return cls(**value)
        except TypeError as error:
            raise ValueError("incomplete Chat metadata mutation") from error


def content_locator(plan, row, index):
    if plan.content_meta_block is None:
        return None, None, None
    extent = index.blocks.get(plan.content_meta_block)
    if extent is None or extent.kind != "content_meta":
        raise IntegrityError("Chat content metadata block is missing")
    return row["id"], extent.offset, extent.length


async def install_chat_metadata(
    db,
    request,
    plan,
    publication,
    index,
    associations,
    *,
    target_session=None,
):
    """Caller holds guard (create), Session, Chat, then publication locks."""
    session, chat, _ = associations
    if session is None or request.turn_id is not None:
        raise IntegrityError(
            "Chat metadata publication requires a Session and no Turn",
        )
    if request.chat_id is None and plan.target_session_id is not None:
        raise IntegrityError("new Chat cannot override its target Session")
    target = target_session if target_session is not None else session
    if target["user_key"] != identity_bytes(plan.user_id, 256) or target[
        "channel_key"
    ] != identity_bytes(plan.channel, 64):
        raise IntegrityError("Chat metadata Session identity mismatch")
    locator = content_locator(plan, publication, index)
    if request.chat_id is None:
        return await _create_chat(
            db,
            request,
            plan,
            locator,
            session,
            existing=chat,
        )
    if chat is None or chat["deleted_at"] is not None:
        raise ConflictError("Chat was deleted")
    if chat["external_chat_key"] != chat_key_bytes(plan.external_chat_key):
        raise IntegrityError("Chat metadata external identity mismatch")
    _validate_rebind(plan, session, target)
    return await _update_chat(db, request, plan, locator, chat)


def _validate_rebind(plan, session, target):
    if plan.target_session_id is not None:
        for associated in (session, target):
            if associated["active_turn_id"] is not None:
                raise ConflictError("cannot rebind a busy Session")
        if target["id"] != plan.target_session_id:
            raise IntegrityError("target Session identity mismatch")


async def _create_chat(db, request, plan, locator, session, *, existing=None):
    if plan.expected_metadata_revision is not None:
        raise ConflictError("new Chat cannot require an existing revision")
    if plan.create_if_absent and session.get("draft_chat_id") is not None:
        # A deleted draft remains a tombstone; retry must never create another.
        raise ConflictError("draft Chat already has a committed identity")
    if plan.create_if_absent:
        if existing is not None:
            await db.execute(
                "UPDATE swe_conversation_sessions SET draft_chat_id=%s WHERE id=%s",
                (existing["id"], request.session_id),
            )
            return existing["id"]
    high = await db.fetch_one(
        "SELECT COALESCE(MAX(registry_order),0) AS high FROM swe_conversation_chats "
        "WHERE service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s",
        request.scope.sql_values,
    )
    columns = (
        "session_id",
        "external_chat_key",
        "registry_order",
        "service_unit_key",
        "tenant_key",
        "source_key",
        "agent_key",
        "user_key",
        "channel_key",
        "title",
        "metadata_text",
        "expert_id",
        "expert_binding_revision",
        "content_meta_publication_id",
        "content_meta_offset",
        "content_meta_length",
        "epoch",
        "status",
        "created_at",
        "updated_at",
    )
    values = (
        request.session_id,
        chat_key_bytes(plan.external_chat_key),
        high["high"] + 1,
        *request.scope.sql_values,
        identity_bytes(plan.user_id, 256),
        identity_bytes(plan.channel, 64),
        plan.title,
        plan.metadata_text,
        (
            identity_bytes(plan.expert_id, 256)
            if plan.expert_id is not None
            else None
        ),
        plan.expert_binding_revision,
        *locator,
        1,
        plan.status,
        utc_datetime(plan.created_at),
        utc_datetime(plan.updated_at),
    )
    chat_id = await db.insert(
        f"INSERT INTO swe_conversation_chats ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))})",
        values,
    )
    if not chat_id:
        raise IntegrityError("Chat creation did not return its identity")
    if plan.create_if_absent:
        await db.execute(
            "UPDATE swe_conversation_sessions SET draft_chat_id=%s WHERE id=%s",
            (chat_id, request.session_id),
        )
    return chat_id


async def _update_chat(db, request, plan, locator, chat):
    if (
        plan.expected_metadata_revision is None
        or chat["metadata_revision"] != plan.expected_metadata_revision
    ):
        raise ConflictError("Chat metadata revision changed")
    light = validate_light_metadata(plan.metadata_text)
    expert_id = chat["expert_id"]
    binding_revision = chat["expert_binding_revision"]
    if expert_id is not None and any(
        light.get(key) is True
        for key in ("plan_mode_enabled", "goal_mode_enabled")
    ):
        expert_id = None
        binding_revision += 1
    result = await db.execute(
        "UPDATE swe_conversation_chats SET title=%s,metadata_text=%s,"
        "session_id=%s,user_key=%s,channel_key=%s,"
        "expert_id=%s,expert_binding_revision=%s,"
        "content_meta_publication_id=%s,content_meta_offset=%s,content_meta_length=%s,"
        "metadata_revision=metadata_revision+1,updated_at=%s "
        "WHERE id=%s AND deleted_at IS NULL AND metadata_revision=%s",
        (
            plan.title,
            plan.metadata_text,
            plan.target_session_id or request.session_id,
            identity_bytes(plan.user_id, 256),
            identity_bytes(plan.channel, 64),
            expert_id,
            binding_revision,
            *locator,
            utc_datetime(plan.updated_at),
            request.chat_id,
            plan.expected_metadata_revision,
        ),
    )
    if result != 1:
        raise ConflictError("Chat metadata update conflicted")
    return request.chat_id
