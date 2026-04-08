# -*- coding: utf-8 -*-
"""Zhaohu channel callback router.

Exports ``zhaohu_router`` with Zhaohu callback endpoint:
``/api/zhaohu/callback`` - receives inbound messages from Zhaohu platform.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Request, Response

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Zhaohu callback router
# ---------------------------------------------------------------------------
zhaohu_router = APIRouter(tags=["zhaohu"])


class ZhaohuCallbackRequest(BaseModel):
    """Zhaohu message callback request body."""

    msg_id: str = Field(default="", alias="msgId")
    from_id: str = Field(default="", alias="fromId")
    to_id: str = Field(default="", alias="toId")
    group_id: Optional[int] = Field(default=None, alias="groupId")
    group_name: Optional[str] = Field(default=None, alias="groupName")
    msg_type: str = Field(default="", alias="msgType")
    msg_content: str = Field(default="", alias="msgContent")
    timestamp: int = Field(default=0)
    custom_info: Optional[Any] = Field(default=None, alias="customInfo")

    model_config = {"populate_by_name": True}


def _get_zhaohu_channel(request: Request):
    """Retrieve the ZhaohuChannel from app state, or None."""
    app = getattr(request, "app", None)
    if not app:
        return None
    cm = getattr(app.state, "channel_manager", None)
    if not cm:
        return None
    for ch in cm.channels:
        if ch.channel == "zhaohu":
            return ch
    return None


async def _process_callback_background(
    channel,
    body: ZhaohuCallbackRequest,
) -> None:
    """Background task to process callback message.

    This runs after the response is returned to the caller.
    The channel.process_callback_message() method will:
    1. Set user context via set_request_user_id()
    2. Query user info (openId -> sapId)
    3. Load session state from file (conversation history)
    4. Call LLM with conversation context
    5. Save session state
    6. Send response via push_url
    """
    try:
        await channel.process_callback_message(body)
    except Exception:
        logger.exception(
            "zhaohu background processing failed: msgId=%s",
            body.msg_id,
        )


@zhaohu_router.post("/zhaohu/callback")
async def zhaohu_callback(
    request: Request,
    body: ZhaohuCallbackRequest,
    background_tasks: BackgroundTasks,
) -> Response:
    """Zhaohu message callback: receive inbound messages.

    Returns immediately with 'received' status, then processes the message
    in the background (query user info, call LLM, send response via push_url).
    """
    zhaohu_ch = _get_zhaohu_channel(request)
    if not zhaohu_ch:
        logger.warning("zhaohu callback received but channel not available")
        return Response(
            content='{"code": "error", "message": "channel not available"}',
            status_code=503,
            media_type="application/json",
        )

    if not zhaohu_ch.enabled:
        logger.debug("zhaohu callback received but channel disabled")
        return Response(
            content='{"code": "error", "message": "channel disabled"}',
            status_code=503,
            media_type="application/json",
        )

    # Log the incoming callback
    logger.info(
        "zhaohu callback: msgId=%s fromId=%s msgType=%s",
        body.msg_id,
        body.from_id,
        body.msg_type,
    )

    # Check for duplicate messages
    if not zhaohu_ch.try_accept_message(body.msg_id):
        logger.info(
            "zhaohu duplicate ignored: msgId=%s from=%s",
            body.msg_id,
            body.from_id,
        )
        return Response(
            content='{"code": "ok", "message": "duplicate ignored"}',
            media_type="application/json",
        )

    # Schedule background processing and return immediately
    # FastAPI will properly await the async function
    background_tasks.add_task(_process_callback_background, zhaohu_ch, body)

    return Response(
        content='{"code": "ok", "message": "received"}',
        media_type="application/json",
    )
