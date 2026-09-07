# -*- coding: utf-8 -*-
# pylint: disable=too-many-branches,too-many-statements,unused-argument
# pylint: disable=too-many-public-methods,unnecessary-pass
"""
Base Channel: bound to AgentRequest/AgentResponse, unified by process.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC
from typing import (
    Optional,
    Dict,
    Any,
    List,
    Union,
    AsyncIterator,
    AsyncGenerator,
    Callable,
    TYPE_CHECKING,
)

from agentscope_runtime.engine.schemas.agent_schemas import (
    RunStatus,
    ContentType,
    TextContent,
    ImageContent,
    VideoContent,
    AudioContent,
    FileContent,
    RefusalContent,
    MessageType,
)

from .renderer import MessageRenderer, RenderStyle
from .schema import ChannelType
from ..tenant_context import bind_tenant_context
from ...config.llm_workload import LLM_WORKLOAD_CHAT, bind_llm_workload
from ...config.context import resolve_runtime_identity
from ...config.utils import load_config
from ..answer_turn.models import TurnIdentity

# Optional callback to enqueue payload (set by manager)
EnqueueCallback = Optional[Callable[[Any], None]]

# Called when a user-originated reply was sent (channel, user_id, session_id)
OnReplySent = Optional[Callable[[str, str, str], None]]

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agentscope_runtime.engine.schemas.agent_schemas import (
        AgentRequest,
        AgentResponse,
        Event,
    )

# process: accepts AgentRequest, streams Event
# (including message events with status completed)
ProcessHandler = Callable[[Any], AsyncIterator["Event"]]

# Outgoing part = runtime content types (no Dict[str, Any])
OutgoingContentPart = Union[
    TextContent,
    ImageContent,
    VideoContent,
    AudioContent,
    FileContent,
    RefusalContent,
]


class BaseChannel(ABC):
    """Base for all channels. Queue lives in ChannelManager; channel defines
    how to consume via consume_one().
    """

    channel: ChannelType

    # If True, manager creates a queue and consumer loop for this channel.
    uses_manager_queue: bool = True

    def __init__(
        self,
        process: ProcessHandler,
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
        dm_policy: str = "open",
        group_policy: str = "open",
        allow_from: Optional[list] = None,
        deny_message: str = "",
        require_mention: bool = False,
    ):
        self._process = process
        self._on_reply_sent = on_reply_sent
        self._show_tool_details = show_tool_details
        self._filter_tool_messages = filter_tool_messages
        self._filter_thinking = filter_thinking
        self.dm_policy = dm_policy or "open"
        self.group_policy = group_policy or "open"
        self.allow_from = set(allow_from or [])
        self.deny_message = deny_message or ""
        self.require_mention = require_mention
        self._enqueue: EnqueueCallback = None
        self._workspace = None
        cfg = load_config()
        internal_tools = frozenset(
            name
            for name, tc in cfg.tools.builtin_tools.items()
            if not tc.display_to_user
        )
        self._render_style = RenderStyle(
            show_tool_details=show_tool_details,
            filter_tool_messages=filter_tool_messages,
            filter_thinking=filter_thinking,
            internal_tools=internal_tools,
        )
        self._renderer = MessageRenderer(self._render_style)
        self._http: Optional[Any] = None
        # Debounce: content from messages that had no text; merged when text
        # arrives. Key = session_id.
        self._pending_content_by_session: Dict[str, List[Any]] = {}
        # Time debounce: merge native payloads within _debounce_seconds.
        # Set > 0 in subclass (e.g. 0.3). Key = get_debounce_key(payload).
        self._debounce_seconds: float = 0.0
        self._debounce_pending: Dict[str, List[Any]] = {}
        self._debounce_timers: Dict[str, asyncio.Task[None]] = {}

    def _is_native_payload(self, payload: Any) -> bool:
        """True if payload is a native dict that can be time-debounced."""
        return isinstance(payload, dict) and "content_parts" in payload

    def get_debounce_key(self, payload: Any) -> str:
        """
        Key for time debounce (same key = same conversation).
        Delegates to ``resolve_session_id`` so every channel gets
        session-scoped isolation automatically.
        """
        if isinstance(payload, dict):
            sender_id = payload.get("sender_id") or ""
            meta = payload.get("meta") or {}
            return payload.get("session_id") or self.resolve_session_id(
                sender_id,
                meta,
            )
        return getattr(payload, "session_id", "") or ""

    def merge_native_items(self, items: List[Any]) -> Any:
        """
        Merge multiple native payloads into one. Override for
        channel-specific merge (e.g. meta keys). Default: concat
        content_parts, merge meta (reply_future, reply_loop, etc.).
        """
        if not items:
            return None
        first = items[0] if isinstance(items[0], dict) else {}
        merged_parts: List[Any] = []
        merged_meta: Dict[str, Any] = dict(first.get("meta") or {})
        for it in items:
            p = it if isinstance(it, dict) else {}
            merged_parts.extend(p.get("content_parts") or [])
            m = p.get("meta") or {}
            for k in (
                "reply_future",
                "reply_loop",
                "incoming_message",
                "conversation_id",
                "message_id",
            ):
                if k in m:
                    merged_meta[k] = m[k]
        return {
            "channel_id": first.get("channel_id") or self.channel,
            "sender_id": first.get("sender_id") or "",
            "content_parts": merged_parts,
            "meta": merged_meta,
        }

    def merge_requests(self, requests: List[Any]) -> Any:
        """
        Merge multiple AgentRequest payloads (same session) into one.
        Used when manager drains same-session queue: concatenate
        input[0].content from all, keep first request's meta/session.
        Returns one request; None if requests empty.
        """
        if not requests:
            return None
        first = requests[0]
        if len(requests) == 1:
            return first
        all_contents: List[Any] = []
        for req in requests:
            inp = getattr(req, "input", None) or []
            if inp and hasattr(inp[0], "content"):
                all_contents.extend(getattr(inp[0], "content") or [])
        if not all_contents:
            return first
        msg = first.input[0]
        if hasattr(msg, "model_copy"):
            new_msg = msg.model_copy(update={"content": all_contents})
        else:
            new_msg = msg
            setattr(new_msg, "content", all_contents)
        if hasattr(first, "model_copy"):
            return first.model_copy(
                update={"input": [new_msg]},
            )
        first.input[0] = new_msg
        return first

    def _on_debounce_buffer_append(
        self,
        key: str,
        payload: Any,
        existing_items: List[Any],
    ) -> None:
        """
        Hook when appending to time-debounce buffer (existing_items
        non-empty). Override e.g. to unblock previous reply_future.
        """
        del key
        del payload
        del existing_items

    @staticmethod
    def _content_part_value(content: Any, key: str) -> Any:
        """兼容运行时 Content 对象和 Console 原始 JSON dict。"""
        if isinstance(content, dict):
            return content.get(key)
        return getattr(content, key, None)

    def _content_has_text(self, contents: List[Any]) -> bool:
        """True if contents has at least one TEXT or REFUSAL with non-empty."""
        if not contents:
            return False
        for c in contents:
            t = self._content_part_value(c, "type")
            if (
                t == ContentType.TEXT
                and (self._content_part_value(c, "text") or "").strip()
            ):
                return True
            if (
                t == ContentType.REFUSAL
                and (self._content_part_value(c, "refusal") or "").strip()
            ):
                return True
        return False

    def _content_has_audio(self, contents: List[Any]) -> bool:
        """True if contents has at least one AUDIO block."""
        return any(
            self._content_part_value(c, "type") == ContentType.AUDIO
            for c in (contents or [])
        )

    def _apply_no_text_debounce(
        self,
        session_id: str,
        content_parts: List[Any],
    ) -> tuple[bool, List[Any]]:
        """
        Debounce: if content has no text, buffer and return (False, []).
        If has text, return (True, merged) with any buffered content prepended.
        Audio-only messages bypass debounce and are processed immediately
        (voice messages are standalone user input, not partial uploads).
        """
        if not self._content_has_text(content_parts):
            if self._content_has_audio(content_parts):
                # Audio-only messages (e.g. voice messages) should be
                # processed immediately — they are complete user input.
                pending = self._pending_content_by_session.pop(
                    session_id,
                    [],
                )
                merged = pending + list(content_parts)
                return (True, merged)
            self._pending_content_by_session.setdefault(
                session_id,
                [],
            ).extend(content_parts)
            logger.debug(
                "channel debounce: no text, buffered session_id=%s",
                session_id[:24] if session_id else "",
            )
            return (False, [])
        pending = self._pending_content_by_session.pop(session_id, [])
        merged = pending + list(content_parts)
        return (True, merged)

    def _check_allowlist(
        self,
        sender_id: str,
        is_group: bool,
    ) -> tuple[bool, Optional[str]]:
        """Check sender against allowlist policy."""
        policy = self.group_policy if is_group else self.dm_policy
        if policy == "open":
            return True, None
        if sender_id in self.allow_from:
            return True, None
        if self.deny_message:
            return False, self.deny_message
        if is_group:
            return (
                False,
                "Sorry, this bot is only available to authorized users.",
            )
        return False, (
            "Sorry, you are not authorized to use this bot. "
            "Please contact the administrator to add your ID "
            f"to the allowlist. Your ID: {sender_id}"
        )

    def _check_group_mention(
        self,
        is_group: bool,
        meta: dict,
    ) -> bool:
        """Return True if message should be processed under mention policy."""
        if not is_group or not self.require_mention:
            return True
        return bool(
            meta.get("bot_mentioned") or meta.get("has_bot_command"),
        )

    def set_enqueue(self, cb: EnqueueCallback) -> None:
        """Set enqueue callback (called by ChannelManager)."""
        self._enqueue = cb

    def set_workspace(
        self,
        workspace,
        command_registry=None,
    ) -> None:
        """Set workspace reference for TaskTracker access.

        Args:
            workspace: Workspace instance with task_tracker and chat_manager
            command_registry: CommandRegistry for control command detection
        """
        self._workspace = workspace
        self._command_registry = command_registry

    def _extract_chat_name(self, payload: Any) -> str:
        """Extract chat name from payload for chat creation.

        Args:
            payload: Message payload (dict or AgentRequest)

        Returns:
            Chat name (truncated to 50 chars)
        """
        try:
            if isinstance(payload, dict):
                parts = payload.get("content_parts", [])
                if parts:
                    first = parts[0]
                    if isinstance(first, dict):
                        text = first.get("text", "")
                    elif hasattr(first, "text"):
                        text = first.text
                    else:
                        text = str(first)
                    if text:
                        return text[:50]
                return "New Chat"
            if hasattr(payload, "input") and payload.input:
                msg = payload.input[0]
                if hasattr(msg, "content") and msg.content:
                    content = msg.content[0]
                    if hasattr(content, "text"):
                        return content.text[:50]
            return "New Chat"
        except Exception as e:
            logger.warning(
                f"Failed to extract chat name from payload: {e}",
                exc_info=True,
            )
            return "New Chat"

    async def _consume_with_tracker(
        self,
        request: "AgentRequest",
        payload: Any,
    ) -> None:
        """Consume message with TaskTracker registration for cancellation.

        TaskTracker is used to track the running task so /stop can cancel it.
        Message serialization is ensured by UnifiedQueueManager which queues
        messages per (channel, session, priority).

        Args:
            request: AgentRequest
            payload: Original payload
        """
        session_id = getattr(request, "session_id", "") or ""
        user_id = getattr(request, "user_id", "") or ""
        channel_id = getattr(request, "channel", self.channel)

        chat = await self._workspace.chat_manager.get_or_create_chat(
            session_id,
            user_id,
            channel_id,
            name=self._extract_chat_name(payload),
        )

        # Inject session channel into channel_meta for downstream use
        # (e.g. session-end push needs to know the session's original channel)
        channel_meta = getattr(request, "channel_meta", None)
        if channel_meta is None:
            channel_meta = {}
            request.channel_meta = channel_meta
        channel_meta["chat_id"] = chat.id
        if isinstance(payload, dict):
            payload_meta = payload.setdefault("meta", {})
            if isinstance(payload_meta, dict):
                payload_meta["chat_id"] = chat.id
        if "session_channel" not in channel_meta:
            channel_meta["session_channel"] = chat.channel

        logger.info(
            f"_consume_with_tracker: chat_id={chat.id} "
            f"session={session_id[:30]}",
        )

        # Debug: verify runner.session is available for state persistence
        if self._workspace is not None and self._workspace.runner is not None:
            _sess = getattr(self._workspace.runner, "session", None)
            logger.info(
                "_consume_with_tracker: runner.session=%s save_dir=%s",
                type(_sess).__name__ if _sess else None,
                getattr(_sess, "save_dir", None) if _sess else None,
            )

        coordinator = self._workspace.answer_turn_coordinator
        if coordinator is None:
            raise RuntimeError("answer-turn coordinator is not configured")
        proposed_msgid = (
            channel_meta.get("msgid")
            if isinstance(channel_meta.get("msgid"), str)
            else None
        )
        lease = await coordinator.start_or_attach(
            chat.id,
            payload,
            self._stream_with_tracker,
            msgid=proposed_msgid,
        )

        if lease.is_new_run:
            try:
                async for _ in self._workspace.task_tracker.stream(
                    lease.identity,
                    lease.queue,
                ):
                    pass
            except asyncio.CancelledError:
                logger.info(
                    f"Task cancelled: chat_id={chat.id} "
                    f"session={session_id[:30]}",
                )
                raise
        else:
            logger.warning(
                f"Message ignored (task already running): "
                f"chat_id={chat.id} session={session_id[:30]}. "
                f"This should not happen with UnifiedQueueManager.",
            )

    def _prepare_stream_context(
        self,
        identity: TurnIdentity,
        payload: Any,
    ) -> tuple["AgentRequest", dict[str, Any], str]:
        """Normalize payload metadata before starting the process stream."""
        if isinstance(payload, dict):
            payload = {
                **payload,
                "meta": {
                    **(payload.get("meta") or {}),
                    "answer_turn_identity": identity,
                    "msgid": identity.msgid,
                },
            }
        else:
            meta = dict(getattr(payload, "channel_meta", None) or {})
            meta.update(answer_turn_identity=identity, msgid=identity.msgid)
            setattr(payload, "channel_meta", meta)
        request = self._payload_to_request(payload)
        if isinstance(payload, dict):
            send_meta = dict(payload.get("meta") or {})
            if payload.get("session_webhook"):
                send_meta["session_webhook"] = payload["session_webhook"]
        else:
            send_meta = getattr(request, "channel_meta", None) or {}
        bot_prefix = getattr(self, "bot_prefix", None) or getattr(
            self,
            "_bot_prefix",
            "",
        )
        if bot_prefix and "bot_prefix" not in send_meta:
            send_meta = {**send_meta, "bot_prefix": bot_prefix}
        return request, send_meta, self.get_to_handle_from_request(request)

    @staticmethod
    def _serialize_stream_event(event: Any) -> str:
        """Serialize one process event to an SSE data payload."""
        import json

        if hasattr(event, "model_dump_json"):
            return event.model_dump_json()
        if hasattr(event, "json"):
            return event.json()
        return json.dumps({"text": str(event)})

    async def _handle_stream_event(
        self,
        request: "AgentRequest",
        event: Any,
        *,
        to_handle: str,
        send_meta: dict[str, Any],
    ) -> Any:
        """Dispatch callbacks and return the latest response event."""
        obj = getattr(event, "object", None)
        status = getattr(event, "status", None)
        if obj == "message" and status == RunStatus.Completed:
            await self.on_event_message_completed(
                request,
                to_handle,
                event,
                send_meta,
            )
            return None
        if obj == "response":
            await self.on_event_response(request, event)
            return event
        return None

    async def _stream_with_tracker(
        self,
        identity: TurnIdentity,
        payload: Any,
    ) -> AsyncGenerator[str, None]:
        """Stream events through TaskTracker for task tracking.

        This method wraps _process and yields SSE-formatted events.
        Called by TaskTracker.attach_or_start to enable task cancellation.

        Args:
            payload: Message payload (dict or AgentRequest)

        Yields:
            SSE-formatted event strings
        """
        request, send_meta, to_handle = self._prepare_stream_context(
            identity,
            payload,
        )

        await self._before_consume_process(request)

        last_response = None
        process_iterator = None
        try:
            process_iterator = self._process(request)
            async for event in process_iterator:
                yield f"data: {self._serialize_stream_event(event)}\n\n"
                response = await self._handle_stream_event(
                    request,
                    event,
                    to_handle=to_handle,
                    send_meta=send_meta,
                )
                if response is not None:
                    last_response = response

            err_msg = self._get_response_error_message(last_response)
            if err_msg:
                await self._on_consume_error(
                    request,
                    to_handle,
                    f"Error: {err_msg}",
                )
            else:
                await self._on_process_completed(
                    request,
                    to_handle,
                    send_meta,
                )

            if self._on_reply_sent:
                args = self.get_on_reply_sent_args(request, to_handle)
                self._on_reply_sent(self.channel, *args)

        except asyncio.CancelledError:
            logger.info(
                f"channel task cancelled: "
                f"session={getattr(request, 'session_id', '')[:30]}",
            )
            if process_iterator is not None:
                await process_iterator.aclose()
            raise

        except Exception as e:
            logger.exception(
                f"channel _stream_with_tracker failed: {e}, "
                f"session={getattr(request, 'session_id', 'N/A')[:30]}, "
                f"agent={to_handle}",
            )
            await self._on_consume_error(
                request,
                to_handle,
                "Internal error",
            )
            raise

    @classmethod
    def from_env(
        cls,
        process: ProcessHandler,
        on_reply_sent: OnReplySent = None,
    ) -> "BaseChannel":
        raise NotImplementedError

    @classmethod
    def from_config(
        cls,
        process: ProcessHandler,
        config: Any,
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
    ) -> "BaseChannel":
        raise NotImplementedError

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Map sender and optional channel meta to session_id.
        Override in subclasses for channel-specific session keys
        (e.g. short suffix of conversation_id for cron lookup).
        """
        return f"{self.channel}:{sender_id}"

    def build_agent_request_from_user_content(
        self,
        channel_id: str,
        sender_id: str,
        session_id: str,
        content_parts: List[Any],
        channel_meta: Optional[Dict[str, Any]] = None,
        message_id: str | None = None,
    ) -> "AgentRequest":
        """
        Build AgentRequest from runtime content parts (Message content list).
        Use agentscope_runtime Message/Content types; no intermediate envelope.
        Subclasses call this after parsing native payload to content_parts.
        """
        from agentscope_runtime.engine.schemas.agent_schemas import (
            AgentRequest,
            Message,
            Role,
        )

        if not content_parts:
            content_parts = [
                TextContent(type=ContentType.TEXT, text=" "),
            ]
        msg = Message(
            type=MessageType.MESSAGE,
            role=Role.USER,
            content=content_parts,
        )
        if message_id:
            msg.id = message_id
        return AgentRequest(
            session_id=session_id,
            user_id=sender_id,
            input=[msg],
            channel=channel_id,
        )

    def build_agent_request_from_native(
        self,
        native_payload: Any,
    ) -> "AgentRequest":
        """
        Convert channel-native message payload to AgentRequest.
        Subclasses must implement: parse native -> content_parts (runtime
        Content types), session_id, then build_agent_request_from_user_content.
        Attach channel_meta to result for send path:
        request.channel_meta = meta.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement "
            "build_agent_request_from_native(native_payload)",
        )

    def _payload_to_request(self, payload: Any) -> "AgentRequest":
        """
        Convert queue payload to AgentRequest. Default: if payload looks like
        AgentRequest (has session_id, input), return it; else
        build_agent_request_from_native(payload). Override if needed.
        """
        if payload is None:
            raise ValueError("payload is None")
        if hasattr(payload, "session_id") and hasattr(payload, "input"):
            return payload
        return self.build_agent_request_from_native(payload)

    def get_to_handle_from_request(self, request: "AgentRequest") -> str:
        """
        Resolve send target (to_handle) from AgentRequest. Default: user_id.
        Override for channels that send by session_id (e.g. Feishu).
        """
        return getattr(request, "user_id", "") or ""

    def get_on_reply_sent_args(
        self,
        request: "AgentRequest",
        to_handle: str,
    ) -> tuple:
        """
        Args for _on_reply_sent(channel, *args). Default: (to_handle,
        session_id). Override e.g. to pass (user_id, session_id).
        """
        session_id = (
            getattr(request, "session_id", "") or f"{self.channel}:{to_handle}"
        )
        return (to_handle, session_id)

    async def refresh_webhook_or_token(self) -> None:
        """
        Optional: refresh webhook URL or API token. Override for channels
        that need periodic or on-401 refresh. Default no-op.
        """

    async def consume_one(self, payload: Any) -> None:
        """
        Process one payload from the manager-owned queue. If
        _debounce_seconds > 0 and payload is native (dict with
        content_parts), append to buffer and flush after delay;
        otherwise call _consume_one_request(payload). Messages
        with no text are buffered until text arrives (see
        _apply_no_text_debounce). Override only when you need
        a different flow (e.g. print).
        """
        if self._debounce_seconds > 0 and self._is_native_payload(payload):
            key = self.get_debounce_key(payload)
            if key in self._debounce_pending and self._debounce_pending[key]:
                self._on_debounce_buffer_append(
                    key,
                    payload,
                    self._debounce_pending[key],
                )
            self._debounce_pending.setdefault(key, []).append(payload)
            old = self._debounce_timers.pop(key, None)
            if old and not old.done():
                old.cancel()

            async def flush(k: str) -> None:
                await asyncio.sleep(self._debounce_seconds)
                items = self._debounce_pending.pop(k, [])
                self._debounce_timers.pop(k, None)
                if not items:
                    return
                merged = self.merge_native_items(items)
                if not merged:
                    return
                await self._consume_one_request(merged)

            self._debounce_timers[key] = asyncio.create_task(flush(key))
            return
        await self._consume_one_request(payload)

    def _extract_query_from_payload(self, payload: Any) -> str:
        """Extract query text from payload for command detection.

        Args:
            payload: Native dict or AgentRequest

        Returns:
            Query text string (empty if not found)
        """
        if isinstance(payload, dict):
            parts = payload.get("content_parts") or []
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text") or ""
                if hasattr(part, "type") and part.type == "text":
                    return getattr(part, "text", "") or ""
            return ""
        if hasattr(payload, "input"):
            inp = payload.input or []
            if inp and hasattr(inp[0], "content"):
                content = inp[0].content or []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            return part.get("text") or ""
                    elif hasattr(part, "type") and part.type == "text":
                        return getattr(part, "text", "") or ""
        return ""

    def _debounce_payload(self, payload: Any) -> bool:
        """Apply no-text debounce on payload; return False if buffered."""
        if isinstance(payload, dict):
            content_parts = payload.get("content_parts") or []
        elif hasattr(payload, "input") and payload.input:
            content_parts = getattr(payload.input[0], "content", None) or []
        else:
            return True

        if not content_parts:
            return True

        session_id = self.get_debounce_key(payload)
        should_process, merged = self._apply_no_text_debounce(
            session_id,
            content_parts,
        )
        if not should_process:
            return False

        # Write merged parts back so downstream paths see full content.
        if isinstance(payload, dict):
            payload["content_parts"] = merged
        elif hasattr(payload, "input") and payload.input:
            first = payload.input[0]
            if hasattr(first, "model_copy"):
                payload.input[0] = first.model_copy(
                    update={"content": merged},
                )
            elif hasattr(first, "content"):
                first.content = merged
        return True

    async def _consume_one_request(self, payload: Any) -> None:
        """
        Convert payload to request, apply no-text debounce, run _process,
        send messages, handle errors and on_reply_sent. Used by
        consume_one (direct or after time-debounce flush).

        If workspace is available, routes through TaskTracker for tracking.
        Control commands bypass TaskTracker for immediate response.
        """
        logger.debug(
            "base _consume_one_request: "
            f"has_workspace={self._workspace is not None}",
        )

        if not self._debounce_payload(payload):
            return

        if self._workspace is not None and self._command_registry is not None:
            query_text = self._extract_query_from_payload(payload)
            logger.debug(
                f"base _consume_one_request: query={query_text[:50]}",
            )
            is_control = self._command_registry.is_control_command(
                query_text,
            )
            logger.debug(
                f"base _consume_one_request: is_control={is_control}",
            )
            if not is_control:
                request = self._payload_to_request(payload)
                tenant_id, source_id, scope_id = self._resolve_bound_identity(
                    request,
                    payload,
                )
                with (
                    bind_tenant_context(
                        tenant_id=tenant_id,
                        user_id=getattr(request, "user_id", None),
                        workspace_dir=getattr(
                            self._workspace,
                            "workspace_dir",
                            None,
                        ),
                        source_id=source_id,
                        scope_id=scope_id,
                    ),
                    bind_llm_workload(LLM_WORKLOAD_CHAT),
                ):
                    await self._consume_with_tracker(request, payload)
                return

        request = self._payload_to_request(payload)
        tenant_id, source_id, scope_id = self._resolve_bound_identity(
            request,
            payload,
        )
        workspace_dir = getattr(self._workspace, "workspace_dir", None)
        # Build meta from payload so session_webhook is never lost when
        # request has no channel_meta (e.g. AgentRequest schema has no field).
        if isinstance(payload, dict):
            meta_from_payload = dict(payload.get("meta") or {})
            if payload.get("session_webhook"):
                meta_from_payload["session_webhook"] = payload[
                    "session_webhook"
                ]
            # Always attach so channel _before_consume_process can use it
            # (e.g. Feishu save receive_id for cron send).
            setattr(request, "channel_meta", meta_from_payload)
        to_handle = self.get_to_handle_from_request(request)
        with (
            bind_tenant_context(
                tenant_id=tenant_id,
                user_id=getattr(request, "user_id", None),
                workspace_dir=workspace_dir,
                source_id=source_id,
                scope_id=scope_id,
            ),
            bind_llm_workload(LLM_WORKLOAD_CHAT),
        ):
            await self._before_consume_process(request)
            # Prefer meta built from payload so session_webhook is present when
            # request.channel_meta is missing (AgentRequest may not have the attr).
            if isinstance(payload, dict):
                send_meta = dict(payload.get("meta") or {})
                if payload.get("session_webhook"):
                    send_meta["session_webhook"] = payload["session_webhook"]
            else:
                send_meta = getattr(request, "channel_meta", None) or {}
            bot_prefix = getattr(self, "bot_prefix", None) or getattr(
                self,
                "_bot_prefix",
                "",
            )
            if bot_prefix and "bot_prefix" not in send_meta:
                send_meta = {**send_meta, "bot_prefix": bot_prefix}
            logger.info(
                "base _consume_one_request: send_meta has_session_webhook=%s",
                bool((send_meta or {}).get("session_webhook")),
            )
            await self._run_process_loop(request, to_handle, send_meta)

    def _resolve_bound_identity(
        self,
        request: "AgentRequest",
        payload: Any | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """还原后台消费时需要绑定的完整 runtime 身份。"""
        runtime_tenant_id = getattr(self._workspace, "tenant_id", None)
        request_scope_id = getattr(request, "scope_id", None)
        channel_meta = getattr(request, "channel_meta", None) or {}
        payload_meta = (
            payload.get("meta", {}) if isinstance(payload, dict) else {}
        )
        scope_id = (
            request_scope_id
            or channel_meta.get("scope_id")
            or payload_meta.get("scope_id")
        )
        source_id = (
            getattr(request, "source_id", None)
            or channel_meta.get(
                "source_id",
            )
            or payload_meta.get("source_id")
        )
        if scope_id is not None:
            return resolve_runtime_identity(scope_id)
        logical_tenant_id, resolved_source_id, resolved_scope_id = (
            resolve_runtime_identity(runtime_tenant_id)
        )
        if resolved_scope_id is not None:
            return (
                logical_tenant_id,
                resolved_source_id,
                resolved_scope_id,
            )
        return resolve_runtime_identity(runtime_tenant_id, source_id)

    async def _run_process_loop(
        self,
        request: "AgentRequest",
        to_handle: str,
        send_meta: Dict[str, Any],
    ) -> None:
        """
        Run _process and send events. Override to use channel-specific
        loop (e.g. DingTalk _process_one_request with webhook sends).
        """
        last_response = None
        try:
            async for event in self._process(request):
                obj = getattr(event, "object", None)
                status = getattr(event, "status", None)
                if obj == "message" and status == RunStatus.Completed:
                    await self.on_event_message_completed(
                        request,
                        to_handle,
                        event,
                        send_meta,
                    )
                elif obj == "response":
                    last_response = event
                    await self.on_event_response(request, event)
            err_msg = self._get_response_error_message(last_response)
            if err_msg:
                await self._on_consume_error(
                    request,
                    to_handle,
                    f"Error: {err_msg}",
                )
            else:
                await self._on_process_completed(
                    request,
                    to_handle,
                    send_meta,
                )
            if self._on_reply_sent:
                args = self.get_on_reply_sent_args(request, to_handle)
                self._on_reply_sent(self.channel, *args)
        except Exception:
            logger.exception("channel consume_one failed")
            await self._on_consume_error(
                request,
                to_handle,
                "An error occurred while processing your request.",
            )

    def _get_response_error_message(self, last_response: Any) -> Optional[str]:
        """
        Extract error message from runtime response event.
        Handles AgentResponse.error or Event wrapper (e.g. .data / .response).
        """
        if not last_response:
            return None
        resp = last_response
        if getattr(last_response, "data", None) is not None:
            resp = last_response.data
        elif getattr(last_response, "response", None) is not None:
            resp = last_response.response
        err = getattr(resp, "error", None)
        if not err:
            return None
        if hasattr(err, "message"):
            return getattr(err, "message", None) or str(err)
        if isinstance(err, dict):
            return err.get("message") or str(err)
        return str(err)

    async def _before_consume_process(self, request: "AgentRequest") -> None:
        """
        Hook called once per consume_one before running _process. Override
        to e.g. save receive_id for send path (Feishu).
        """

    async def on_event_message_completed(
        self,
        request: "AgentRequest",
        to_handle: str,
        event: Any,
        send_meta: Dict[str, Any],
    ) -> None:
        """
        Hook: one message event completed. Default: send_message_content.
        Override for batch/debounce (e.g. DingTalk merge then send).
        """
        await self.send_message_content(to_handle, event, send_meta)

    async def on_event_response(
        self,
        request: "AgentRequest",
        event: Any,
    ) -> None:
        """Hook: response event received. Default: no-op."""

    async def _on_process_completed(
        self,
        request: "AgentRequest",
        to_handle: str,
        send_meta: Dict[str, Any],
    ) -> None:
        """Hook called after all events processed without error.

        Override for post-processing (e.g. Feishu DONE reaction).
        """
        await self._try_session_end_push(request, to_handle)

    def _should_session_end_push(self, request=None) -> bool:
        """Check if zhaohu session-end push is enabled.

        Push is skipped when the message originates from the zhaohu channel
        itself (upstream callback) because the reply is already sent there.
        When the message comes from another channel (e.g. console) targeting
        a zhaohu session, push is enabled if the config flag is on.
        """
        if self.channel == "zhaohu":
            logger.info("session-end push skipped: self.channel is zhaohu")
            return False
        if self._workspace is None:
            logger.info("session-end push skipped: no workspace")
            return False
        zhaohu_cfg = getattr(
            self._workspace._config.channels,
            "zhaohu",
            None,
        )
        if zhaohu_cfg is None:
            logger.info("session-end push skipped: no zhaohu config")
            return False
        enabled = bool(getattr(zhaohu_cfg, "session_end_push_enabled", False))
        if not enabled:
            logger.info(
                "session-end push skipped: session_end_push_enabled=False",
            )
        return enabled

    async def _try_session_end_push(
        self,
        request: "AgentRequest",
        to_handle: str,
        reply_text: str = "",
    ) -> None:
        """If session-end push is enabled, send notification via zhaohu.

        When session_end_push_link_prefix is configured, an extra jump link
        is attached (using session_id or chat_id as configured).
        Push rule by source_id:
        - "ruice": only push when a link (file link or jump link) exists
        - other sources: push without link requirement
        """
        if not self._should_session_end_push(request):
            return
        try:
            zhaohu_ch = await self._get_zhaohu_channel()
            if zhaohu_ch is None:
                return

            zhaohu_cfg = self._get_zhaohu_config()
            source_id = self._get_push_source_id(request)
            session_channel = self._get_session_channel(request)
            logger.info(
                "session-end push check: channel=%s session_channel=%s source_id=%s",
                self.channel,
                session_channel,
                source_id,
            )

            text = self._build_session_end_push_text(request)
            meta = await self._build_session_end_push_meta(
                zhaohu_cfg,
                request,
                reply_text,
            )
            # if self._should_skip_session_end_push(source_id, meta):
            #     return
            await zhaohu_ch.send(to_handle, text, meta or None)
            logger.info("session-end push sent via zhaohu to %s", to_handle)
        except Exception:
            logger.exception("session-end push failed for %s", to_handle)

    async def _get_zhaohu_channel(self) -> Any:
        """Return the zhaohu channel instance, or None if unavailable."""
        cm = self._workspace.channel_manager
        if cm is None:
            return None
        zhaohu_ch = await cm.get_channel("zhaohu")
        if zhaohu_ch is None:
            logger.info("session-end push skipped: zhaohu channel not found")
        return zhaohu_ch

    def _get_zhaohu_config(self) -> Any:
        """Return the workspace zhaohu channel config (may be None)."""
        return getattr(self._workspace._config.channels, "zhaohu", None)

    def _get_push_source_id(self, request: "AgentRequest") -> str:
        """Get the session's original source_id from request or channel_meta."""
        channel_meta = getattr(request, "channel_meta", None) or {}
        return (
            getattr(
                request,
                "source_id",
                None,
            )
            or channel_meta.get("source_id")
            or ""
        )

    def _build_session_end_push_text(self, request: "AgentRequest") -> str:
        """Build the session-end push notification text from user query."""
        query = self._extract_user_query(request)
        if len(query) > 30:
            prefix = query[:30]
            return f"【{prefix}...】任务已完成请及时查看"
        return (
            f"【{query}】任务已完成请及时查看"
            if query
            else "任务已完成请及时查看"
        )

    async def _build_session_end_push_meta(
        self,
        zhaohu_cfg: Any,
        request: "AgentRequest",
        reply_text: str,
    ) -> dict:
        """Build push meta: file links and optional jump link."""
        meta = {}
        links = self._extract_file_links(reply_text)
        if links:
            meta["link_items"] = [
                {"url": url, "text": name} for name, url in links
            ]
        link_url = await self._build_session_end_push_link(
            zhaohu_cfg,
            request,
        )
        if link_url:
            meta["link_url"] = link_url
            meta["link_text"] = "点击查看详情"
        return meta

    async def _build_session_end_push_link(
        self,
        zhaohu_cfg: Any,
        request: "AgentRequest",
    ) -> str:
        """Build jump link from zhaohu config; empty when prefix is unset."""
        if not zhaohu_cfg or not getattr(
            zhaohu_cfg,
            "session_end_push_link_prefix",
            "",
        ):
            return ""
        prefix = zhaohu_cfg.session_end_push_link_prefix
        sep = "&" if "?" in prefix else "?"
        id_type = (
            getattr(zhaohu_cfg, "session_end_push_link_id_type", "")
            or "session_id"
        )
        session_id = getattr(request, "session_id", "") or ""
        if id_type == "chat_id":
            chat_id = await self._resolve_chat_id(request)
            if chat_id:
                return f"{prefix}{sep}chatId={chat_id}"
            if session_id:
                logger.info(
                    "session-end push: chat_id unavailable, fallback sessionId",
                )
                return f"{prefix}{sep}sessionId={session_id}"
        if session_id:
            return f"{prefix}{sep}sessionId={session_id}"
        return ""

    # def _should_skip_session_end_push(
    #     self,
    #     source_id: str,
    #     meta: dict,
    # ) -> bool:
    #     """Skip push when ruice source has no file or jump link."""
    #     has_link = bool(meta.get("link_items") or meta.get("link_url"))
    #     if source_id == "ruice" and not has_link:
    #         logger.info("session-end push skipped: ruice source and no links")
    #         return True
    #     return False

    async def _resolve_chat_id(self, request: "AgentRequest") -> str:
        """Get chat_id from workspace chat_manager.

        Returns empty string on failure so the caller can fall back
        to session_id, without aborting the whole push.
        """
        try:
            session_id = getattr(request, "session_id", "") or ""
            user_id = getattr(request, "user_id", "") or ""
            channel_id = getattr(request, "channel", self.channel)
            chat = await self._workspace.chat_manager.get_or_create_chat(
                session_id,
                user_id,
                channel_id,
            )
            return getattr(chat, "id", "") or ""
        except Exception:
            logger.info("session-end push: get_or_create_chat failed")
            return ""

    def _get_session_channel(self, request: "AgentRequest") -> str:
        """Get the session's original channel from channel_meta."""
        channel_meta = getattr(request, "channel_meta", None) or {}
        return channel_meta.get("session_channel", "")

    def _extract_file_links(self, text: str) -> list[tuple[str, str]]:
        """Extract file links and names from reply text by matching FILE_URL prefix.

        URL format: {FILE_URL}/static/{scope_id}/{agent_id}/{filename}
        e.g. http://localhost:8088/static/ODAyNDU2MDQ.Uk1BU1NJU1Q/default/报告.pdf
        """
        import os
        import re

        base = os.getenv("FILE_URL", "http://localhost:8088").rstrip("/")
        pattern = (
            re.escape(base)
            + r"/static/[^/\s]+/[^/\s]+/[^/\s]+\.[a-zA-Z]{1,10}"
        )
        matches = re.findall(pattern, text)
        seen = set()
        result = []
        for url in matches:
            if url in seen:
                continue
            seen.add(url)
            name = url.rsplit("/", 1)[-1] if "/" in url else ""
            result.append((name, url))
        return result

    def _extract_user_query(self, request: "AgentRequest") -> str:
        """Extract user's query text from request."""
        user_input = getattr(request, "input", None) or []
        for item in user_input:
            for part in getattr(item, "content", []) or []:
                if getattr(part, "type", None) == ContentType.TEXT:
                    text = getattr(part, "text", None)
                    if text:
                        return text.strip()
        return ""

    async def _on_consume_error(
        self,
        request: Any,
        to_handle: str,
        err_text: str,
    ) -> None:
        """
        Called when consume_one hits an error or response.error. Default:
        send err_text via send_content_parts. Override to send via channel
        API (e.g. imessage _send_sync).
        """
        await self.send_content_parts(
            to_handle,
            [TextContent(type=ContentType.TEXT, text=err_text)],
            getattr(request, "channel_meta", None) or {},
        )

    async def send_response(
        self,
        to_handle: str,
        response: "AgentResponse",
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Convert AgentResponse to this channel's reply and send.
        Default: take last message text from output and call
        send(to_handle, text, meta).
        Subclasses may override to support image, video attachments.
        """
        text = self._response_to_text(response)
        await self.send(to_handle, text or "", meta)

    def _message_to_content_parts(
        self,
        message: Any,
    ) -> List[OutgoingContentPart]:
        """
        Convert a Message (object=='message') into sendable parts.
        Delegates to self._renderer; override _renderer or _render_style
        for channel-specific formatting.
        """
        return self._renderer.message_to_parts(message)

    async def send_message_content(
        self,
        to_handle: str,
        message: Any,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Send all content of a Message
        (text, image, video, audio, file, refusal).
        Subclasses may override send_content_parts for channel-specific
        multi-part sending.
        """
        parts = self._message_to_content_parts(message)
        if not parts:
            logger.debug(
                f"channel send_message_content: no parts for to_handle="
                f"{to_handle}, skip send",
            )
            return
        logger.debug(
            f"channel send_message_content: to_handle={to_handle} "
            f"parts_count={len(parts)} "
            f"part_types={[getattr(p, 'type', None) for p in parts]}",
        )
        await self.send_content_parts(to_handle, parts, meta)

    async def send_content_parts(
        self,
        to_handle: str,
        parts: List[OutgoingContentPart],
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Send a list of content parts.
        Default: merge text/refusal into one text, append media URLs as
        fallback, send one message; optionally call send_media for each
        media part if overridden.
        """
        text_parts: List[str] = []
        media_parts: List[OutgoingContentPart] = []
        for p in parts:
            t = getattr(p, "type", None)
            if t == ContentType.TEXT and getattr(p, "text", None):
                text_parts.append(p.text or "")
            elif t == ContentType.REFUSAL and getattr(p, "refusal", None):
                text_parts.append(p.refusal or "")
            elif t in (
                ContentType.IMAGE,
                ContentType.VIDEO,
                ContentType.AUDIO,
                ContentType.FILE,
            ):
                media_parts.append(p)
        body = "\n".join(text_parts) if text_parts else ""
        prefix = (meta or {}).get("bot_prefix", "") or ""
        if prefix and body:
            body = prefix + "  " + body
        for m in media_parts:
            t = getattr(m, "type", None)
            if t == ContentType.IMAGE and getattr(m, "image_url", None):
                body += f"\n[Image: {m.image_url}]"
            elif t == ContentType.VIDEO and getattr(m, "video_url", None):
                body += f"\n[Video: {m.video_url}]"
            elif t == ContentType.FILE and (
                getattr(m, "file_url", None) or getattr(m, "file_id", None)
            ):
                body += f"\n[File: {m.file_url or m.file_id}]"
            elif t == ContentType.AUDIO and getattr(m, "data", None):
                body += "\n[Audio]"
        if body.strip():
            logger.debug(
                f"channel send_content_parts: to_handle={to_handle} "
                f"body_len={len(body)} preview="
                f"{body[:120] + '...' if len(body) > 120 else body}",
            )
            await self.send(to_handle, body.strip(), meta)
        for m in media_parts:
            await self.send_media(to_handle, m, meta)

    async def send_media(
        self,
        to_handle: str,
        part: OutgoingContentPart,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Send a single media part (image, video, audio, file).
        Default: no-op (already appended to text in send_content_parts).
        Subclasses override to send real attachments.
        """
        pass

    def _response_to_text(self, response: "AgentResponse") -> str:
        """Extract reply text from AgentResponse (last message in output)."""
        if not response.output:
            return ""
        last_msg = response.output[-1]
        if last_msg.type != MessageType.MESSAGE or not last_msg.content:
            return ""
        parts = []
        for c in last_msg.content:
            if getattr(c, "type", None) == ContentType.TEXT and getattr(
                c,
                "text",
                None,
            ):
                parts.append(c.text)
            elif getattr(c, "type", None) == ContentType.REFUSAL and getattr(
                c,
                "refusal",
                None,
            ):
                parts.append(c.refusal)
        return "".join(parts)

    def clone(self, config) -> "BaseChannel":
        """Clone a new channel instance with updated config, cloning
        process and on_reply_sent from self.

        Subclasses must implement from_config(process, config, on_reply_sent).

        show_tool_details is global config (not in channel config), so we
        preserve from self. filter_tool_messages and filter_thinking are
        per-channel config, so we read from new config.
        """
        return self.__class__.from_config(
            process=self._process,
            config=config,
            on_reply_sent=self._on_reply_sent,
            show_tool_details=getattr(self, "_show_tool_details", True),
            filter_tool_messages=getattr(
                config,
                "filter_tool_messages",
                False,
            ),
            filter_thinking=getattr(
                config,
                "filter_thinking",
                False,
            ),
        )

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def send(
        self,
        to_handle: str,
        text: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Subclass implements: send one text
        (and optional attachments) to to_handle.
        """
        raise NotImplementedError

    def to_handle_from_target(self, *, user_id: str, session_id: str) -> str:
        """Map cron dispatch target to channel-specific to_handle.

        Default: use user_id. For many channels, this is enough.
        Discord proactive send relies on meta['channel_id'] or
         meta['user_id'] anyway.
        """
        return user_id

    async def send_event(
        self,
        *,
        user_id: str,
        session_id: str,
        event: "Event",
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send a runner Event to this channel (non-stream).

        We only send when event is a completed message, then reuse
        send_message_content().
        """
        # Delay import to avoid hard dependency at module import time

        obj = getattr(event, "object", None)
        status = getattr(event, "status", None)

        if obj != "message" or status != RunStatus.Completed:
            return

        to_handle = self.to_handle_from_target(
            user_id=user_id,
            session_id=session_id,
        )
        await self.send_message_content(to_handle, event, meta)
