# -*- coding: utf-8 -*-
"""swe_mcp_clients 同步核心逻辑.

封装"扫描用户文件系统下的 agent.json + 把 MCP 客户端 upsert 到 swe_mcp_clients 表"的整套流程，
供以下两处复用：
1. admin 端点 POST /market/admin/mcp/init-swe-mcp-clients（事后批量补救）
2. 内部端点 POST /market/internal/tenants/{tenant_id}/sync-mcp
   （由 src/swe 的 tenant_initializer 在新用户 bootstrap 末尾调用）

失败语义：单个 MCP 写库异常被捕获到 results["errors"]，不影响其他 MCP 的写入。
"""

import json
import logging
from pathlib import Path
from typing import Optional, TypedDict

from .fs import _load_user_agent_config
from .service import _QUERY_USERS_BY_TENANT_IDS_SQL

logger = logging.getLogger(__name__)


class MCPClientInfo(TypedDict, total=False):
    """单个 MCP 客户端信息."""

    client_key: str
    mcp_name: str
    transport: str | None
    url: str | None
    source: str


class SyncResult(TypedDict, total=False):
    """单次 sync 的统计结果."""

    tenant_id: str
    total_workspaces: int
    total_mcp_clients: int
    synced: int
    errors: list[dict]
    details: list[dict]


async def sync_tenant_mcp(
    tenant_dir: Path,
    registry,
    source_id: Optional[str] = None,
) -> int:
    """扫描 tenant_dir 下的所有 workspace，把 MCP 客户端全量 upsert 到 swe_mcp_clients。

    给内部端点 /market/internal/tenants/{id}/sync-mcp 使用，行为最简：
    返回成功 upsert 的 MCP 客户端数。

    Args:
        tenant_dir: 用户根目录（如 ~/.swe/tenants/alice）
        registry: MCPRegistry 实例
        source_id: 租户 source_id（可选）

    Returns:
        实际 upsert 的 MCP 客户端数量（成功数）
    """
    result = await process_tenant_mcp(
        tenant_dir,
        source_id=source_id,
        registry=registry,
        dry_run=False,
    )
    return result["synced"]


async def process_tenant_mcp(
    tenant_dir: Path,
    source_id: Optional[str],
    registry,
    dry_run: bool = False,
) -> SyncResult:
    """处理单个租户目录下的所有 MCP 客户端.

    给 admin 端点 init-swe-mcp-clients 使用，支持 dry_run 和 details 统计。

    Args:
        tenant_dir: 用户根目录
        source_id: 租户 source_id
        registry: MCPRegistry
        dry_run: 试运行模式，仅统计不写库

    Returns:
        SyncResult 统计
    """
    from ..runtime.context import decode_scope_id

    dir_name = tenant_dir.name
    user_id = dir_name

    if dir_name.startswith("default_"):
        user_id = "default"
    elif "." in dir_name:
        try:
            decoded_user_id, _ = decode_scope_id(dir_name)
            user_id = decoded_user_id
        except ValueError:
            pass

    workspace_base = tenant_dir / "workspaces"
    if not workspace_base.exists():
        return {
            "tenant_id": user_id,
            "total_workspaces": 0,
            "total_mcp_clients": 0,
            "synced": 0,
            "errors": [],
            "details": [],
        }

    result: SyncResult = {
        "tenant_id": user_id,
        "total_workspaces": 0,
        "total_mcp_clients": 0,
        "synced": 0,
        "errors": [],
        "details": [],
    }

    # 查询 tenant_name 和 bbk_id（每个租户查一次）
    tenant_name = ""
    bbk_id = ""
    if source_id and registry.is_connected():
        try:
            rows = await registry.db.fetch_all(
                _QUERY_USERS_BY_TENANT_IDS_SQL.format(placeholders="%s"),
                (source_id, user_id),
            )
            if rows:
                tenant_name = rows[0].get("tenant_name", "")
                bbk_id = rows[0].get("bbk_id", "")
        except Exception as e:
            logger.warning(
                "Failed to query tenant info for %s: %s",
                user_id,
                e,
            )

    logger.info(
        "处理租户目录: dir_name=%s, user_id=%s, source_id=%s, dry_run=%s",
        dir_name,
        user_id,
        source_id,
        dry_run,
    )

    for workspace_dir in workspace_base.iterdir():
        if not workspace_dir.is_dir():
            continue
        result["total_workspaces"] += 1
        await _process_workspace_mcp(
            workspace_dir,
            user_id,
            source_id,
            registry,
            dry_run,
            result,
            tenant_name,
            bbk_id,
        )

    return result


async def _process_workspace_mcp(
    workspace_dir: Path,
    user_id: str,
    source_id: Optional[str],
    registry,
    dry_run: bool,
    result: SyncResult,
    tenant_name: str,
    bbk_id: str,
) -> None:
    """处理单个 workspace 下的所有 MCP 客户端."""
    agent_config_path = workspace_dir / "agent.json"

    if not agent_config_path.exists():
        return

    user_config = _load_user_agent_config(agent_config_path)
    if not user_config:
        return

    mcp_section = user_config.get("mcp", {})
    mcp_clients = mcp_section.get("clients", {})

    if not isinstance(mcp_clients, dict):
        return

    logger.info(
        "读取 workspace MCP 配置: user_id=%s, workspace=%s, clients_count=%d",
        user_id,
        workspace_dir.name,
        len(mcp_clients),
    )

    for client_key, client_config in mcp_clients.items():
        if not isinstance(client_config, dict):
            continue
        await _process_single_mcp_client(
            client_key,
            client_config,
            user_id,
            source_id,
            registry,
            dry_run,
            result,
            tenant_name,
            bbk_id,
        )


async def _process_single_mcp_client(
    client_key: str,
    client_config: dict,
    user_id: str,
    source_id: Optional[str],
    registry,
    dry_run: bool,
    result: SyncResult,
    tenant_name: str,
    bbk_id: str,
) -> None:
    """处理单个 MCP 客户端."""
    mcp_name = client_config.get("name", "")
    source = client_config.get("source", "")

    # 提取 marketplace item_id（仅 market 分发的 MCP 有）
    if source.startswith("marketplace:"):
        mcp_source_id = source[len("marketplace:") :]
    else:
        mcp_source_id = ""

    # 优先使用 MCP 配置中的 source_id（marketplace item_id），其次才是参数传入的 source_id
    effective_source_id = mcp_source_id or source_id or ""

    result["total_mcp_clients"] += 1

    transport = client_config.get("transport")
    url = client_config.get("url")
    cn_name = client_config.get("cn_name", "") or mcp_name

    if not dry_run:
        error = await _upsert_mcp_client_to_db(
            registry,
            client_key,
            mcp_name,
            user_id,
            tenant_name,
            bbk_id,
            source,
            effective_source_id,
            transport,
            url,
            cn_name,
        )
        if error:
            result["errors"].append(
                {
                    "tenant_id": user_id,
                    "client_key": client_key,
                    "error": f"数据库写入失败: {error}",
                },
            )
        else:
            result["synced"] += 1

    result["details"].append(
        {
            "tenant_id": user_id,
            "client_key": client_key,
            "mcp_name": mcp_name,
            "source": source,
            "source_id": effective_source_id,
        },
    )

    logger.info(
        "MCP 客户端 %s (user_id=%s): client_key=%s, mcp_name=%s, source=%s",
        client_key,
        user_id,
        client_key,
        mcp_name,
        source,
    )


async def _upsert_mcp_client_to_db(
    registry,
    client_key: str,
    mcp_name: str,
    tenant_id: str,
    tenant_name: str,
    bbk_id: str,
    source: str,
    source_id: str,
    transport: str | None,
    url: str | None,
    cn_name: str = "",
) -> str | None:
    """写入 MCP 客户端到数据库，返回错误信息或 None."""
    try:
        await registry.upsert_mcp_client(
            client_key=client_key,
            mcp_name=mcp_name,
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            bbk_id=bbk_id,
            source=source,
            source_id=source_id,
            transport=transport,
            url=url,
            enabled=True,
            cn_name=cn_name or mcp_name,
        )
        return None
    except Exception as e:
        return str(e)
