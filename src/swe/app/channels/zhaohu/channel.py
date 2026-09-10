# -*- coding: utf-8 -*-
"""Built-in Zhaohu channel.

Supports both outbound push and inbound message handling via callback.
"""

from __future__ import annotations

import base64
import json
import re
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Union
from urllib.parse import quote as url_quote

import asyncio
import ssl

import httpx
from agentscope_runtime.engine.schemas.agent_schemas import (
    ContentType,
    MessageType,
    TextContent,
)

from ....config.config import ZhaohuConfig as ZhaohuChannelConfig
from ..base import BaseChannel, OnReplySent, ProcessHandler
from ..renderer import MessageRenderer, RenderStyle

logger = logging.getLogger(__name__)

_TEXT_PART_LIMIT = 200
_SUMMARY_LIMIT = 50
_DEFAULT_CHANNEL = "ZH"
_DEFAULT_NET = "DMZ"
_DEFAULT_TIMEOUT = 15.0
_APPROVAL_TEXT_LIMIT = 800
_APPROVAL_INPUT_LIMIT = 600
_DELIVERY_UNAVAILABLE_MESSAGE = "zhaohu delivery unavailable"

# Message dedup: keep processed IDs for 5 minutes
_DEDUP_TTL_SECONDS = 300


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _normalize_text(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _chunk_text_values(text: str, limit: int = _TEXT_PART_LIMIT) -> list[str]:
    """Split text into text values under Zhaohu's 200-char limit."""
    normalized = _normalize_text(text)
    if not normalized:
        return [""]

    chunks: list[str] = []
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if line:
            chunks.append(line)

    return chunks or [_truncate(normalized, limit)]


def _clean_payload(obj: Any) -> Any:
    """Remove None and empty-string values from nested payloads."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            cleaned = _clean_payload(value)
            if (
                cleaned is None
                or cleaned == ""
                or cleaned == []
                or cleaned == {}
            ):
                continue
            out[key] = cleaned
        return out
    if isinstance(obj, list):
        out = []
        for item in obj:
            cleaned = _clean_payload(item)
            if cleaned is None or cleaned == {}:
                continue
            out.append(cleaned)
        return out
    return obj


def _raise_cron_delivery_failure(meta: Optional[dict], detail: str) -> None:
    if isinstance(meta, dict) and meta.get("cron_delivery_key"):
        raise RuntimeError(f"{_DELIVERY_UNAVAILABLE_MESSAGE}: {detail}")


def _build_approval_result_text(
    *,
    request_id: str,
    session_id: str,
    user_id: str,
    tool_name: str,
    decision: str,
    source_channel: str,
) -> str:
    """构建不含按钮的工具审批结果通知正文。"""
    decision_text = "通过" if decision == "approved" else "拒绝"
    source_channel_text = "招乎" if source_channel == "zhaohu" else "平台"
    lines = [
        f"{tool_name or 'unknown'}工具审批已{decision_text}",
        f"审批ID：{request_id}",
        f"用户：{user_id or '-'}",
    ]
    if source_channel:
        lines.append(f"审批来源：{source_channel_text}")
    return "\n".join(lines)


class ZhaohuChannel(BaseChannel):
    """Official built-in Zhaohu channel (outbound push + inbound callback)."""

    channel = "zhaohu"
    display_name = "Zhaohu"

    def __init__(
        self,
        process: ProcessHandler,
        enabled: bool,
        push_url: str,
        sys_id: str,
        robot_open_id: str,
        channel_code: str,
        net: str,
        request_timeout: float,
        bot_prefix: str,
        user_query_url: str = "",
        extract_url: str = "",
        oauth_url: str = "",
        client_id: str = "",
        client_secret: str = "",
        custom_card_url: str = "",
        cron_task_menu_id: str = "",
        cron_task_error_page: str = "",
        cron_task_sys_id: str = "",
        intent_url: str = "",
        intent_open_id: str = "",
        intent_api_key: str = "",
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
        dm_policy: str = "open",
        group_policy: str = "open",
        allow_from: Optional[list] = None,
        deny_message: str = "",
    ):
        super().__init__(
            process,
            on_reply_sent=on_reply_sent,
            show_tool_details=show_tool_details,
            filter_tool_messages=filter_tool_messages,
            filter_thinking=filter_thinking,
            dm_policy=dm_policy,
            group_policy=group_policy,
            allow_from=allow_from,
            deny_message=deny_message,
        )

        self.enabled = enabled
        self.push_url = push_url or ""
        self.sys_id = sys_id or ""
        self.robot_open_id = robot_open_id or ""
        self.channel_code = channel_code or _DEFAULT_CHANNEL
        self.net = net or _DEFAULT_NET
        self.request_timeout = max(float(request_timeout or 0), 1.0)
        self.bot_prefix = bot_prefix or ""
        self.user_query_url = user_query_url or ""
        self.extract_url = extract_url or ""
        self.oauth_url = oauth_url or ""
        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self.custom_card_url = custom_card_url or ""
        self.cron_task_menu_id = cron_task_menu_id or ""
        self.cron_task_error_page = cron_task_error_page or ""
        self.cron_task_sys_id = cron_task_sys_id or ""
        self.intent_url = intent_url or ""
        self.intent_open_id = intent_open_id or ""
        self.intent_api_key = intent_api_key or ""

        # Message dedup: set of processed message IDs with timestamp
        self._processed_message_ids: Dict[str, float] = {}
        self._dedup_lock = threading.Lock()

        # Per-user route lock for /new session switching
        self._user_route_locks: Dict[str, asyncio.Lock] = {}
        self._route_lock_guard = asyncio.Lock()

        # 简洁模式（filter_thinking=True，前端"显示思考过程"禁用）下，
        # 按接收方缓冲最后一条完成消息，流程结束时只推送最终结果
        # （思考/工具等中间消息只在 Console 展示）。
        self._zhaohu_pending: Dict[str, Any] = {}
        # 详细模式（filter_thinking=False，前端"显示思考过程"启用）下
        # 思考内容也要推送，用不受过滤的独立渲染器
        # （MessageRenderer 无状态，可安全共享）。
        self._verbose_renderer = MessageRenderer(
            RenderStyle(
                show_tool_details=self._show_tool_details,
                filter_tool_messages=self._filter_tool_messages,
                filter_thinking=False,
                internal_tools=self._render_style.internal_tools,
            ),
        )

        # OAuth token cache: token string and creation timestamp
        self._oauth_token: Optional[str] = None
        self._oauth_token_created_at: Optional[float] = None
        # Token validity: 90 minutes (5400 seconds)
        self._token_validity_seconds = 90 * 60

    @classmethod
    def from_env(
        cls,
        process: ProcessHandler,
        on_reply_sent: OnReplySent = None,
    ) -> "ZhaohuChannel":
        allow_from_env = os.getenv("ZHAOHU_ALLOW_FROM", "")
        allow_from = (
            [s.strip() for s in allow_from_env.split(",") if s.strip()]
            if allow_from_env
            else []
        )
        return cls(
            process=process,
            enabled=os.getenv("ZHAOHU_CHANNEL_ENABLED", "0") == "1",
            push_url=os.getenv("ZHAOHU_PUSH_URL", ""),
            sys_id=os.getenv("ZHAOHU_SYS_ID", ""),
            robot_open_id=os.getenv("ZHAOHU_ROBOT_OPEN_ID", ""),
            channel_code=os.getenv("ZHAOHU_CHANNEL", _DEFAULT_CHANNEL),
            net=os.getenv("ZHAOHU_NET", _DEFAULT_NET),
            request_timeout=float(
                os.getenv("ZHAOHU_REQUEST_TIMEOUT", str(_DEFAULT_TIMEOUT)),
            ),
            bot_prefix=os.getenv("ZHAOHU_BOT_PREFIX", ""),
            user_query_url=os.getenv("ZHAOHU_USER_QUERY_URL", ""),
            extract_url=os.getenv("ZHAOHU_EXTRACT_URL", ""),
            oauth_url=os.getenv("ZHAOHU_OAUTH_URL", ""),
            client_id=os.getenv("ZHAOHU_CLIENT_ID", ""),
            client_secret=os.getenv("ZHAOHU_CLIENT_SECRET", ""),
            custom_card_url=os.getenv("ZHAOHU_CUSTOM_CARD_URL", ""),
            cron_task_menu_id=os.getenv("ZHAOHU_CRON_TASK_MENU_ID", ""),
            cron_task_error_page=os.getenv("ZHAOHU_CRON_TASK_ERROR_PAGE", ""),
            cron_task_sys_id=os.getenv("ZHAOHU_CRON_TASK_SYS_ID", ""),
            intent_url=os.getenv("ZHAOHU_INTENT_URL", ""),
            intent_open_id=os.getenv("ZHAOHU_INTENT_OPEN_ID", ""),
            intent_api_key=os.getenv("ZHAOHU_INTENT_API_KEY", ""),
            on_reply_sent=on_reply_sent,
            dm_policy=os.getenv("ZHAOHU_DM_POLICY", "open"),
            group_policy=os.getenv("ZHAOHU_GROUP_POLICY", "open"),
            allow_from=allow_from,
            deny_message=os.getenv("ZHAOHU_DENY_MESSAGE", ""),
        )

    @classmethod
    def from_config(
        cls,
        process: ProcessHandler,
        config: Union[ZhaohuChannelConfig, dict],
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
    ) -> "ZhaohuChannel":
        c = config if isinstance(config, dict) else config.model_dump()

        def _get_str(key: str) -> str:
            return (c.get(key) or "").strip()

        return cls(
            process=process,
            enabled=bool(c.get("enabled", False)),
            push_url=_get_str("push_url"),
            sys_id=_get_str("sys_id"),
            robot_open_id=_get_str("robot_open_id"),
            channel_code=_get_str("channel") or _DEFAULT_CHANNEL,
            net=_get_str("net") or _DEFAULT_NET,
            request_timeout=float(
                c.get("request_timeout") or _DEFAULT_TIMEOUT,
            ),
            bot_prefix=_get_str("bot_prefix"),
            user_query_url=_get_str("user_query_url"),
            extract_url=_get_str("extract_url"),
            oauth_url=_get_str("oauth_url"),
            client_id=_get_str("client_id"),
            client_secret=_get_str("client_secret"),
            custom_card_url=_get_str("custom_card_url"),
            cron_task_menu_id=_get_str("cron_task_menu_id"),
            cron_task_error_page=_get_str("cron_task_error_page"),
            cron_task_sys_id=_get_str("cron_task_sys_id"),
            intent_url=_get_str("intent_url"),
            intent_open_id=_get_str("intent_open_id"),
            intent_api_key=_get_str("intent_api_key"),
            on_reply_sent=on_reply_sent,
            show_tool_details=show_tool_details,
            filter_tool_messages=filter_tool_messages,
            filter_thinking=filter_thinking,
            dm_policy=c.get("dm_policy") or "open",
            group_policy=c.get("group_policy") or "open",
            allow_from=c.get("allow_from") or [],
            deny_message=c.get("deny_message") or "",
        )

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate session_id for zhaohu callback messages.

        Session ID format: `zhaohu:callback:{sapId}`
        This ensures:
        - Same user (same sapId) has the same session for conversation continuity
        - Different from frontend sessions (which use UUID)
        - Isolated from outbound push sessions

        Args:
            sender_id: User's sapId from user query
            channel_meta: Optional channel metadata

        Returns:
            Session ID string
        """
        return f"zhaohu:callback:{sender_id}"

    async def _get_user_route_lock(self, sap_id: str) -> asyncio.Lock:
        """Get per-user route lock for serializing /new pointer switches."""
        async with self._route_lock_guard:
            lock = self._user_route_locks.get(sap_id)
            if lock is None:
                lock = asyncio.Lock()
                self._user_route_locks[sap_id] = lock
            return lock

    async def _get_active_session_id(self, sap_id: str) -> str:
        """Get the user's current active session_id.

        Returns the active pointer if set and the corresponding ChatSpec
        still exists.  If the ChatSpec was deleted, clears the stale
        pointer and falls back to the legacy format.
        """
        if (
            self._workspace is None
            or self._workspace.runner is None
            or self._workspace.runner.session is None
        ):
            return self.resolve_session_id(sap_id)

        meta_session_id = f"zhaohu:active:{sap_id}"
        try:
            state = (
                await self._workspace.runner.session.get_session_state_dict(
                    session_id=meta_session_id,
                    user_id=sap_id,
                    allow_not_exist=True,
                )
            )
            active_sid = state.get("active_session_id")
            if active_sid:
                # Verify the ChatSpec still exists
                if self._workspace.chat_manager is not None:
                    existing = (
                        await self._workspace.chat_manager.get_chat_by_session(
                            active_sid,
                            self.channel,
                            sap_id,
                        )
                    )
                    if existing is not None:
                        logger.info(
                            "zhaohu active session: sapId=%s -> session_id=%s",
                            sap_id,
                            active_sid,
                        )
                        return active_sid

                    # ChatSpec deleted — clear stale pointer
                    logger.warning(
                        "zhaohu active session not found in chat_manager, "
                        "clearing pointer: sapId=%s session_id=%s "
                        "channel=%s user_id=%s",
                        sap_id,
                        active_sid,
                        self.channel,
                        sap_id,
                    )
                    await self._set_active_session_id(sap_id, "")
                else:
                    return active_sid
        except Exception:
            logger.warning(
                "zhaohu _get_active_session_id: failed for sapId=%s",
                sap_id,
                exc_info=True,
            )

        default_sid = self.resolve_session_id(sap_id)
        logger.info(
            "zhaohu no active pointer, using default: sapId=%s -> session_id=%s",
            sap_id,
            default_sid,
        )
        return default_sid

    async def _set_active_session_id(
        self,
        sap_id: str,
        session_id: str,
    ) -> None:
        """Set the user's current active session_id pointer."""
        if (
            self._workspace is None
            or self._workspace.runner is None
            or self._workspace.runner.session is None
        ):
            logger.warning(
                "zhaohu _set_active_session_id: workspace/session not available",
            )
            return

        meta_session_id = f"zhaohu:active:{sap_id}"
        try:
            await self._workspace.runner.session.update_session_state(
                session_id=meta_session_id,
                key="active_session_id",
                value=session_id,
                user_id=sap_id,
            )
            logger.info(
                "zhaohu set active session: sapId=%s -> session_id=%s",
                sap_id,
                session_id,
            )
        except Exception:
            logger.warning(
                "zhaohu _set_active_session_id: failed for sapId=%s",
                sap_id,
                exc_info=True,
            )

    def get_to_handle_from_request(self, request: Any) -> str:
        """Get the send target from AgentRequest.

        For Zhaohu, we need to send to yst_id (not sap_id/user_id).
        The yst_id is stored in channel_meta['send_addr'].

        Args:
            request: AgentRequest with channel_meta

        Returns:
            yst_id for sending, or user_id as fallback
        """
        channel_meta = getattr(request, "channel_meta", None) or {}
        send_addr = channel_meta.get("send_addr")
        if send_addr:
            return send_addr
        # Fallback to user_id (sapId) if send_addr not in meta
        return getattr(request, "user_id", "") or ""

    def get_on_reply_sent_args(
        self,
        request: Any,
        to_handle: str,
    ) -> tuple:
        """Args for _on_reply_sent(channel, *args).

        Override to pass (user_id, session_id) for Zhaohu tracking.
        """
        session_id = getattr(request, "session_id", "") or ""
        user_id = getattr(request, "user_id", "") or ""
        return (user_id, session_id)

    def build_agent_request_from_native(self, native_payload: Any) -> Any:
        """Convert native callback payload to AgentRequest."""

        payload = native_payload if isinstance(native_payload, dict) else {}
        channel_id = payload.get("channel_id") or self.channel
        sender_id = payload.get("sender_id") or ""
        meta = payload.get("meta") or {}
        session_id = payload.get("session_id") or self.resolve_session_id(
            sender_id,
            meta,
        )
        content_parts = payload.get("content_parts") or []

        if not content_parts:
            content_parts = [
                TextContent(
                    type=ContentType.TEXT,
                    text=payload.get("text", ""),
                ),
            ]

        request = self.build_agent_request_from_user_content(
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
            content_parts=content_parts,
            channel_meta=meta,
        )
        request.channel_meta = meta
        return request

    def try_accept_message(self, msg_id: str) -> bool:
        """Check if message ID is new; return True if accepted (not duplicate).

        Maintains a time-based dedup cache that prunes entries older than TTL.
        """

        if not msg_id:
            return True

        now = time.time()
        with self._dedup_lock:
            # Prune old entries
            expired = [
                mid
                for mid, ts in self._processed_message_ids.items()
                if now - ts > _DEDUP_TTL_SECONDS
            ]
            for mid in expired:
                del self._processed_message_ids[mid]

            if msg_id in self._processed_message_ids:
                return False

            self._processed_message_ids[msg_id] = now
            return True

    async def _get_oauth_token(self) -> Optional[str]:
        """Get OAuth token for Zhaohu API authentication.

        Token is cached in memory with creation timestamp.
        If token exists and is valid (less than 90 minutes old), return cached token.
        If token is expired or missing, fetch new token via /oauth/token endpoint.

        Returns:
            OAuth token string, or None if fetch fails.
        """
        if not self.oauth_url or not self.client_id or not self.client_secret:
            logger.warning(
                "zhaohu oauth skipped: oauth_url, client_id, or client_secret not configured",
            )
            return None

        now = time.time()

        # Check if cached token is still valid
        if (
            self._oauth_token
            and self._oauth_token_created_at
            and (
                now - self._oauth_token_created_at
                < self._token_validity_seconds
            )
        ):
            logger.debug(
                "zhaohu oauth: using cached token (age=%.0f seconds)",
                now - self._oauth_token_created_at,
            )
            return self._oauth_token

        # Token expired or missing, fetch new token
        logger.info(
            "zhaohu oauth: fetching new token from %s",
            self.oauth_url,
        )

        form_data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        timeout = httpx.Timeout(self.request_timeout, connect=10.0)
        context = ssl.create_default_context()
        context.options |= 0x4

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                verify=context,
            ) as client:
                response = await client.post(
                    self.oauth_url,
                    data=form_data,  # form-data format
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                response.raise_for_status()
                data = response.json()

            access_token = data.get("access_token")
            if not access_token:
                logger.warning(
                    "zhaohu oauth: no access_token in response: %s",
                    data,
                )
                return None

            # Cache the token
            self._oauth_token = access_token
            self._oauth_token_created_at = now

            logger.info(
                "zhaohu oauth: new token obtained, expires_in=%s seconds",
                data.get("expires_in"),
            )
            return access_token

        except Exception:
            logger.exception(
                "zhaohu oauth: failed to fetch token from %s",
                self.oauth_url,
            )
            return None

    async def send_custom_card(
        self,
        to_id: str,
        content: list,
    ) -> tuple[int, str]:
        """Send custom card message to Zhaohu user.

        First obtains OAuth token (cached or fresh), then sends card via
        /robot-service/single-message/custom-card endpoint.

        Args:
            to_id: Receiver's Zhaohu OpenID (user's openId)
            content: Card content as JSONArray (structure TBD by caller)

        Returns:
            tuple (code, msg):
                - code: 0 for success, non-zero for failure
                - msg: Message ID on success, error description on failure
        """
        if not self.custom_card_url:
            logger.warning(
                "zhaohu send_custom_card skipped: custom_card_url not configured",
            )
            return (-1, "custom_card_url not configured")

        if not to_id:
            logger.warning("zhaohu send_custom_card skipped: to_id is empty")
            return (-1, "to_id is empty")

        if not content:
            logger.warning("zhaohu send_custom_card skipped: content is empty")
            return (-1, "content is empty")

        # Get OAuth token
        token = await self._get_oauth_token()
        if not token:
            logger.warning(
                "zhaohu send_custom_card failed: unable to get OAuth token",
            )
            return (-1, "unable to get OAuth token")

        # Build request payload
        payload = {
            "fromId": self.robot_open_id,
            "toId": to_id,
            "content": content,
        }

        headers = {
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
        }

        timeout = httpx.Timeout(self.request_timeout, connect=10.0)
        context = ssl.create_default_context()
        context.options |= 0x4

        logger.info(
            "zhaohu send_custom_card: sending to toId=%s, content_count=%d",
            to_id,
            len(content),
        )

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                verify=context,
            ) as client:
                response = await client.post(
                    self.custom_card_url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

            code = data.get("code", -1)
            msg = data.get("msg", "")

            if code == 0:
                logger.info(
                    "zhaohu send_custom_card: success, msgId=%s",
                    msg,
                )
            else:
                logger.warning(
                    "zhaohu send_custom_card: failed, code=%d msg=%s",
                    code,
                    msg,
                )

            return (code, msg)

        except Exception:
            logger.exception(
                "zhaohu send_custom_card: request failed to %s",
                self.custom_card_url,
            )
            return (-1, "request failed")

    def _format_approval_tool_input(self, tool_input: object) -> str:
        """把工具参数格式化为可读文本，避免通知正文无限增长。"""
        if not tool_input:
            return ""
        try:
            text = json.dumps(
                tool_input,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except Exception:
            text = str(tool_input)
        return _truncate(_normalize_text(text), _APPROVAL_INPUT_LIMIT)

    def _build_approval_pending_text(
        self,
        *,
        request_id: str,
        session_id: str,
        user_id: str,
        tool_name: str,
        result_summary: str,
        findings_count: object,
        tool_input: object,
    ) -> str:
        """构建不含审批按钮的工具审批待处理通知正文。"""
        lines = [
            f"{tool_name or 'unknown'}工具调用需要审批",
        ]
        params_text = self._format_approval_tool_input(tool_input)
        if params_text:
            lines.extend(["调用参数：", params_text])
        lines.extend(
            [
                f"审批ID：{request_id}",
                f"用户：{user_id or '-'}",
            ],
        )
        return "\n".join(lines)

    async def send_cron_approval_card(
        self,
        *,
        request_id: str,
        session_id: str,
        user_id: str,
        tool_name: str,
        agent_id: str = "",
        tenant_id: str = "",
        source_id: str = "",
        result_summary: str = "",
        findings_count: object = 0,
        tool_input: object = None,
        **_: object,
    ) -> tuple[int, str]:
        """发送工具审批待处理通知，本阶段只发正文信息，不发按钮。"""
        if not user_id:
            logger.warning(
                "zhaohu approval pending notification skipped: user_id is empty",
            )
            return (-1, "user_id is empty")

        text = self._build_approval_pending_text(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            tool_name=tool_name,
            result_summary=result_summary,
            findings_count=findings_count,
            tool_input=tool_input,
        )
        content = self._build_approval_task_initiated_card(
            text,
            agent_id=agent_id,
            request_id=request_id,
            tenant_id=tenant_id,
            source_id=source_id,
        )
        open_id = await self.deal_eight_sap_to_open(user_id)
        await self.send_custom_card(
            open_id,
            content,
        )
        return (0, "sent")

    @staticmethod
    def _build_zhclient_url(tag: dict) -> str:
        """Build zhclient:// URL for approval button actionLink.

        Format: zhclient:///?actionCode=9&actionParams=UrlEncode(Base64(params))
        Encoding: UTF-8
        """
        params = json.dumps({"tag": tag}, ensure_ascii=False)
        encoded = url_quote(
            base64.b64encode(params.encode("utf-8")).decode("utf-8"),
        )
        return f"zhclient:///?actionCode=9&actionParams={encoded}"

    def _build_approval_task_initiated_card(
        self,
        task_content: str,
        agent_id: str = "",
        request_id: str = "",
        tenant_id: str = "",
        source_id: str = "",
    ) -> list:
        """Build card content for task initiated notification (Template 1).

        Used when user message length > 10 (task assignment).

        Args:
            task_content: The original task content from user message

        Returns:
            Card content array for send_custom_card
        """
        # Template 1: Task initiated notification
        approval_tag = {
            "agent_id": agent_id,
            "agentId": agent_id,
            "tenant_id": tenant_id,
            "source_id": source_id,
            "request_id": request_id,
        }
        approve_url = self._build_zhclient_url(
            {**approval_tag, "type": "approve"},
        )
        reject_url = self._build_zhclient_url(
            {**approval_tag, "type": "reject"},
        )
        card_content = [
            {
                "type": "content",
                "list": [
                    {
                        "type": [0],
                        "content": f"{task_content}",
                        "style": 5,
                    },
                ],
            },
            {
                "type": "operate",
                "arrange": 0,
                "list": [
                    {
                        "content": "通过",
                        "style": 1,
                        "action": 3,
                        "tag": "approve",
                        "disable": 0,
                        "actionLink": {
                            "url": approve_url,
                            "pcUrl": approve_url,
                            "mobileUrl": approve_url,
                        },
                    },
                    {
                        "content": "拒绝",
                        "style": 0,
                        "action": 3,
                        "tag": "reject",
                        "disable": 0,
                        "actionLink": {
                            "url": reject_url,
                            "pcUrl": reject_url,
                            "mobileUrl": reject_url,
                        },
                    },
                ],
            },
        ]

        return card_content

    async def deal_eight_sap_to_open(self, send_addr):
        if send_addr and len(send_addr) == 8:
            logger.info(
                "zhaohu _build_push_payload: send_addr is 8, querying user info for sapId=%s",
                send_addr,
            )
            user_info = await self._query_user_info_by_sap(send_addr)
            if user_info and user_info.get("openId"):
                open_id = user_info.get("openId")
                logger.info(
                    "zhaohu _build_push_payload: sapId=%s -> openId=%s",
                    send_addr,
                    open_id,
                )
                send_addr = open_id
            else:
                logger.warning(
                    "zhaohu _build_push_payload: failed to get openId for sapId=%s",
                    send_addr,
                )
        return send_addr

    async def send_cron_approval_result(
        self,
        *,
        request_id: str,
        session_id: str,
        user_id: str,
        tool_name: str = "",
        decision: str,
        source_channel: str = "",
        **_: object,
    ) -> tuple[int, str]:
        """发送工具审批结果通知，本阶段只发正文信息，不发按钮。"""
        if not user_id:
            logger.warning(
                "zhaohu approval result notification skipped: user_id is empty",
            )
            return (-1, "user_id is empty")

        text = _build_approval_result_text(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            tool_name=tool_name,
            decision=decision,
            source_channel=source_channel,
        )
        await self.send(
            user_id,
            text,
            {
                "session_id": session_id,
                "notification_summary": "工具审批结果",
            },
        )
        return (0, "sent")

    def _build_claw_url(
        self,
        chat_id: str,
    ) -> str:
        """Build claw URL for card navigation.

        Generates a URL following the formula:
        param = { errorPage, to: menuId, type: "toMenu",
                  queryParam: {sessionId, origin: 'Y'} }
        pcParams = encodeURIComponent(btoa(JSON.stringify(param)))
        pcParams2 = encodeURIComponent(btoa('pcParams='+pcParams))
        URL = CMBMobileOA:///?pcSysId=${sys_id}&pcParams={pcParams2}

        Args:
            chat_id: Chat ID for queryParam (sessionId key)

        Returns:
            Generated claw URL string
        """
        if not self.cron_task_menu_id or not self.cron_task_sys_id:
            logger.warning(
                "zhaohu _build_claw_url: missing cron_task_menu_id or "
                "cron_task_sys_id, returning empty URL",
            )
            return ""

        # Always use sessionId as key, chat_id as value
        query_param = {"sessionId": chat_id, "origin": "Y"}

        param = {
            "errorPage": self.cron_task_error_page,
            "to": self.cron_task_menu_id,
            "type": "toMenu",
            "queryParam": query_param,
        }

        # Encode: pcParams = encodeURIComponent(btoa(JSON.stringify(param)))
        param_json = json.dumps(param, separators=(",", ":"))
        pc_params = url_quote(base64.b64encode(param_json.encode()).decode())

        # Encode: pcParams2 = encodeURIComponent(btoa('pcParams='+pcParams))
        pc_params_str = f"pcParams={pc_params}"
        pc_params2 = url_quote(
            base64.b64encode(pc_params_str.encode()).decode(),
        )
        pc_web_config = "eyJuYW1lIjoi6LSi5a%2BMVysiLCJ5c3RBdXRoIjoidHJ1ZSJ9"

        # Build final URL
        url = (
            f"CMBMobileOA:///?pcSysId={self.cron_task_sys_id}"
            f"&pcWebConfig={pc_web_config}"
            f"&pcParams={pc_params2}"
        )

        return url

    def _build_task_initiated_card(
        self,
        task_content: str,
        chat_id: str,
        include_link: bool = True,
    ) -> list:
        """Build card content for task initiated notification (Template 1).

        Used when user message length > 10 (task assignment).

        Args:
            task_content: The original task content from user message
            chat_id: Chat ID for generating claw URL
            include_link: 是否在卡片中包含跳转链接

        Returns:
            Card content array for send_custom_card
        """
        # Template 1: Task initiated notification
        card_content = [
            {
                "type": "content",
                "list": [
                    {
                        "content": f"任务【{task_content}】已发起，任务处理完成我还会通知你的",
                        "style": 5,
                    },
                ],
            },
        ]

        if include_link:
            claw_url = self._build_claw_url(chat_id)
            card_content.append(
                {
                    "type": "content",
                    "list": [
                        {
                            "type": [3],
                            "content": "点击跳转小助claw版查看",
                            "style": 1,
                            "action": 1,
                            "link": {
                                "pcUrl": claw_url,
                            },
                        },
                    ],
                },
            )

        return card_content

    def _build_task_progress_card(
        self,
        tasks: list,
        include_link: bool = True,
    ) -> list:
        """Build card content for task progress query (Template 2).

        Used when user queries task progress with keywords.

        Args:
            tasks: List of task info dicts, each containing:
                - task_name: Task name/description
                - status: Task status ("completed", "in_progress", "pending")
                - status_text: Status display text (e.g., "已完成", "进行中", "待开始")
                - time_info: Time information (e.g., "已于8:30执行完成")
                - task_chat_id: Chat ID for navigation (from task meta)

        Returns:
            Card content array for send_custom_card
        """
        total_tasks = len(tasks)

        card_content: list[dict[str, object]] = []
        # Title section
        card_content.append(
            {
                "type": "title",
                "content": f"今日任务进度({total_tasks})",
            },
        )

        # Task containers
        for task in tasks:
            task_name = task.get("task_name", "未知任务")
            status = task.get("status", "pending")
            status_text = task.get("status_text", "待开始")
            time_info = task.get("time_info", "")
            task_chat_id = task.get("task_chat_id", "")

            # Determine style and backgroundColor based on status
            if status == "completed":
                style = 3
                background_color = 1
            elif status == "in_progress":
                style = 1
                background_color = 2
            else:  # pending
                style = 4
                background_color = 3

            # Build task container list
            task_list = [
                # Task name row
                {
                    "type": "content",
                    "list": [
                        {
                            "content": task_name,
                            "style": 5,
                        },
                    ],
                },
                # Status and time row
                {
                    "type": "content",
                    "list": [
                        {
                            "content": f"  {status_text}  ",
                            "style": style,
                            "backgroundColor": background_color,
                            "fontSize": 1,
                        },
                        {
                            "content": f"  {time_info}",
                            "style": 5,
                            "fontSize": 1,
                        },
                    ],
                },
            ]

            # Operation row (view result button) - only for completed tasks with chat_id
            if status == "completed" and task_chat_id and include_link:
                # Generate claw URL using task_chat_id
                result_url = self._build_claw_url(task_chat_id)
                task_list.append(
                    {
                        "type": "operate",
                        "list": [
                            {
                                "content": "查看结果",
                                "style": 1,
                                "action": 1,
                                "disable": 0,
                                "link": {
                                    "pcUrl": result_url,
                                },
                            },
                        ],
                    },
                )

            task_container = {
                "type": "container",
                "style": 0,
                "list": task_list,
            }
            card_content.append(task_container)

        # If no tasks, add empty container message
        if total_tasks == 0:
            card_content.append(
                {
                    "type": "container",
                    "style": 0,
                    "list": [
                        {
                            "type": "content",
                            "list": [
                                {
                                    "content": "暂无任务",
                                    "style": 5,
                                },
                            ],
                        },
                    ],
                },
            )

        return card_content

    async def _should_include_link(self, tenant_id: str) -> bool:
        """判断当前租户的通知卡片是否应包含跳转链接。

        仅 RMASSIST 来源的租户需要跳转链接。

        Args:
            tenant_id: 租户标识（sap_id）。

        Returns:
            如果应包含链接返回 True。
        """
        try:
            from ...workspace.tenant_init_source_store import is_tenant_source

            return await is_tenant_source(tenant_id, "RMASSIST")
        except Exception:
            logger.warning(
                "zhaohu _should_include_link: failed to check source for %s",
                tenant_id,
            )
            return False

    async def _query_user_info(self, open_id: str) -> Optional[Dict[str, Any]]:
        """Query user info to convert openId to sapId.

        Returns user info dict with sapId, or None if query fails.
        """

        if not self.user_query_url:
            logger.warning(
                "zhaohu user query skipped: user_query_url not configured",
            )
            return None

        if not open_id:
            return None

        query_payload = {
            "compareType": "EQ",
            "matchFields": ["openId"],
            "keyWord": open_id,
        }
        timeout = httpx.Timeout(self.request_timeout, connect=10.0)
        # 自定义SSL上下文
        context = ssl.create_default_context()
        context.options |= 0x4

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                verify=context,
            ) as client:
                response = await client.post(
                    self.user_query_url,
                    json=query_payload,
                )
                response.raise_for_status()
                data = response.json()

            if data.get("code") != "200":
                logger.warning(
                    "zhaohu user query failed: code=%s message=%s",
                    data.get("code"),
                    data.get("message"),
                )
                return None

            user_list = data.get("data") or []
            if not user_list or not isinstance(user_list, list):
                logger.warning(
                    "zhaohu user query: no user found for openId=%s",
                    open_id,
                )
                return None

            user_info = user_list[0]
            logger.info(
                "zhaohu user query: openId=%s -> sapId=%s",
                open_id,
                user_info.get("sapId"),
            )
            return user_info

        except Exception:
            logger.exception("zhaohu user query failed for openId=%s", open_id)
            return None

    async def _query_user_info_by_sap(
        self,
        sap_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Query user info to convert openId to sapId.

        Returns user info dict with sapId, or None if query fails.
        """

        if not self.user_query_url:
            logger.warning(
                "zhaohu user query skipped: user_query_url not configured",
            )
            return None

        if not sap_id:
            return None

        query_payload = {
            "compareType": "EQ",
            "matchFields": ["sapId"],
            "keyWord": sap_id,
        }
        timeout = httpx.Timeout(self.request_timeout, connect=10.0)
        # 自定义SSL上下文
        context = ssl.create_default_context()
        context.options |= 0x4

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                verify=context,
            ) as client:
                response = await client.post(
                    self.user_query_url,
                    json=query_payload,
                )
                response.raise_for_status()
                data = response.json()

            if data.get("code") != "200":
                logger.warning(
                    "zhaohu user query failed: code=%s message=%s",
                    data.get("code"),
                    data.get("message"),
                )
                return None

            user_list = data.get("data") or []
            if not user_list or not isinstance(user_list, list):
                logger.warning(
                    "zhaohu user query: no user found for openId=%s",
                    sap_id,
                )
                return None

            user_info = user_list[0]
            logger.info(
                "zhaohu user query: openId=%s -> sapId=%s",
                sap_id,
                user_info.get("sapId"),
            )
            return user_info

        except Exception:
            logger.exception("zhaohu user query failed for openId=%s", sap_id)
            return None

    async def enqueue_callback_message(self, callback_body: Any) -> None:
        """Process callback message and enqueue for agent processing.

        Called by the router when a message is received from Zhaohu.
        """
        if not self._enqueue:
            logger.warning(
                "zhaohu callback: enqueue not set, message dropped",
            )
            return

        msg_id = getattr(callback_body, "msg_id", "") or ""
        from_id = getattr(callback_body, "from_id", "") or ""
        to_id = getattr(callback_body, "to_id", "") or ""
        group_id = getattr(callback_body, "group_id", None)
        group_name = getattr(callback_body, "group_name", None)
        msg_type = getattr(callback_body, "msg_type", "") or ""
        msg_content = getattr(callback_body, "msg_content", "") or ""
        timestamp = getattr(callback_body, "timestamp", 0)
        # Query user info to get sapId from openId
        user_info = await self._query_user_info(from_id)
        sap_id = (user_info or {}).get("sapId") or from_id
        user_name = (user_info or {}).get("userName") or ""
        is_group = group_id is not None

        # Build meta for send path
        meta: Dict[str, Any] = {
            "send_addr": sap_id,
            "open_id": from_id,
            "to_id": to_id,
            "group_id": group_id,
            "group_name": group_name,
            "msg_type": msg_type,
            "timestamp": timestamp,
            "is_group": is_group,
            "source_id": self.channel,
        }
        if user_name:
            meta["user_name"] = user_name

        # Build content parts
        content_parts = [
            TextContent(type=ContentType.TEXT, text=msg_content),
        ]
        # Build native payload for queue
        native = {
            "channel_id": self.channel,
            "sender_id": sap_id,
            "content_parts": content_parts,
            "meta": meta,
            "message_id": msg_id,
        }
        logger.info(
            "zhaohu enqueue: msgId=%s fromId=%s sapId=%s text=%s",
            msg_id,
            from_id,
            sap_id,
            msg_content[:100] if msg_content else "",
        )

        self._enqueue(native)

    # Task progress query keywords
    _TASK_PROGRESS_KEYWORDS = frozenset(
        [
            "我的任务进度",
            "任务进度",
            "查看任务进度",
        ],
    )

    async def _query_task_progress(
        self,
        user_id: str,
        open_id: str,
    ) -> bool:
        """Query scheduled task progress for the user and send card (Template 2).

        Queries all tasks for the user on today's date from CronManager.

        Args:
            user_id: User's sapId
            open_id: User's openId (for sending message)

        Returns:
            True if card sent successfully, False otherwise
        """
        from datetime import datetime, timezone

        logger.info(
            "zhaohu _query_task_progress: querying for user_id=%s",
            user_id,
        )

        # Get today's date in UTC (scheduler uses UTC timezone)
        today = datetime.now(timezone.utc)

        # Query tasks from CronManager
        tasks: list = []
        logger.info(
            "zhaohu _query_task_progress: workspace=%s, cron_manager=%s, "
            "workspace_dir=%s, user_id=%s",
            self._workspace is not None,
            self._workspace.cron_manager if self._workspace else None,
            self._workspace.workspace_dir if self._workspace else None,
            user_id,
        )
        if self._workspace and self._workspace.cron_manager:
            try:
                raw_tasks = await self._workspace.cron_manager.query_user_tasks_by_date(
                    user_id,
                    today,
                )
                # Convert to format expected by _build_task_progress_card
                for raw_task in raw_tasks:
                    task_meta = raw_task.get("meta") or {}
                    tasks.append(
                        {
                            "task_name": raw_task.get("task_name", "未知任务"),
                            "status": raw_task.get("status", "pending"),
                            "status_text": raw_task.get(
                                "status_text",
                                "待开始",
                            ),
                            "time_info": raw_task.get("time_info", ""),
                            "task_chat_id": task_meta.get("task_chat_id", ""),
                            "job_id": raw_task.get("job_id", ""),
                        },
                    )
                logger.info(
                    "zhaohu _query_task_progress: found %d tasks for user_id=%s",
                    len(tasks),
                    user_id,
                )
            except Exception:
                logger.exception(
                    "zhaohu _query_task_progress: failed to query tasks for user_id=%s",
                    user_id,
                )

        # Build and send card using Template 2 (task progress card)
        include_link = await self._should_include_link(user_id)
        card_content = self._build_task_progress_card(
            tasks,
            include_link=include_link,
        )
        code, msg = await self.send_custom_card(open_id, card_content)

        if code == 0:
            logger.info(
                "zhaohu _query_task_progress: card sent successfully, msgId=%s",
                msg,
            )
            return True
        else:
            logger.warning(
                "zhaohu _query_task_progress: card send failed, code=%d msg=%s",
                code,
                msg,
            )
            return False

    async def _run_task_llm_async(
        self,
        request: Any,
        session_id: str,
        task_content: str,
    ) -> None:
        """Run LLM task asynchronously in background.

        This method runs as a background task after the card is sent to user.
        The LLM processes the task content via Runner.stream_query, which
        automatically saves the session state to file after completion.

        Args:
            request: AgentRequest for the task session
            session_id: Unique task session ID
            task_content: Task content/description
        """
        from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

        logger.info(
            "zhaohu _run_task_llm_async: starting for sessionId=%s, task_len=%d",
            session_id,
            len(task_content),
        )

        response_text = ""
        try:
            # self._process calls Runner.stream_query, which:
            # 1. Loads session state
            # 2. Processes the request through LLM
            # 3. Saves session state to file in finally block
            async for event in self._process(request):
                obj = getattr(event, "object", None)
                status = getattr(event, "status", None)
                if obj == "message" and status == RunStatus.Completed:
                    # Extract text from the completed message
                    parts = self._message_to_content_parts(event)
                    for part in parts:
                        if hasattr(part, "text") and part.text:
                            response_text += part.text
                        elif hasattr(part, "refusal") and part.refusal:
                            response_text += part.refusal

            if response_text:
                logger.info(
                    "zhaohu _run_task_llm_async: completed for sessionId=%s, "
                    "response_len=%d, session saved",
                    session_id,
                    len(response_text),
                )
                # Notification placeholder: send result to user when needed
            else:
                logger.warning(
                    "zhaohu _run_task_llm_async: no response for sessionId=%s",
                    session_id,
                )

        except Exception:
            logger.exception(
                "zhaohu _run_task_llm_async: failed for sessionId=%s",
                session_id,
            )

    async def _create_task_chat(
        self,
        session_id: str,
        user_id: str,
        task_content: str,
    ) -> str:
        """Create chat for task and return chat_id."""
        chat_id = session_id  # Default fallback
        if self._workspace and self._workspace.chat_manager:
            try:
                chat = await self._workspace.chat_manager.get_or_create_chat(
                    session_id,
                    user_id,
                    self.channel,
                    name=task_content[:50] if task_content else "New Chat",
                )
                chat_id = chat.id
                logger.info(
                    "zhaohu _create_task_chat: created chat, "
                    "chat_id=%s, session_id=%s",
                    chat_id,
                    session_id,
                )
            except Exception:
                logger.warning(
                    "zhaohu _create_task_chat: failed to create chat, "
                    "using session_id as fallback",
                )
        return chat_id

    async def _send_task_result(
        self,
        session_id: str,
        response_text: str,
        meta: Dict[str, Any],
    ) -> None:
        """Send task result to user via push_url."""
        yst_id = meta.get("send_addr", "")
        if not yst_id:
            return
        if response_text:
            await self.send(yst_id, response_text, meta)
            logger.info(
                "zhaohu _send_task_result: result sent to yst_id=%s",
                yst_id,
            )
        else:
            await self.send(
                yst_id,
                "抱歉，处理您的任务时发生错误，请稍后重试。",
                meta,
            )
            logger.warning(
                "zhaohu _send_task_result: sent error notification for session_id=%s",
                session_id,
            )

    async def _collect_response_from_events(
        self,
        request: Any,
    ) -> str:
        """Run LLM and collect complete result from events."""
        from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

        response_text = ""
        async for event in self._process(request):
            obj = getattr(event, "object", None)
            status = getattr(event, "status", None)
            if obj == "message" and status == RunStatus.Completed:
                parts = self._message_to_content_parts(event)
                for part in parts:
                    if hasattr(part, "text") and part.text:
                        response_text += part.text
                    elif hasattr(part, "refusal") and part.refusal:
                        response_text += part.refusal
        return response_text

    async def _run_task_llm_and_notify(
        self,
        request: Any,
        session_id: str,
        task_content: str,
        from_id: str,
        meta: Dict[str, Any],
        user_id: str,
    ) -> None:
        """Run LLM task and send final result to user.

        This method:
        1. Creates Chat first to get chat.id for navigation
        2. Sends task initiated card notification to user immediately
        3. Runs LLM to get complete result (no streaming)
        4. Sends final result to user when complete

        Args:
            request: AgentRequest for the task session
            session_id: Unique task session ID
            task_content: Task content/description
            from_id: User's openId (for sending messages)
            meta: Channel metadata
            user_id: User's sapId for chat creation
        """
        # Step 0: Create Chat first to get chat.id for navigation
        chat_id = await self._create_task_chat(
            session_id,
            user_id,
            task_content,
        )

        # Step 1: Send card notification immediately
        include_link = await self._should_include_link(user_id)
        card_content = self._build_task_initiated_card(
            task_content,
            chat_id,
            include_link=include_link,
        )
        code, msg = await self.send_custom_card(from_id, card_content)
        if code == 0:
            logger.info(
                "zhaohu _run_task_llm_and_notify: card sent successfully, msgId=%s",
                msg,
            )
        else:
            logger.warning(
                "zhaohu _run_task_llm_and_notify: card send failed, code=%d msg=%s",
                code,
                msg,
            )

        # Step 2: Run LLM and collect complete result
        logger.info(
            "zhaohu _run_task_llm_and_notify: starting LLM for sessionId=%s, chat_id=%s",
            session_id,
            chat_id,
        )

        try:
            response_text = await self._collect_response_from_events(request)
            if response_text:
                logger.info(
                    "zhaohu _run_task_llm_and_notify: completed for sessionId=%s, "
                    "response_len=%d",
                    session_id,
                    len(response_text),
                )
                await self._send_task_result(session_id, response_text, meta)
            else:
                logger.warning(
                    "zhaohu _run_task_llm_and_notify: no response for sessionId=%s",
                    session_id,
                )
        except Exception:
            logger.exception(
                "zhaohu _run_task_llm_and_notify: failed for sessionId=%s",
                session_id,
            )
            await self._send_task_result(session_id, "", meta)

    async def process_callback_message(self, callback_body: Any) -> None:
        """Process callback message: query user, route by message type."""
        from ...tenant_context import bind_tenant_context

        # Extract callback fields
        (
            msg_id,
            source_id,
            from_id,
            to_id,
            group_id,
            group_name,
            msg_type,
            msg_content,
            timestamp,
        ) = self._extract_callback_fields(callback_body)

        logger.info(
            "zhaohu processing: msgId=%s fromId=%s text=%s",
            msg_id,
            from_id,
            msg_content[:50] if msg_content else "",
        )

        # Query user info
        user_info = await self._query_user_info(from_id)
        sap_id = (user_info or {}).get("sapId") or ""
        yst_id = (user_info or {}).get("ystId") or ""
        user_name = (user_info or {}).get("userName") or ""

        # Build meta
        meta = self._build_callback_meta(
            yst_id,
            source_id,
            from_id,
            to_id,
            group_id,
            group_name,
            msg_type,
            timestamp,
            user_name,
        )
        source_id = str(meta.get("source_id") or self.channel)
        meta["source_id"] = source_id
        logger.info("source id final sourceId=%s", source_id)

        with bind_tenant_context(
            tenant_id=sap_id,
            user_id=sap_id,
            source_id=source_id,
        ):
            try:
                await self._route_message(
                    msg_id,
                    from_id,
                    sap_id,
                    yst_id,
                    msg_content,
                    meta,
                )
            except Exception:
                logger.exception(
                    "zhaohu LLM processing failed: msgId=%s",
                    msg_id,
                )
                await self.send(
                    yst_id,
                    "抱歉，处理您的消息时发生错误，请稍后重试。",
                    meta,
                )

    def _extract_callback_fields(self, callback_body: Any) -> tuple:
        """Extract fields from callback body.

        Returns:
            tuple: (msg_id, from_id, to_id, group_id, group_name, msg_type, msg_content, timestamp)
        """
        return (
            getattr(callback_body, "msg_id", "") or "",
            getattr(callback_body, "source_id", "") or "",
            getattr(callback_body, "from_id", "") or "",
            getattr(callback_body, "to_id", "") or "",
            getattr(callback_body, "group_id", None),
            getattr(callback_body, "group_name", None),
            getattr(callback_body, "msg_type", "") or "",
            getattr(callback_body, "msg_content", "") or "",
            getattr(callback_body, "timestamp", 0),
        )

    def _build_callback_meta(
        self,
        yst_id: str,
        source_id: str,
        from_id: str,
        to_id: str,
        group_id: Optional[int],
        group_name: Optional[str],
        msg_type: str,
        timestamp: int,
        user_name: str,
    ) -> Dict[str, Any]:
        """Build metadata dict for callback message."""
        meta: Dict[str, Any] = {
            "send_addr": yst_id,
            "source_id": source_id,
            "open_id": from_id,
            "to_id": to_id,
            "group_id": group_id,
            "group_name": group_name,
            "msg_type": msg_type,
            "timestamp": timestamp,
            "is_group": group_id is not None,
        }
        if user_name:
            meta["user_name"] = user_name
        return meta

    async def _check_intent(self, text: str) -> bool:
        """Check if the text is a task assignment via intent recognition API.

        Args:
            text: The user input text to check

        Returns:
            True if intent is "是" (task assignment), False otherwise.
            Returns False on API failure.
        """
        if (
            not self.intent_url
            or not self.intent_open_id
            or not self.intent_api_key
        ):
            logger.warning(
                "zhaohu _check_intent: intent_url, intent_open_id, or intent_api_key "
                "not configured, defaulting to False",
            )
            return False

        payload = {
            "inputParams": {"question": text},
            "openId": self.intent_open_id,
        }

        headers = {
            "Content-Type": "application/json",
            "API-Key": self.intent_api_key,
        }

        timeout = httpx.Timeout(self.request_timeout, connect=10.0)
        context = ssl.create_default_context()
        context.options |= 0x4

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                verify=context,
            ) as client:
                response = await client.post(
                    self.intent_url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

            return_code = data.get("returnCode", "")
            if return_code != "SUC0000":
                logger.warning(
                    "zhaohu _check_intent: API returned error code=%s",
                    return_code,
                )
                return False

            body = data.get("body") or {}
            output = body.get("output") or {}
            result = output.get("result", "")

            is_task = result == "是"
            logger.info(
                "zhaohu _check_intent: text=%s, result=%s, is_task=%s",
                text[:50] if text else "",
                result,
                is_task,
            )
            return is_task

        except Exception:
            logger.exception(
                "zhaohu _check_intent: failed to call intent API for text=%s",
                text[:50] if text else "",
            )
            return False

    async def _handle_new_session(
        self,
        sap_id: str,
        from_id: str,
        first_message: str,
        meta: Dict[str, Any],
        yst_id: str,
    ) -> None:
        """Handle /new command: create a new session and switch active pointer."""
        seq = format(int(time.time()), "x")[-8:]
        new_session_id = f"zhaohu:callback:{sap_id}:{seq}"

        await self._set_active_session_id(sap_id, new_session_id)

        if not first_message:
            await self.send(yst_id, "已开启新会话，请发送消息开始对话。", meta)
            return

        content_parts = [
            TextContent(type=ContentType.TEXT, text=first_message),
        ]
        native_payload = {
            "channel_id": self.channel,
            "sender_id": sap_id,
            "session_id": new_session_id,
            "content_parts": content_parts,
            "meta": meta,
        }

        if self._workspace is not None:
            request = self.build_agent_request_from_user_content(
                channel_id=self.channel,
                sender_id=sap_id,
                session_id=new_session_id,
                content_parts=content_parts,
                channel_meta=meta,
            )
            request.channel_meta = meta
            await self._consume_with_tracker(request, native_payload)
        else:
            logger.warning(
                "zhaohu _handle_new_session: workspace not set, "
                "using direct processing",
            )
            request = self.build_agent_request_from_user_content(
                channel_id=self.channel,
                sender_id=sap_id,
                session_id=new_session_id,
                content_parts=content_parts,
                channel_meta=meta,
            )
            request.channel_meta = meta
            await self._get_llm_response_direct(
                meta,
                "",
                request,
                "",
                yst_id,
            )

    async def _route_message(
        self,
        msg_id: str,
        from_id: str,
        sap_id: str,
        yst_id: str,
        msg_content: str,
        meta: Dict[str, Any],
    ) -> None:
        """Route message by content type."""
        route_lock = await self._get_user_route_lock(sap_id)
        async with route_lock:
            await self._route_message_inner(
                msg_id,
                from_id,
                sap_id,
                yst_id,
                msg_content,
                meta,
            )

    async def _route_message_inner(
        self,
        msg_id: str,
        from_id: str,
        sap_id: str,
        yst_id: str,
        msg_content: str,
        meta: Dict[str, Any],
    ) -> None:
        """Inner routing logic, called under per-user lock."""
        msg_content_stripped = msg_content.strip()
        msg_content_len = len(msg_content_stripped)

        # Case 0: /new prefix - create new session
        if msg_content_stripped.startswith("/new"):
            after_new = (
                msg_content_stripped[4:]
                if len(msg_content_stripped) > 4
                else ""
            )
            if after_new == "" or after_new[0] == " ":
                remaining = after_new.strip()
                logger.info(
                    "zhaohu message type: new_session, content=%s",
                    msg_content_stripped[:50],
                )
                await self._handle_new_session(
                    sap_id,
                    from_id,
                    remaining,
                    meta,
                    yst_id,
                )
                return

        # Case 1: Task progress query
        if msg_content_stripped in self._TASK_PROGRESS_KEYWORDS:
            logger.info(
                "zhaohu message type: task_progress_query, content=%s",
                msg_content_stripped,
            )
            await self._query_task_progress(sap_id, from_id)
            return

        # Case 2 vs Case 3: Intent recognition
        # Length <= 5: always Case 3 (casual chat)
        # Length > 5: call intent API to determine
        is_task_assignment = False
        if msg_content_len > 5:
            is_task_assignment = await self._check_intent(msg_content_stripped)

        if is_task_assignment:
            # Case 2: Task assignment - send card notification
            logger.info(
                "zhaohu message type: task_assignment, content_len=%d",
                msg_content_len,
            )
            await self._handle_task_assignment(
                sap_id,
                from_id,
                msg_content_stripped,
                meta,
                yst_id,
                msg_content,
            )
            return

        # Case 3: Casual chat
        await self._handle_casual_chat(
            msg_id,
            sap_id,
            yst_id,
            msg_content,
            msg_content_len,
            meta,
        )

    async def _handle_task_assignment(
        self,
        sap_id: str,
        from_id: str,
        task_content: str,
        meta: Dict[str, Any],
        yst_id: str,
        msg_content: str,
    ) -> None:
        """Handle task assignment (Case 2): send card notification and process task.

        Uses the same session_id as casual chat (callback session), the only
        difference is sending a card notification before processing.

        Args:
            sap_id: User's sapId
            from_id: User's openId (for sending card)
            task_content: Task description/content from user message
            meta: Channel metadata
            yst_id: User's ystId for sending response
            msg_content: Original message content
        """
        from ....config.context import get_current_workspace_dir

        # Use active session (or fallback to legacy format)
        session_id = await self._get_active_session_id(sap_id)
        logger.info(
            "zhaohu task assignment: sessionId=%s userId=%s working_dir=%s",
            session_id,
            sap_id,
            get_current_workspace_dir(),
        )

        # Send card notification to user (task initiated)
        include_link = await self._should_include_link(sap_id)
        card_content = self._build_task_initiated_card(
            task_content,
            session_id,
            include_link=include_link,
        )
        code, msg = await self.send_custom_card(from_id, card_content)

        if code == 0:
            logger.info(
                "zhaohu _handle_task_assignment: card sent successfully, msgId=%s",
                msg,
            )
        else:
            logger.warning(
                "zhaohu _handle_task_assignment: card send failed, "
                "code=%d msg=%s",
                code,
                msg,
            )

        # Build content parts and request
        content_parts = [TextContent(type=ContentType.TEXT, text=msg_content)]

        # Build native payload (dict format) for _consume_with_tracker
        native_payload = {
            "channel_id": self.channel,
            "sender_id": sap_id,
            "session_id": session_id,
            "content_parts": content_parts,
            "meta": meta,
        }

        # Use BaseChannel's standard flow if workspace is available
        if self._workspace is not None:
            request = self.build_agent_request_from_user_content(
                channel_id=self.channel,
                sender_id=sap_id,
                session_id=session_id,
                content_parts=content_parts,
                channel_meta=meta,
            )
            request.channel_meta = meta
            await self._consume_with_tracker(request, native_payload)
        else:
            # Fallback to direct processing (no Console streaming)
            logger.warning(
                "zhaohu _handle_task_assignment: workspace not set, "
                "using direct processing without streaming support",
            )
            request = self.build_agent_request_from_user_content(
                channel_id=self.channel,
                sender_id=sap_id,
                session_id=session_id,
                content_parts=content_parts,
                channel_meta=meta,
            )
            request.channel_meta = meta
            response_text = ""
            await self._get_llm_response_direct(
                meta,
                "",  # msg_id not needed
                request,
                response_text,
                yst_id,
            )

    async def _handle_casual_chat(
        self,
        msg_id: str,
        sap_id: str,
        yst_id: str,
        msg_content: str,
        msg_content_len: int,
        meta: Dict[str, Any],
    ) -> None:
        """Handle casual chat (Case 3): proceed with LLM flow.

        Uses BaseChannel's _consume_with_tracker to enable:
        1. TaskTracker event broadcasting for Console frontend streaming
        2. Standard message sending via on_event_message_completed
        """
        from ....config.context import get_current_workspace_dir

        logger.info(
            "zhaohu message type: casual_chat, content_len=%d",
            msg_content_len,
        )

        # Build content parts
        content_parts = [TextContent(type=ContentType.TEXT, text=msg_content)]

        # Build session_id (active session or fallback to legacy format)
        session_id = await self._get_active_session_id(sap_id)
        logger.info(
            "zhaohu session: sessionId=%s userId=%s working_dir=%s",
            session_id,
            sap_id,
            get_current_workspace_dir(),
        )

        # Build native payload (dict format) for _consume_with_tracker
        native_payload = {
            "channel_id": self.channel,
            "sender_id": sap_id,
            "session_id": session_id,
            "content_parts": content_parts,
            "meta": meta,
        }

        # Use BaseChannel's standard flow if workspace is available
        # This enables TaskTracker broadcasting for Console frontend streaming
        if self._workspace is not None:
            request = self.build_agent_request_from_user_content(
                channel_id=self.channel,
                sender_id=sap_id,
                session_id=session_id,
                content_parts=content_parts,
                channel_meta=meta,
            )
            request.channel_meta = meta
            await self._consume_with_tracker(request, native_payload)
        else:
            # Fallback to direct processing (no Console streaming)
            logger.warning(
                "zhaohu _handle_casual_chat: workspace not set, "
                "using direct processing without streaming support",
            )
            request = self.build_agent_request_from_user_content(
                channel_id=self.channel,
                sender_id=sap_id,
                session_id=session_id,
                content_parts=content_parts,
                channel_meta=meta,
            )
            request.channel_meta = meta
            response_text = ""
            await self._get_llm_response_direct(
                meta,
                msg_id,
                request,
                response_text,
                yst_id,
            )

    async def _get_llm_response_direct(
        self,
        meta,
        msg_id,
        request,
        response_text,
        yst_id,
    ):
        """Direct LLM processing without TaskTracker broadcasting.

        Used as fallback when workspace is not available.
        """
        from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

        async for event in self._process(request):
            obj = getattr(event, "object", None)
            status = getattr(event, "status", None)
            if obj == "message" and status == RunStatus.Completed:
                # Extract text from the completed message
                parts = self._message_to_content_parts(event)
                for part in parts:
                    if hasattr(part, "text") and part.text:
                        response_text += part.text
                    elif hasattr(part, "refusal") and part.refusal:
                        response_text += part.refusal
        if response_text:
            logger.info(
                "zhaohu response: msgId=%s to=%s text=%s",
                msg_id,
                yst_id,
                response_text[:50] if response_text else "",
            )
            # Send response via push_url
            await self.send(yst_id, response_text, meta)
        else:
            logger.warning(
                "zhaohu no response text: msgId=%s",
                msg_id,
            )

    async def on_event_message_completed(
        self,
        request,
        to_handle: str,
        event: Any,
        send_meta: Dict[str, Any],
    ) -> None:
        """按 filter_thinking 控制推送策略：
        - True（过滤思考，前端"显示思考过程"禁用）：只推最终结果 1 条，
          思考/工具等中间消息只在 Console 流式展示，流程结束时推送
          保留的最后一条完成消息；
        - False（不过滤，前端"显示思考过程"启用）：思考/工具/最终
          结果全部推送到 Zhaohu。"""
        if self._filter_thinking:
            # 简洁模式：跳过思考，缓冲最后一条完成消息待流程结束推送
            if getattr(event, "type", None) == MessageType.REASONING:
                return
            self._zhaohu_pending[to_handle] = (event, send_meta)
            return
        # 详细模式：思考内容用不受过滤的渲染器渲染后立即推送
        if getattr(event, "type", None) == MessageType.REASONING:
            parts = self._verbose_renderer.message_to_parts(event)
            if parts:
                await self.send_content_parts(to_handle, parts, send_meta)
            return
        await super().on_event_message_completed(
            request,
            to_handle,
            event,
            send_meta,
        )

    async def _on_process_completed(
        self,
        request,
        to_handle: str,
        send_meta: Dict[str, Any],
    ) -> None:
        """简洁模式下流程结束：推送该回话保留的最后一条完成消息。"""
        if self._filter_thinking:
            pending = self._zhaohu_pending.pop(to_handle, None)
            if pending is not None:
                event, meta = pending
                await self.send_message_content(
                    to_handle,
                    event,
                    meta or send_meta,
                )
        await super()._on_process_completed(request, to_handle, send_meta)

    async def start(self) -> None:
        if not self.enabled:
            logger.debug("zhaohu channel disabled")
            return
        logger.info(
            "zhaohu channel started (outbound push + inbound callback)",
        )

    async def stop(self) -> None:
        if not self.enabled:
            return
        logger.info("zhaohu channel stopped")

    async def send(
        self,
        to_handle: str,
        text: str,
        meta: Optional[dict] = None,
    ) -> bool:
        """POST a Zhaohu push payload to the configured endpoint."""
        if not self._validate_send_configuration(to_handle, meta):
            return False
        payload = await self._build_push_payload(to_handle, text, meta or {})
        data = await self._post_push_payload(payload)
        return self._handle_push_response(to_handle, data, meta)

    def _validate_send_configuration(
        self,
        to_handle: str,
        meta: Optional[dict],
    ) -> bool:
        if not self.enabled:
            logger.warning("zhaohu send skipped: channel disabled")
            _raise_cron_delivery_failure(meta, "disabled")
            return False
        if not self.push_url:
            logger.warning(
                "zhaohu send skipped: push_url not configured for %s",
                to_handle,
            )
            _raise_cron_delivery_failure(meta, "push_url not configured")
            return False
        if (
            self.sys_id
            and self.robot_open_id
            and to_handle
            and to_handle.strip()
        ):
            return True
        logger.warning(
            "zhaohu send skipped: sys_id or robot_open_id or to_handle missing",
        )
        _raise_cron_delivery_failure(meta, "identity not configured")
        return False

    async def _post_push_payload(self, payload: dict) -> dict:
        timeout = httpx.Timeout(self.request_timeout, connect=10.0)
        # 自定义SSL上下文
        context = ssl.create_default_context()
        context.options |= 0x4
        async with httpx.AsyncClient(
            timeout=timeout,
            verify=context,
        ) as client:
            response = await client.post(self.push_url, json=payload)
            response.raise_for_status()
            try:
                data = response.json() if response.content else {}
            except ValueError:
                data = {}
        return data

    def _handle_push_response(
        self,
        to_handle: str,
        data: dict,
        meta: Optional[dict],
    ) -> bool:
        body = data.get("body") or []
        exp_msg_ids = [
            str(item.get("expMsgId"))
            for item in body
            if isinstance(item, dict) and item.get("expMsgId")
        ]
        return_code = str(data.get("returnCode") or "")
        if return_code != "SUC0000":
            detail = f"returnCode={return_code or '(empty)'}"
            if isinstance(meta, dict) and meta.get("cron_delivery_key"):
                raise RuntimeError(f"zhaohu delivery failed: {detail}")
            logger.warning(
                "zhaohu push failed: to=%s %s",
                to_handle,
                detail,
            )
            return False
        logger.info(
            "zhaohu push ok: to=%s returnCode=%s expMsgIds=%s",
            to_handle,
            return_code,
            exp_msg_ids,
        )
        return True

    async def _build_push_payload(
        self,
        to_handle: str,
        text: str,
        meta: dict,
    ) -> dict:
        # send_addr: prefer meta["send_addr"] (yst_id), then meta["yst_id"],
        # then to_handle as fallback
        send_addr = str(
            meta.get("send_addr") or meta.get("yst_id") or to_handle,
        ).strip()
        # 如果send_addr是8位，则可能是sapId，需要查询ystId
        send_addr = await self.deal_eight_sap(send_addr)
        session_id = str(meta.get("session_id") or "").strip()
        send_pk = str(
            meta.get("send_pk")
            or f"{session_id}_{send_addr}".strip("_")
            or send_addr,
        ).strip()
        summary = "小助消息提醒"
        # text_values = [{"type": "txt", "text": chunk} for chunk in _chunk_text_values(text)]
        text_values = "\n".join(_chunk_text_values(text))
        if self._filter_thinking and len(text_values.split("</think>")) > 1:
            text_values = text_values.rsplit("</think>", maxsplit=1)[
                -1
            ].strip()
        text_values = await self.crit_answer(text_values)

        # 构建消息块
        message_blocks = [
            {
                "type": "txt",
                "value": [
                    {
                        "type": "txt",
                        "text": text_values,
                    },
                ],
            },
        ]

        # 支持 link 类型消息
        if meta.get("link_url"):
            link_url = meta.get("link_url")
            link_text = meta.get("link_text", "点击查看")
            message_blocks.append(
                {
                    "type": "link",
                    "value": [
                        {
                            "subtype": "2",
                            "subvalue": link_url,
                            "text": link_text,
                        },
                    ],
                },
            )

        # 支持多链接 (link_items: list of {url, text})
        for item in meta.get("link_items", []):
            message_blocks.append(
                {
                    "type": "link",
                    "value": [
                        {
                            "subtype": "2",
                            "subvalue": item.get("url", ""),
                            "text": item.get("text", "点击查看"),
                        },
                    ],
                },
            )

        # 支持自定义 summary
        notification_summary = meta.get("notification_summary") or summary

        payload = {
            "baseInfo": {
                "sysId": self.sys_id,
                "ssnId": meta.get("ssn_id") or session_id or "",
                "ssnNo": meta.get("ssn_no") or "",
                "msgBigCls": meta.get("msg_big_cls") or "",
                "msgSmlCls": meta.get("msg_sml_cls") or "",
                "channel": self.channel_code,
                "robotOpenId": self.robot_open_id,
                "sendAddrs": [
                    {
                        "sendAddr": send_addr,
                        "sendPk": send_pk,
                    },
                ],
                "net": self.net,
            },
            "msgCtlInfo": {
                "configId": meta.get("config_id") or "",
                "batchId": (
                    meta.get("cron_delivery_key") or meta.get("batch_id") or ""
                ),
            },
            "msgContent": {
                "summary": notification_summary,
                "pushContent": notification_summary,
                "message": message_blocks,
            },
        }
        return _clean_payload(payload)

    async def deal_eight_sap(self, send_addr):
        if send_addr and len(send_addr) == 8:
            logger.info(
                "zhaohu _build_push_payload: send_addr is 8, querying user info for sapId=%s",
                send_addr,
            )
            user_info = await self._query_user_info_by_sap(send_addr)
            if user_info and user_info.get("ystId"):
                yst_id = user_info.get("ystId")
                logger.info(
                    "zhaohu _build_push_payload: sapId=%s -> ystId=%s",
                    send_addr,
                    yst_id,
                )
                send_addr = yst_id
            else:
                logger.warning(
                    "zhaohu _build_push_payload: failed to get ystId for sapId=%s",
                    send_addr,
                )
        return send_addr

    async def _mask_names(self, answer: str) -> tuple[str, bool]:
        """脱敏姓名信息

        Args:
            answer: 原始文本

        Returns:
            tuple[脱敏后文本, 是否进行了脱敏]
        """
        if not self.extract_url:
            return answer, False

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(
                    self.extract_url,
                    json={"text": answer},
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                try:
                    res = response.json() if response.content else {}
                except ValueError:
                    res = {}

            if response.status_code == 200:
                data = res
                if data and data.get("names"):
                    names = sorted(set(data["names"]), key=len, reverse=True)
                    masked = False
                    for name in names:
                        if (
                            name.lower()
                            in ("think", "thinks", "hink", "hinks")
                            or len(name) <= 1
                        ):
                            continue
                        if name in answer:
                            answer = answer.replace(
                                name,
                                f"{name[0]}{'*' * (len(name) - 1)}",
                            )
                            masked = True
                    return answer, masked
        except Exception as e:
            logger.warning(f"姓名服务失败: {e}")
        return answer, False

    async def _mask_id_cards(self, answer: str) -> tuple[str, bool]:
        """脱敏18位身份证号码

        Args:
            answer: 原始文本

        Returns:
            tuple[脱敏后文本, 是否进行了脱敏]
        """
        id18_pattern = r"(?<!\d)(\d{6})(\d{8})(\d{3}[\dXx])(?!\d)"
        masked = False

        def mask_id18(m):
            nonlocal masked
            # 简单日期校验
            try:
                y, mo, d = (
                    int(m.group(2)[:4]),
                    int(m.group(2)[4:6]),
                    int(m.group(2)[6:8]),
                )
                if 1900 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
                    masked = True
                    return f"{m.group(1)[:3]}{'*' * 11}{m.group(3)}"
            except Exception as e:
                logger.warning(f"mask_id18 error: {e}")
            return m.group(0)

        answer = re.sub(id18_pattern, mask_id18, answer, flags=re.IGNORECASE)
        return answer, masked

    async def _mask_bank_cards(self, answer: str) -> tuple[str, bool]:
        masked = False
        # 带关键词的（支持卡号后直接跟数字，不必有冒号/空格）
        card_kw = r"(?:[:：\s]*)(\d{4})\s*(\d{4,12})\s*(\d{4})\b"

        def mask_card(m):
            nonlocal masked
            if "*" in m.group(0):
                return m.group(0)
            masked = True
            mid = m.group(2).replace(" ", "")
            return f"{m.group(1)}{'*' * len(mid)}{m.group(3)}"

        answer = re.sub(card_kw, mask_card, answer, flags=re.IGNORECASE)

        # 16-19位无提示数字（避开18位已脱敏的身份证）
        card_num = r"(?<!\d)([456]\d{3})(\d{7,11})(\d{4})(?!\d)"

        def mask_card2(m):
            nonlocal masked
            full = m.group(0)
            if "*" in full or len(full) == 18:  # 跳过已脱敏或18位身份证长度
                return full
            # 19位额外检查是否像身份证号日期
            if len(full) == 19:
                try:
                    dt = full[6:14]
                    if 1900 <= int(dt[:4]) <= 2030 and 1 <= int(dt[4:6]) <= 12:
                        return full
                except Exception as e:
                    logger.warning(f"mask_card2 error: {e}")
            masked = True
            return f"{m.group(1)}{'*' * len(m.group(2))}{m.group(3)}"

        answer = re.sub(card_num, mask_card2, answer)
        return answer, masked

    async def _mask_phones(self, answer: str) -> tuple[str, bool]:
        phone = r"(?<![\d*])(1[3-9]\d)(\d{4})(\d{4})(?!\d)"
        masked = False

        def mask_phone(m):
            nonlocal masked

            if "*" in m.group(0):
                return m.group(0)
            masked = True
            return f"{m.group(1)}{'*' * 4}{m.group(3)}"

        answer = re.sub(phone, mask_phone, answer)
        return answer, masked

    async def _mask_landlines(self, answer: str) -> tuple[str, bool]:
        landline = r"(?<![\d*])(0\d{1,3}-)(\d{3,4})(\d{4})(?!\d)"
        masked = False

        def mask_ll(m):
            nonlocal masked
            if "*" in m.group(0):
                return m.group(0)
            masked = True
            return f"{m.group(1)}{'*' * len(m.group(2))}{m.group(3)}"

        answer = re.sub(landline, mask_ll, answer)
        return answer, masked

    async def crit_answer(self, answer: str) -> str:
        if answer is None:
            return ""
        if not isinstance(answer, str):
            answer = str(answer)
        if not answer.strip():
            return ""

        # 依次应用各种脱敏规则
        answer, _ = await self._mask_names(answer)
        answer, _ = await self._mask_id_cards(answer)
        answer, _ = await self._mask_bank_cards(answer)
        answer, _ = await self._mask_phones(answer)
        answer, _ = await self._mask_landlines(answer)

        return answer
