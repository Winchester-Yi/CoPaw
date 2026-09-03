# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..runner.api import (
    _message_original_id,
    _redact_hidden_context_messages,
    _slice_answer_turn,
)
from ..runner.models import ChatMessage
from .models import ChatShareRecord, ChatShareSnapshot


class ChatSharingService:
    """Create and read immutable Chat share snapshots."""

    def __init__(self, store: Any, snapshot_root: Path):
        self.store = store
        self.snapshot_root = Path(snapshot_root).resolve()

    @staticmethod
    def _safe_component(value: str, field: str) -> str:
        normalized = str(value).strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or Path(normalized).name != normalized
        ):
            raise ValueError(f"Invalid {field}")
        return normalized

    @staticmethod
    def _message_id(message: ChatMessage) -> str | None:
        original_id = _message_original_id(message)
        if original_id:
            return original_id
        value = getattr(message, "id", None)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _normalise_messages(messages: Iterable[Any]) -> list[ChatMessage]:
        return [
            (
                item
                if isinstance(item, ChatMessage)
                else ChatMessage.model_validate(item)
            )
            for item in messages
        ]

    async def create_snapshot(
        self,
        *,
        chat_id: str,
        chat_name: str | None,
        messages: Iterable[Any],
        selected_turn_ids: list[str],
        turn_statuses: dict[str, str],
        creator_id: str,
        tenant_id: str = "default",
    ) -> ChatShareRecord:
        if not selected_turn_ids:
            raise ValueError("Select at least one answer turn")

        tenant_component = self._safe_component(tenant_id, "tenant")

        normalised = self._normalise_messages(messages)
        selected: list[tuple[int, list[ChatMessage]]] = []
        for turn_id in dict.fromkeys(selected_turn_ids):
            if turn_statuses.get(turn_id) != "completed":
                raise ValueError("Only completed answer turns can be shared")
            turn = _slice_answer_turn(normalised, msgid=turn_id)
            if turn is None:
                raise ValueError("Selected answer turn was not found")
            anchor = next(
                index
                for index, message in enumerate(normalised)
                if self._message_id(message) == turn_id
            )
            selected.append((anchor, turn))

        selected.sort(key=lambda item: item[0])
        turn_messages = [message for _, turn in selected for message in turn]
        redacted = _redact_hidden_context_messages(turn_messages)
        token = secrets.token_urlsafe(32)
        relative_key = f"{tenant_component}/chat_shares/{token}.json"
        target = self.snapshot_root / relative_key
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = ChatShareSnapshot(
            chat_name=(chat_name or "分享的会话").strip() or "分享的会话",
            messages=[message.model_dump(mode="json") for message in redacted],
        ).model_dump(mode="json")
        await asyncio.to_thread(self._atomic_write_json, target, payload)

        record = ChatShareRecord(
            token=token,
            chat_id=chat_id,
            creator_id=creator_id,
            snapshot_key=relative_key,
            tenant_id=tenant_component,
            created_at=datetime.now(timezone.utc),
        )
        try:
            await self.store.create(record)
        except Exception:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return record

    async def get_snapshot(self, token: str) -> dict[str, Any]:
        record = await self.store.get(token)
        if record is None:
            raise KeyError("Share not found")
        token_component = self._safe_component(token, "token")
        tenant_component = self._safe_component(record.tenant_id, "tenant")
        expected_key = f"{tenant_component}/chat_shares/{token_component}.json"
        if record.snapshot_key != expected_key:
            raise OSError("Invalid snapshot scope")
        target = (self.snapshot_root / expected_key).resolve()
        try:
            target.relative_to(self.snapshot_root)
        except ValueError as exc:
            raise OSError("Invalid snapshot path") from exc
        payload = await asyncio.to_thread(target.read_text, encoding="utf-8")
        snapshot = json.loads(payload)
        await self.store.record_access(token)
        return snapshot

    @staticmethod
    def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
        content = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
