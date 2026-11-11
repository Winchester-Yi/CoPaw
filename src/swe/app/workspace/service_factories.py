# -*- coding: utf-8 -*-
"""Service factory functions for workspace components.

Factory functions are used by Workspace._register_services() to create
and initialize service components. Extracted from local functions to
improve testability and code organization.
"""

from typing import TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from .workspace import Workspace

logger = logging.getLogger(__name__)


async def create_chat_service(ws: "Workspace", service):
    """Create and attach chat manager, or reuse existing one.

    Args:
        ws: Workspace instance
        service: Existing ChatManager if reused, None if creating new
    """
    # pylint: disable=protected-access
    from ...agents.memory.conversation_archive import ConversationArchiveStore
    from ..runner.manager import ChatManager
    from ..runner.repo.json_repo import JsonChatRepository

    storage = getattr(ws, "conversation_storage", None)
    if storage is not None:
        return await storage.bind_workspace(ws)

    if service is not None:
        # Reused ChatManager - just wire to new runner
        cm = service
        ws._service_manager.services["chat_manager"] = cm
        logger.info(f"Reusing ChatManager for {ws.agent_id}")
    else:
        # Create new ChatManager
        chats_path = str(ws.workspace_dir / "chats.json")
        chat_repo = JsonChatRepository(chats_path)
        cm = ChatManager(
            repo=chat_repo,
            archive_store=ConversationArchiveStore(
                ws.workspace_dir / "dialog",
            ),
            resource_root=ws.workspace_dir / ".scenario_sessions",
            expert_dependency_root=ws.workspace_dir,
        )
        ws._service_manager.services["chat_manager"] = cm
        logger.info(f"ChatManager created: {chats_path}")

    # Always wire to new runner
    ws._service_manager.services["runner"].set_chat_manager(cm)
    # pylint: enable=protected-access


async def _init_zhaohu_binding(ws: "Workspace", zhaohu_cfg):
    """招乎渠道绑定初始化：source_id 无值时跳过，仅无已有记录时插入。"""
    from ..channels.zhaohu.binding_store import get_zhaohu_binding_store
    from ...config.context import get_current_source_id, decode_scope_id

    store = get_zhaohu_binding_store()
    if store is None:
        return
    robot_id = getattr(zhaohu_cfg, "robot_open_id", "") or ""
    if not robot_id:
        return
    try:
        effective_source_id = get_current_source_id()
        if not effective_source_id:
            logger.debug(
                "zhaohu binding skip on channel init: no source_id in context, tenant=%s",
                ws.tenant_id,
            )
            return
        # tenant_id 解码：MDAw.MDAw -> 取前半段 MDAw -> 解码为 000
        try:
            decoded_parts = decode_scope_id(ws.tenant_id)
            effective_tenant_id = (
                decoded_parts[0] if decoded_parts else ws.tenant_id
            )
        except Exception:
            effective_tenant_id = ws.tenant_id
        # 仅在无已有记录时插入，避免热加载覆盖用户更新的 priority
        existing = await store.get_binding(
            effective_tenant_id,
            effective_source_id,
        )
        if existing is None:
            await store.upsert_binding(
                tenant_id=effective_tenant_id,
                source_id=effective_source_id,
                robot_id=robot_id,
                priority=30,
            )
    except Exception:
        logger.warning(
            "zhaohu binding persist failed on channel init: tenant=%s",
            ws.tenant_id,
        )


async def create_channel_service(ws: "Workspace", service):
    """Create channel manager if configured, or reuse existing one.

    Args:
        ws: Workspace instance
        service: Existing ChannelManager if reused, None if creating new

    Returns:
        ChannelManager instance or None if not configured
    """
    # pylint: disable=protected-access
    if service is not None:
        # Reuse existing channel manager
        ws._service_manager.services["channel_manager"] = service
        return service

    from ...config.config import Config, normalize_channel_config_set
    from ...config.utils import update_last_dispatch
    from ..channels.manager import ChannelManager
    from ..channels.utils import make_process_from_runner

    normalized_channels = normalize_channel_config_set(
        ws._config.channels,
        materialize_missing_console=True,
    )
    if not normalized_channels:
        return None

    temp_config = Config(channels=normalized_channels)
    runner = ws._service_manager.services["runner"]

    def on_last_dispatch(channel, user_id, session_id):
        update_last_dispatch(
            channel=channel,
            user_id=user_id,
            session_id=session_id,
            agent_id=ws.agent_id,
            tenant_id=ws.tenant_id,
        )

    cm = ChannelManager.from_config(
        process=make_process_from_runner(runner),
        config=temp_config,
        on_last_dispatch=on_last_dispatch,
        workspace_dir=ws.workspace_dir,
    )
    ws._service_manager.services["channel_manager"] = cm

    # 招乎渠道配置创建时将绑定信息落库
    zhaohu_cfg = getattr(ws._config.channels, "zhaohu", None)
    if zhaohu_cfg is not None and ws.tenant_id:
        await _init_zhaohu_binding(ws, zhaohu_cfg)

    # Inject workspace into ChannelManager and all channels
    cm.set_workspace(ws)

    # Inject workspace into runner for control command handlers
    runner.set_workspace(ws)

    return cm
    # pylint: enable=protected-access


async def refresh_system_cron(ws: "Workspace", _):
    """系统刷新独立于旧任务管理器；同步异常不阻塞工作区启动。"""
    from ..crons.system_bridge import submit_workspace_refresh

    try:
        await submit_workspace_refresh(ws, None)
    except Exception:
        logger.warning(
            "Failed to submit workspace system cron refresh",
            exc_info=True,
        )


async def create_agent_config_watcher(ws: "Workspace", _):
    """Create agent config watcher if channel/cron exists.

    Args:
        ws: Workspace instance
        _: Unused service parameter

    Returns:
        AgentConfigWatcher instance or None if not needed
    """
    # pylint: disable=protected-access
    channel_mgr = ws._service_manager.services.get("channel_manager")
    cron_mgr = ws._service_manager.services.get("cron_manager")

    if not (channel_mgr or cron_mgr):
        return None

    from ..agent_config_watcher import AgentConfigWatcher

    watcher = AgentConfigWatcher(
        agent_id=ws.agent_id,
        workspace_dir=ws.workspace_dir,
        channel_manager=channel_mgr,
        cron_manager=cron_mgr,
        tenant_id=ws.tenant_id,
    )
    ws._service_manager.services["agent_config_watcher"] = watcher
    return watcher
    # pylint: enable=protected-access
