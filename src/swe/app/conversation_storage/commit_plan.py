# -*- coding: utf-8 -*-
"""Body-free, versioned installation plans persisted before immutable NAS IO."""

from dataclasses import dataclass
import json

from .archive import (
    ArchiveInstall,
    CheckpointInstall,
    ContextReset,
    EventInstall,
)
from .archive_payloads import validate_archive_plan
from .chat_mutations import ChatMetadataInstall, load_metadata_json
from .errors import IntegrityError
from .identity import identity_bytes
from .message_installs import MessageInstall, TurnInstall
from .shares import ShareInstall

MAX_PLAN_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CommitPlan:
    """Body-free installation plan persisted before any NAS write."""

    recovery_block: str | None = None
    result_ids: tuple[int, ...] = ()
    chat_metadata: ChatMetadataInstall | None = None
    messages: tuple[MessageInstall, ...] = ()
    turn_install: TurnInstall | None = None
    archive: ArchiveInstall | None = None
    checkpoint: CheckpointInstall | None = None
    events: EventInstall | None = None
    reset: ContextReset | None = None
    prune_ids: tuple[int, ...] = ()
    share: ShareInstall | None = None

    def serialize(self):
        self._validate_share_plan()
        validate_archive_plan(self)
        if self.recovery_block is not None:
            identity_bytes(self.recovery_block, 256)
            if not self.recovery_block:
                raise ValueError("recovery block must be nonempty")
        self._validate_result_ids()
        value = {
            "version": 1,
            "recovery_block": self.recovery_block,
            "result_ids": self.result_ids,
        }
        if self.share is not None:
            value["share"] = self.share.to_dict()
        self._serialize_chat_metadata(value)
        if self.messages:
            if len(self.messages) > 4096:
                raise ValueError(
                    "publication message batch exceeds record limit",
                )
            value["messages"] = [
                message.to_dict() for message in self.messages
            ]
        if self.turn_install is not None:
            if self.recovery_block is None:
                raise ValueError(
                    "Turn admission or terminal requires a complete recovery root",
                )
            if self.turn_install.phase == "admission":
                anchors = [
                    message
                    for message in self.messages
                    if message.layer == "anchor" and message.role == "user"
                ]
                if len(anchors) != 1:
                    raise ValueError(
                        "admission requires exactly one durable user anchor",
                    )
            value["turn_install"] = self.turn_install.to_dict()
        for key in ("archive", "checkpoint", "events", "reset"):
            item = getattr(self, key)
            if item is not None:
                value[key] = item.to_dict()
        if self.reset is not None and (
            self.recovery_block is None
            or any((self.messages, self.archive, self.events, self.checkpoint))
        ):
            raise ValueError("reset requires only a complete recovery root")
        if self.prune_ids:
            if (
                self.recovery_block is None
                or len(self.prune_ids) > 4096
                or any(
                    type(item) is not int or item <= 0
                    for item in self.prune_ids
                )
            ):
                raise ValueError(
                    "pruning requires a complete root and bounded occurrence IDs",
                )
            value["prune_ids"] = self.prune_ids
        text = json.dumps(value, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_PLAN_BYTES:
            raise ValueError("publication commit plan exceeds byte limit")
        return text

    def _validate_result_ids(self):
        if len(self.result_ids) > 4096 or any(
            type(value) is not int or value <= 0 for value in self.result_ids
        ):
            raise ValueError(
                "publication result IDs must be bounded positive integers",
            )

    def _validate_share_plan(self):
        if self.share is not None and any(
            (
                self.recovery_block is not None,
                self.result_ids,
                self.chat_metadata,
                self.messages,
                self.turn_install,
                self.archive,
                self.checkpoint,
                self.events,
                self.reset,
                self.prune_ids,
            ),
        ):
            raise ValueError("share installation must be an independent plan")

    def _serialize_chat_metadata(self, value):
        if self.chat_metadata is not None:
            if (
                self.recovery_block is not None
                or self.messages
                or self.turn_install is not None
                or any(
                    (
                        self.archive,
                        self.checkpoint,
                        self.events,
                        self.reset,
                        self.prune_ids,
                    ),
                )
            ):
                raise ValueError(
                    "Chat metadata cannot replace a recovery root",
                )
            value["chat_metadata"] = self.chat_metadata.to_dict()

    @classmethod
    def parse(cls, text):
        try:
            if len(text.encode("utf-8")) > MAX_PLAN_BYTES:
                raise ValueError("oversized commit plan")
            value = load_metadata_json(text)
            expected = {"version", "recovery_block", "result_ids"}
            if (
                not expected <= set(value)
                or set(value)
                - (
                    expected
                    | {
                        "chat_metadata",
                        "messages",
                        "turn_install",
                        "archive",
                        "checkpoint",
                        "events",
                        "reset",
                        "prune_ids",
                        "share",
                    }
                )
                or value.pop("version") != 1
            ):
                raise ValueError("unknown commit plan version")
            value["result_ids"] = tuple(value["result_ids"])
            if "share" in value:
                value["share"] = ShareInstall.from_dict(value["share"])
            if "chat_metadata" in value:
                value["chat_metadata"] = ChatMetadataInstall.from_dict(
                    value["chat_metadata"],
                )
            if "messages" in value:
                value["messages"] = tuple(
                    MessageInstall.from_dict(item)
                    for item in value["messages"]
                )
            if "turn_install" in value:
                value["turn_install"] = TurnInstall.from_dict(
                    value["turn_install"],
                )
            for key, cls_type in (
                ("archive", ArchiveInstall),
                ("checkpoint", CheckpointInstall),
                ("events", EventInstall),
                ("reset", ContextReset),
            ):
                if key in value:
                    value[key] = cls_type.from_dict(value[key])
            if "prune_ids" in value:
                value["prune_ids"] = tuple(value["prune_ids"])
            plan = cls(**value)
            plan.serialize()
            return plan
        except (ValueError, TypeError, AttributeError, KeyError) as error:
            raise IntegrityError(
                "publication commit plan is invalid",
            ) from error
