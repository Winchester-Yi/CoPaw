# -*- coding: utf-8 -*-
"""Bounded recovery of signed outbox events from one trusted workspace."""

from __future__ import annotations

import hashlib
import json
import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from .outbox import verify_event
from .outbox_recovery import OutboxPage

logger = logging.getLogger(__name__)
# This cursor stays inside the trusted recovery checkpoint; it grants no read
# authority. A stable key lets any replica resume a page across restarts.
_RECOVERY_ARCHIVE_CURSOR_KEY = hashlib.sha256(
    b"swe-agentobs-recovery-archive-cursor-v1",
).digest()


class _RecoveryManager:
    def __init__(self, manager):
        self._manager = manager
        self._repo = getattr(manager, "_repo", None)

    async def list_chats_cursor(self, *, page_size, cursor=None):
        method = getattr(self._repo, "list_recovery_chats_cursor", None)
        if method is None:
            method = self._manager.list_chats_cursor
        return await method(page_size=page_size, cursor=cursor)

    async def get_chat(self, chat_id):
        method = getattr(self._repo, "get_recovery_chat", None)
        if method is None:
            method = self._manager.get_chat
        return await method(chat_id)


class _RecoveryWorkspace:
    def __init__(self, workspace):
        self._workspace = workspace
        self.chat_manager = _RecoveryManager(workspace.chat_manager)

    def __getattr__(self, name):
        return getattr(self._workspace, name)


class WorkspaceOutboxScanner:
    """Scan one already resolved workspace without starting its services.

    Cursors traverse a live Chat list; a completed cycle restarts at the head,
    so Chats reordered during a cycle are revisited in a later cycle.
    """

    def __init__(self, workspace: Any, scope: Any):
        from ..app.conversation_storage.runtime import resolved_scope

        if (
            resolved_scope(workspace, service_unit_id=scope.service_unit_id)
            != scope
        ):
            raise ValueError("workspace scope mismatch")
        self.workspace = workspace
        self.scope = scope
        self._recovery_workspace = _RecoveryWorkspace(workspace)

    @staticmethod
    def _cursor(value):
        if value is None:
            return {
                "chat_cursor": None,
                "chat_id": None,
                "next_chat": None,
                "phase": "online",
                "offset": 0,
                "archive_cursor": None,
            }
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError("invalid outbox cursor")
        data = json.loads(value)
        if (
            not isinstance(data, dict)
            or set(data)
            != {
                "chat_cursor",
                "chat_id",
                "next_chat",
                "phase",
                "offset",
                "archive_cursor",
            }
            or data["phase"] not in {"online", "archive"}
            or type(data["offset"]) is not int
            or data["offset"] < 0
        ):
            raise ValueError("invalid outbox cursor")
        if any(
            data[k] is not None and not isinstance(data[k], str)
            for k in (
                "chat_cursor",
                "chat_id",
                "next_chat",
                "archive_cursor",
            )
        ):
            raise ValueError("invalid outbox cursor")
        return data

    @staticmethod
    def _encode(state):
        return json.dumps(state, separators=(",", ":"))

    async def scan_page(self, cursor: str | None, limit: int) -> OutboxPage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid outbox page limit")
        try:
            state = self._cursor(cursor)
        except (ValueError, TypeError):
            logger.warning("Evidence outbox checkpoint reset")
            state = self._cursor(None)
        manager = self._recovery_workspace.chat_manager
        if state["chat_id"] is None:
            try:
                page = await manager.list_chats_cursor(
                    page_size=1,
                    cursor=state["chat_cursor"],
                )
            except ValueError as exc:
                if (
                    state["chat_cursor"] is None
                    or str(exc) != "Invalid chat pagination cursor"
                ):
                    raise
                logger.warning("Evidence outbox Chat cursor reset")
                state = self._cursor(None)
                page = await manager.list_chats_cursor(
                    page_size=1,
                    cursor=None,
                )
            if not page.items:
                state["chat_cursor"] = page.next_cursor
                return OutboxPage(
                    [],
                    self._encode(state) if page.next_cursor else None,
                )
            state["chat_id"] = page.items[0].id
            state["next_chat"] = page.next_cursor
        try:
            return await self._scan_chat(state, limit)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            # The next full cycle revisits this Chat. Do not let a damaged
            # root or archive prevent other Chats from being recovered.
            logger.warning(
                "Evidence outbox chat scan failed: %s",
                type(exc).__name__,
            )
            return self._next_chat(state)

    async def _scan_chat(self, state, limit):
        from ..agents.memory.conversation_archive import (
            ConversationArchiveStore,
        )

        chat = await self._recovery_workspace.chat_manager.get_chat(
            state["chat_id"],
        )
        if chat is None:
            return self._next_chat(state)
        records = []
        if state["phase"] == "online":
            root = await self.workspace.runner.session.get_persisted_session_state_dict(
                chat.session_id,
                chat.user_id,
            )
            agent = root.get("agent", {})
            if not isinstance(agent, dict):
                raise ValueError("invalid session agent")
            memory = agent.get("memory", {})
            entries = (
                memory.get("content", []) if isinstance(memory, dict) else []
            )
            if not isinstance(entries, list):
                raise ValueError("invalid session memory")
            end = min(len(entries), state["offset"] + limit)
            for entry in entries[state["offset"] : end]:
                raw = (
                    entry[0]
                    if isinstance(entry, (list, tuple)) and entry
                    else None
                )
                candidate = self._candidate(chat.id, chat.session_id, raw)
                if candidate is not None:
                    records.append(candidate)
            state["offset"] = end
            if end >= len(entries):
                state.update(phase="archive", offset=0)
            return OutboxPage(records, self._encode(state))
        archive = ConversationArchiveStore(
            self.workspace.workspace_dir / "dialog",
            cursor_secret=_RECOVERY_ARCHIVE_CURSOR_KEY,
        )
        try:
            page = await archive.read_page(
                chat.id,
                before=state["archive_cursor"],
                limit=limit,
            )
        except ValueError as exc:
            if (
                state["archive_cursor"] is None
                or str(exc) != "Invalid conversation archive cursor"
            ):
                raise
            # An old key or a removed boundary invalidates the checkpoint.
            # Restart once; replay is idempotent and an invalid archive will
            # still fail and yield to the next Chat.
            logger.warning("Evidence outbox archive cursor reset")
            state["archive_cursor"] = None
            page = await archive.read_page(chat.id, limit=limit)
        for message in page.messages:
            candidate = self._candidate(
                chat.id,
                chat.session_id,
                message.to_dict(),
            )
            if candidate is not None:
                records.append(candidate)
        if page.has_more and page.next_cursor:
            state["archive_cursor"] = page.next_cursor
            return OutboxPage(records, self._encode(state))
        result = self._next_chat(state)
        return OutboxPage(records, result.next_cursor)

    def _next_chat(self, state):
        if state["next_chat"] is None:
            return OutboxPage([], None)
        state.update(
            chat_cursor=state["next_chat"],
            chat_id=None,
            next_chat=None,
            phase="online",
            offset=0,
            archive_cursor=None,
        )
        return OutboxPage([], self._encode(state))

    def _candidate(self, chat_id, session_id, raw):
        from .projector import _validate_payload, _validate_protocol
        from .query import _evidence_message_payload

        if not isinstance(raw, dict) or not isinstance(
            raw.get("metadata"),
            dict,
        ):
            return None
        record = raw["metadata"].get("agent_observability_outbox")
        try:
            event = verify_event(record)
            encoded = json.dumps(
                event,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if (
                len(encoded.encode("utf-8")) > 24576
                or _validate_payload(encoded) != event
            ):
                return None
            _validate_protocol(event)
            if (
                event.get("event_type") != "evidence.linked"
                or event.get("saved_state") != "available"
            ):
                return None
        except (ValueError, TypeError, KeyError):
            return None
        if (
            event.get("chat_id") != chat_id
            or event.get("message_id") != raw.get("id")
            or event.get("reference_id")
            != raw["metadata"].get("agent_observability_evidence_ref")
        ):
            return None
        try:
            content = json.dumps(
                _evidence_message_payload(raw, session_id),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (KeyError, TypeError, ValueError):
            return None
        return {
            "chat_id": chat_id,
            "record": record,
            "content_sha256": hashlib.sha256(content).hexdigest(),
        }

    # Keep the quiescent traversal and its final validation in one operation.
    async def scan_offline_complete(  # pylint: disable=too-many-statements
        self,
        *,
        max_entries: int = 10000,
    ) -> list[dict]:
        """Enumerate and reverify a quiescent workspace or fail closed.

        The caller must stop all writers and recovery workers for the entire
        migration barrier. A returned list represents a completed traversal;
        every event must independently be acknowledged before rebinding.
        """
        from ..app.runner.api import _archive_store

        if type(max_entries) is not int or not 1 <= max_entries <= 100000:
            raise ValueError("invalid offline scan limit")
        archive = _archive_store(self.workspace)
        if archive is None:
            raise ValueError("archive unavailable")
        repo = getattr(self.workspace.chat_manager, "_repo", None)
        validate_directory = getattr(repo, "validate_recovery_directory", None)
        directory_snapshot = (
            await validate_directory()
            if validate_directory is not None
            else None
        )
        manager = (
            self._recovery_workspace.chat_manager
            if validate_directory is not None
            else self.workspace.chat_manager
        )
        records = []
        seen_chats = set()
        seen_refs = set()
        seen_event_ids = set()
        cursor = None
        count = 0
        bytes_seen = 0
        while True:
            page = await manager.list_chats_cursor(page_size=50, cursor=cursor)
            if not page.items and page.next_cursor is not None:
                raise ValueError("invalid chat pagination")
            for chat in page.items:
                if chat.id in seen_chats or len(seen_chats) >= max_entries:
                    raise ValueError("duplicate or excessive chats")
                seen_chats.add(chat.id)
                root = await self.workspace.runner.session.get_persisted_session_state_dict(
                    chat.session_id,
                    chat.user_id,
                )
                bytes_seen += len(
                    json.dumps(root, ensure_ascii=False).encode("utf-8"),
                )
                memory = root.get("agent", {}).get("memory", {})
                if not isinstance(memory, dict) or not isinstance(
                    memory.get("content", []),
                    list,
                ):
                    raise ValueError("invalid committed memory")
                entries = memory.get("content", [])
                try:
                    archived, archive_bytes = await asyncio.to_thread(
                        self._offline_archive,
                        archive,
                        chat.id,
                        max_entries - count,
                        64 * 1024 * 1024 - bytes_seen,
                    )
                except (OSError, KeyError, TypeError) as exc:
                    raise ValueError("invalid archive") from exc
                bytes_seen += archive_bytes
                count += len(entries) + len(archived)
                if count > max_entries or bytes_seen > 64 * 1024 * 1024:
                    raise ValueError("offline scan limit exceeded")
                for entry in entries:
                    if (
                        not isinstance(entry, (list, tuple))
                        or not entry
                        or not isinstance(entry[0], dict)
                    ):
                        raise ValueError("invalid committed memory entry")
                for raw in [entry[0] for entry in entries] + archived:
                    metadata = raw.get("metadata")
                    if not isinstance(metadata, dict):
                        continue
                    if not (
                        {
                            "agent_observability_outbox",
                            "agent_observability_evidence_ref",
                        }
                        & metadata.keys()
                    ):
                        continue
                    candidate = self._candidate(chat.id, chat.session_id, raw)
                    if candidate is None:
                        raise ValueError("invalid outbox occurrence")
                    event = verify_event(candidate["record"])
                    if (
                        event["reference_id"] in seen_refs
                        or event["event_id"] in seen_event_ids
                    ):
                        raise ValueError("duplicate outbox occurrence")
                    seen_refs.add(event["reference_id"])
                    seen_event_ids.add(event["event_id"])
                    records.append(candidate)
            if page.next_cursor is None:
                break
            if page.next_cursor == cursor:
                raise ValueError("invalid chat cursor")
            cursor = page.next_cursor
        events = []
        for record in records:
            event = await self.verified_event(record, strict=True)
            if event is None:
                raise ValueError("outbox occurrence failed revalidation")
            events.append(
                {"event": event, "content_sha256": record["content_sha256"]},
            )
        if (
            validate_directory is not None
            and await validate_directory() != directory_snapshot
        ):
            raise ValueError("Chat directory changed during offline scan")
        return events

    @staticmethod
    def _offline_archive(archive, chat_id, remaining_entries, remaining_bytes):
        canonical = archive._validate_chat_id(chat_id)
        with archive._chat_lock(canonical):
            if archive._tombstone_path(canonical).exists():
                raise ValueError("deleted archive")
            directory = archive.path_for(canonical)
            manifest = archive._read_manifest(directory / "manifest.json")
            boundaries = archive._visible_boundaries(manifest, canonical)
            if len(boundaries) != len(manifest["boundaries"]) or len(
                {b.id for b in boundaries},
            ) != len(boundaries):
                raise ValueError("invalid archive manifest")
            rows = []
            total = 0
            for boundary in boundaries:
                path = directory / f"{boundary.id}.jsonl"
                total += path.stat().st_size
                if (
                    total > remaining_bytes
                    or len(rows) + boundary.archived_message_count
                    > remaining_entries
                ):
                    raise ValueError("offline scan limit exceeded")
                messages = archive._read_batch(path)
                if len(messages) != boundary.archived_message_count or any(
                    m is None for m in messages
                ):
                    raise ValueError("invalid archive batch")
                rows.extend(message.to_dict() for message in messages)
            return rows, total

    async def verified_event(
        self,
        record: Any,
        *,
        strict=False,
    ) -> dict[str, Any] | None:
        from ..app.runner.api import _archive_store
        from .identity import same_observability_scope
        from .query import _read_pending_online

        try:
            event = verify_event(record["record"])
            workspace = self.workspace if strict else self._recovery_workspace
            chat = await workspace.chat_manager.get_chat(record["chat_id"])
            if chat is None or event.get("chat_id") != chat.id:
                return None
            scope = {
                k: getattr(self.scope, k)
                for k in (
                    "service_unit_id",
                    "source_id",
                    "tenant_id",
                    "agent_id",
                )
            }
            if not same_observability_scope(event.get("scope"), scope):
                return None
            ref = {
                key: event[key]
                for key in (
                    "chat_id",
                    "message_id",
                    "reference_id",
                    "content_type",
                )
            }
            ref["evidence_ref"] = "event:" + event["event_id"]
            ref["saved_state"] = "available"
            run = {
                "run_id": event["obs_run_id"],
                "chat_key": chat.id,
                "session_key": chat.session_id,
                **{
                    f"{k.removesuffix('_id')}_key": v for k, v in scope.items()
                },
            }
            # service_unit is named explicitly in the projection schema.
            run["service_unit_key"] = scope["service_unit_id"]
            request = SimpleNamespace(
                state=SimpleNamespace(source_id=scope["source_id"]),
            )
            result = await _read_pending_online(
                workspace,
                request,
                run,
                ref,
                event["obs_span_id"],
            )
            if result.get("status") not in {"available", "compacted"}:
                return None
            session = self.workspace.runner.session
            root = await session.get_persisted_session_state_dict(
                chat.session_id,
                chat.user_id,
            )
            entries = (
                root.get("agent", {}).get("memory", {}).get("content", [])
            )
            matches = [
                self._candidate(chat.id, chat.session_id, e[0])
                for e in entries
                if isinstance(e, (list, tuple)) and e
            ]
            matches = [m for m in matches if m == record]
            archive = _archive_store(self.workspace)
            found = await archive.read_evidence_occurrence(
                chat.id,
                ref["reference_id"],
                ref["message_id"],
                max_bytes=8 * 1024 * 1024,
            )
            if found.get("status") == "available":
                candidate = self._candidate(
                    chat.id,
                    chat.session_id,
                    found["message"].to_dict(),
                )
                if candidate == record:
                    matches.append(candidate)
            if len(matches) != 1:
                return None
            # Revalidate after retrieving the precise signed envelope.
            again = await _read_pending_online(
                workspace,
                request,
                run,
                ref,
                event["obs_span_id"],
            )
            if again != result:
                return None
            return event
        except (KeyError, TypeError, ValueError, AttributeError, OSError):
            return None
