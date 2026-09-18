# -*- coding: utf-8 -*-
"""我的 MCP 管理路由。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import unquote

from fastapi import (
    APIRouter,
    Body,
    HTTPException,
    Path as FastAPIPath,
    Request,
)
from pydantic import BaseModel, Field

from ...runtime.config_store import MCPClientConfig, MCPConfig
from ...runtime.context import tenant_context
from ...runtime.mcp_masking import mask_env_value, restore_original_values
from ...runtime.stateful_client import HttpStatefulClient, StdIOStatefulClient

from ...marketplace.schemas import PublishMCPRequest as MarketPublishMCPRequest
from ...marketplace.service import (
    MCPNameConflictError,
    MCPVersionConflictError,
    _QUERY_USERS_BY_TENANT_IDS_SQL,
)
from ...marketplace.mcp_registry import MCPRegistry
from ...marketplace.fs import load_index
from ...utils.logging_utils import log_params
from ..my_mcp_helpers import (
    load_agent_config_for_request,
    mark_request_state,
    save_agent_config_for_request,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/market/my-mcp", tags=["my-mcp"])
MCP_CLIENT_NOT_FOUND_TEMPLATE = "MCP client '{client_key}' not found"
NO_MCP_CLIENTS_CONFIGURED_DETAIL = "No MCP clients configured"
MANAGER_ACCESS_REQUIRED_DETAIL = "Manager access required"
NO_CLIENT_KEYS_PROVIDED_DETAIL = "No client_keys provided"
MCP_TRANSPORT_DESCRIPTION = "MCP 传输类型"
HTTP_HEADERS_DESCRIPTION = "HTTP headers"
STDIO_COMMAND_DESCRIPTION = "stdio 命令"
COMMAND_ARGS_DESCRIPTION = "命令行参数"
LAZY_LOAD_DESCRIPTION = "是否懒加载"
MCP_CLIENT_KEY_DESCRIPTION = "MCP client key"

SENSITIVE_FIELDS = [
    "transport",
    "url",
    "headers",
    "command",
    "args",
    "env",
    "cwd",
]


async def _log_my_mcp_operation(
    request: Request,
    context,
    operation: str,
    item_name: str,
) -> None:
    """在数据库可用时记录 MyMCP 操作日志。"""
    db = getattr(request.app.state.marketplace, "db", None)
    if db is None or not getattr(db, "is_connected", False):
        return

    try:
        await db.execute(
            """
            INSERT INTO swe_user_item_operation_logs
                (source_id, user_id, user_name, bbk_id, operation,
                 item_type, item_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                context.source_id,
                context.user_id,
                context.user_name,
                context.bbk_id,
                operation,
                "mcp",
                item_name,
            ),
        )
    except Exception as e:
        logger.warning(
            "Failed to log %s operation: %s",
            operation,
            e,
        )


def _is_distributed_from_market(client: MCPClientConfig) -> bool:
    """判断 MCP 是否来自市场分发。"""
    return client.source.startswith("marketplace:")


def _fill_creator_from_market(
    client: MCPClientConfig,
    detail: MyMCPDetail,
    request: Request,
    context: Any,
) -> None:
    """分发的 MCP：如果本地无创建者信息，从市场索引补充。"""
    if not _is_distributed_from_market(client) or detail.creator_name:
        return

    marketplace = getattr(request.app.state, "marketplace", None)
    if not marketplace or not context.source_id:
        return

    try:
        items = load_index(
            marketplace.marketplace_root,
            context.source_id,
        )
    except Exception:  # pylint: disable=broad-except
        return

    for item in items:
        if (
            item.item_type == "mcp"
            and item.name == client.name
            and item.status == "active"
        ):
            if item.creator_name:
                detail.creator_name = item.creator_name
            if item.creator_id:
                detail.creator_id = item.creator_id
            break


def _bump_patch(version: str) -> str:
    """Increment patch version: '1.0.0' -> '1.0.1'（委托共享工具）."""
    from ...utils.version import bump_patch

    return bump_patch(version)


def _mcp_client_not_found_detail(client_key: str) -> str:
    """构造 MCP 不存在的统一错误文案。"""
    return MCP_CLIENT_NOT_FOUND_TEMPLATE.format(client_key=client_key)


class MyMCPListItem(BaseModel):
    """我的 MCP 列表项。"""

    client_key: str = Field(..., description="唯一标识 key")
    name: str = Field(..., description="显示名称")
    description: str = Field(default="", description="描述")
    transport: Literal["stdio", "streamable_http", "sse"] = Field(
        default="stdio",
        description=MCP_TRANSPORT_DESCRIPTION,
    )
    enabled: bool = Field(default=True, description="是否启用")
    source: str = Field(default="", description="来源（本地/市场）")
    market_client_key: str = Field(
        default="",
        description="市场原始 client_key",
    )
    created_at: str = Field(default="", description="创建时间")
    updated_at: str = Field(default="", description="更新时间")
    version: str = Field(default="", description="MCP 版本号")
    received_version: str = Field(
        default="",
        description="市场分发时接收的版本号",
    )
    has_update: bool = Field(default=False, description="市场是否有新版本")


class MyMCPDetail(MyMCPListItem):
    """我的 MCP 详情。"""

    url: str = Field(default="", description="HTTP/SSE URL")
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description=HTTP_HEADERS_DESCRIPTION,
    )
    command: str = Field(default="", description=STDIO_COMMAND_DESCRIPTION)
    args: List[str] = Field(
        default_factory=list,
        description=COMMAND_ARGS_DESCRIPTION,
    )
    env: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    cwd: str = Field(default="", description="工作目录")
    lazy_load: bool = Field(default=False, description=LAZY_LOAD_DESCRIPTION)
    distributed_by: str = Field(default="", description="分发来源")
    creator_id: str = Field(default="", description="创建者 ID")
    creator_name: str = Field(default="", description="创建者名称")
    # T7 R2: update_my_mcp 响应专用字段
    version_changed: bool = Field(
        default=False,
        description="此次请求是否实际产生了版本号变更",
    )
    previous_version: str = Field(
        default="",
        description="变更前的版本号（仅 update_my_mcp 响应填充）",
    )
    bump_reason: Literal["explicit", "auto", "unchanged", ""] = Field(
        default="",
        description=(
            "版本号变更原因："
            "explicit=请求体显式指定 / auto=内容变化触发 patch+1 / "
            "unchanged=内容未变保持原版本 / 空串=非 update 场景"
        ),
    )


class MyMCPCreateRequest(BaseModel):
    """创建 MCP 请求。"""

    client_key: str = Field(..., description="唯一标识 key")
    name: str = Field(..., description="显示名称")
    description: str = Field(default="", description="描述")
    transport: Literal["stdio", "streamable_http", "sse"] = Field(
        default="stdio",
        description=MCP_TRANSPORT_DESCRIPTION,
    )
    url: str = Field(default="", description="HTTP/SSE URL")
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description=HTTP_HEADERS_DESCRIPTION,
    )
    command: str = Field(default="", description=STDIO_COMMAND_DESCRIPTION)
    args: List[str] = Field(
        default_factory=list,
        description=COMMAND_ARGS_DESCRIPTION,
    )
    env: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    cwd: str = Field(default="", description="工作目录")
    lazy_load: bool = Field(default=False, description=LAZY_LOAD_DESCRIPTION)


class MyMCPUpdateRequest(BaseModel):
    """更新 MCP 请求（所有字段可选）。"""

    name: Optional[str] = Field(None, description="显示名称")
    description: Optional[str] = Field(None, description="描述")
    transport: Optional[Literal["stdio", "streamable_http", "sse"]] = Field(
        None,
        description=MCP_TRANSPORT_DESCRIPTION,
    )
    url: Optional[str] = Field(None, description="HTTP/SSE URL")
    headers: Optional[Dict[str, str]] = Field(
        None,
        description=HTTP_HEADERS_DESCRIPTION,
    )
    command: Optional[str] = Field(None, description=STDIO_COMMAND_DESCRIPTION)
    args: Optional[List[str]] = Field(
        None,
        description=COMMAND_ARGS_DESCRIPTION,
    )
    env: Optional[Dict[str, str]] = Field(None, description="环境变量")
    cwd: Optional[str] = Field(None, description="工作目录")
    lazy_load: Optional[bool] = Field(None, description=LAZY_LOAD_DESCRIPTION)
    version: Optional[str] = Field(
        None,
        description="显式指定版本号（R2：传入则使用，不传则按内容变化决定是否 patch+1）",
    )


class MyMCPDraftTestRequest(BaseModel):
    """测试草稿 MCP 请求。"""

    baseline_client_key: Optional[str] = Field(
        None,
        description="编辑场景的原始 client_key，用于恢复脱敏字段",
    )
    name: str = Field(default="test-connection", description="显示名称")
    transport: Literal["stdio", "streamable_http", "sse"] = Field(
        default="stdio",
        description=MCP_TRANSPORT_DESCRIPTION,
    )
    url: str = Field(default="", description="HTTP/SSE URL")
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description=HTTP_HEADERS_DESCRIPTION,
    )
    command: str = Field(default="", description=STDIO_COMMAND_DESCRIPTION)
    args: List[str] = Field(
        default_factory=list,
        description=COMMAND_ARGS_DESCRIPTION,
    )
    env: Dict[str, str] = Field(default_factory=dict, description="环境变量")
    cwd: str = Field(default="", description="工作目录")


class PublishMCPRequest(BaseModel):
    """发布到市场请求。"""

    client_keys: List[str] = Field(..., description="要发布的 client_key 列表")
    category_id: Optional[int] = Field(None, description="分类 ID")
    bbk_ids: List[str] = Field(
        default_factory=list,
        description="关联 BBK ID 列表",
    )
    overwrite: bool = Field(
        default=False,
        description="同名 MCP 已存在时是否覆盖",
    )


class PublishSingleMCPRequest(BaseModel):
    """单个 MCP 发布到市场请求。"""

    category_id: Optional[int] = Field(None, description="分类 ID")
    bbk_ids: List[str] = Field(
        default_factory=list,
        description="关联 BBK ID 列表",
    )
    overwrite: bool = Field(
        default=False,
        description="同名 MCP 已存在时是否覆盖",
    )


class PublishMCPResult(BaseModel):
    """单个发布结果。"""

    client_key: str = Field(..., description=MCP_CLIENT_KEY_DESCRIPTION)
    item_id: Optional[str] = Field(None, description="市场 item ID")
    success: bool = Field(..., description="是否成功")
    error: Optional[str] = Field(None, description="错误信息")
    version_unchanged: bool = Field(
        False,
        description="内容未变化，市场版本未增加",
    )


class PublishMCPResponse(BaseModel):
    """发布响应。"""

    results: List[PublishMCPResult] = Field(
        default_factory=list,
        description="发布结果列表",
    )


class PublishSingleMCPResponse(BaseModel):
    """单个 MCP 发布响应。"""

    client_key: str = Field(..., description=MCP_CLIENT_KEY_DESCRIPTION)
    item_id: str = Field(..., description="市场 item ID")
    success: bool = Field(..., description="是否成功")
    version_unchanged: bool = Field(
        False,
        description="内容未变化，市场版本未增加",
    )


class MarketPublishContext(BaseModel):
    """发布到市场所需的公共上下文。"""

    source_id: str = Field(..., description="市场 source_id")
    user_id: str = Field(..., description="操作用户 ID")
    user_name: str = Field(..., description="操作用户名")
    category_id: Optional[int] = Field(None, description="分类 ID")
    bbk_ids: List[str] = Field(
        default_factory=list,
        description="关联 BBK ID 列表",
    )


def _mask_sensitive_values(client: MCPClientConfig) -> MyMCPDetail:
    """构建详情响应，脱敏 env 和 headers。"""

    masked_env = (
        {k: mask_env_value(v) for k, v in client.env.items()}
        if client.env
        else {}
    )
    masked_headers = (
        {k: mask_env_value(v) for k, v in client.headers.items()}
        if client.headers
        else {}
    )

    return MyMCPDetail(
        client_key="",
        name=client.name,
        description=client.description,
        transport=client.transport,
        enabled=client.enabled,
        source=client.source,
        market_client_key=client.market_client_key,
        created_at=client.created_at,
        updated_at=client.updated_at,
        version=client.version,
        received_version=client.received_version,
        url=client.url,
        headers=masked_headers,
        command=client.command,
        args=client.args,
        env=masked_env,
        cwd=client.cwd,
        lazy_load=client.lazy_load,
        distributed_by=client.distributed_by,
        creator_id=getattr(client, "creator_id", ""),
        creator_name=getattr(client, "creator_name", ""),
    )


@router.get("", response_model=List[MyMCPListItem])
async def list_my_mcp(request: Request) -> List[MyMCPListItem]:
    """获取我的 MCP 列表。"""
    context, agent_config = load_agent_config_for_request(request)
    mark_request_state(request, context)

    log_params(
        logger,
        request.method,
        request.url.path,
        source_id=context.source_id,
    )

    if agent_config.mcp is None or not agent_config.mcp.clients:
        return []

    # 获取市场 MCP 最新版本映射，用于判断 has_update
    marketplace = getattr(request.app.state, "marketplace", None)
    market_versions: dict[str, str] = {}
    market_creators: dict[str, tuple[str, str]] = (
        {}
    )  # name -> (creator_id, creator_name)
    if marketplace and context.source_id:
        try:
            items = load_index(marketplace.marketplace_root, context.source_id)
            for item in items:
                if item.item_type == "mcp" and item.status == "active":
                    market_versions[item.name] = item.version
                    market_creators[item.name] = (
                        item.creator_id,
                        item.creator_name,
                    )
        except Exception:  # pylint: disable=broad-except
            pass

    result: list[MyMCPListItem] = []
    for client_key, client in agent_config.mcp.clients.items():
        is_distributed = client.source.startswith("marketplace:")
        received_version = client.received_version
        market_version = market_versions.get(client.name)
        has_update = (
            is_distributed
            and received_version is not None
            and received_version != ""
            and market_version is not None
            and received_version != market_version
        )
        result.append(
            MyMCPListItem(
                client_key=client_key,
                name=client.name,
                description=client.description,
                transport=client.transport,
                enabled=client.enabled,
                source=client.source,
                market_client_key=client.market_client_key,
                created_at=client.created_at,
                updated_at=client.updated_at,
                version=client.version,
                received_version=received_version,
                has_update=has_update,
            ),
        )

    result.sort(key=lambda item: item.updated_at or "", reverse=True)
    return result


@router.get("/{client_key}", response_model=MyMCPDetail)
async def get_my_mcp_detail(
    request: Request,
    client_key: str = FastAPIPath(
        ...,
        description=MCP_CLIENT_KEY_DESCRIPTION,
    ),
) -> MyMCPDetail:
    """获取单个 MCP 详情。"""
    context, agent_config = load_agent_config_for_request(request)
    mark_request_state(request, context)

    log_params(
        logger,
        request.method,
        request.url.path,
        client_key=client_key,
        source_id=context.source_id,
    )

    if agent_config.mcp is None:
        raise HTTPException(
            404,
            detail=_mcp_client_not_found_detail(client_key),
        )

    client = agent_config.mcp.clients.get(client_key)
    if client is None:
        raise HTTPException(
            404,
            detail=_mcp_client_not_found_detail(client_key),
        )

    detail = _mask_sensitive_values(client)
    detail.client_key = client_key

    # 分发的 MCP：如果本地无创建者信息，从市场索引补充
    _fill_creator_from_market(client, detail, request, context)

    return detail


@router.post("", response_model=MyMCPDetail, status_code=201)
async def create_my_mcp(
    request: Request,
    body: MyMCPCreateRequest = Body(...),
) -> MyMCPDetail:
    """创建新的 MCP。"""
    context, agent_config = load_agent_config_for_request(request)
    mark_request_state(request, context)

    log_params(
        logger,
        request.method,
        request.url.path,
        client_key=body.client_key,
        name=body.name,
        transport=body.transport,
        source_id=context.source_id,
    )

    if agent_config.mcp is None:
        agent_config.mcp = MCPConfig(clients={})

    if body.client_key in agent_config.mcp.clients:
        raise HTTPException(
            400,
            detail=f"MCP client '{body.client_key}' already exists",
        )

    now = datetime.now(timezone.utc).isoformat()
    new_client = MCPClientConfig(
        name=body.name,
        description=body.description,
        enabled=True,
        transport=body.transport,
        url=body.url,
        headers=body.headers,
        command=body.command,
        args=body.args,
        env=body.env,
        cwd=body.cwd,
        lazy_load=body.lazy_load,
        source="",
        version="1.0.0",
        created_at=now,
        updated_at=now,
    )
    # 标记为当前用户创建的 MCP，供市场分发时做同名冲突检测
    new_client.creator_id = context.user_id
    new_client.creator_name = context.user_name or context.user_id

    agent_config.mcp.clients[body.client_key] = new_client
    save_agent_config_for_request(context, agent_config, request)

    await _log_my_mcp_operation(
        request,
        context,
        "create",
        body.name,
    )

    # 查询 tenant_name 和 bbk_id
    tenant_name = ""
    bbk_id = ""
    svc = request.app.state.marketplace
    if svc.db.is_connected:
        try:
            rows = await svc.db.fetch_all(
                _QUERY_USERS_BY_TENANT_IDS_SQL.format(placeholders="%s"),
                (context.source_id, context.user_id),
            )
            if rows:
                tenant_name = rows[0].get("tenant_name", "")
                bbk_id = rows[0].get("bbk_id", "")
        except Exception as e:
            logger.warning("Failed to query tenant info: %s", e)

    # 写入 swe_mcp_clients 表
    if svc.db.is_connected:
        try:
            registry = MCPRegistry(svc.db)
            await registry.upsert_mcp_client(
                client_key=body.client_key,
                mcp_name=body.name,
                tenant_id=context.user_id,
                tenant_name=tenant_name,
                bbk_id=bbk_id,
                source="",
                source_id=context.source_id,
                transport=body.transport,
                url=body.url,
                enabled=True,
                cn_name=body.name,
            )
        except Exception as e:
            logger.warning("Failed to upsert swe_mcp_clients: %s", e)

    detail = _mask_sensitive_values(new_client)
    detail.client_key = body.client_key
    return detail


def _check_distributed_mcp_sensitive_fields(
    existing: MCPClientConfig,
    update_data: dict,
) -> None:
    """校验分发的 MCP 不允许修改敏感字段，违规则抛 403。"""
    if not _is_distributed_from_market(existing):
        return
    for field in SENSITIVE_FIELDS:
        if field in update_data:
            raise HTTPException(
                403,
                detail=f"Cannot modify '{field}' for distributed MCP",
            )


def _restore_sensitive_dicts(
    update_data: dict,
    existing: MCPClientConfig,
) -> None:
    """对 env/headers 字段还原被遮蔽的原始值（原地修改 update_data）。"""
    if "env" in update_data and update_data["env"] is not None:
        update_data["env"] = restore_original_values(
            update_data["env"],
            existing.env or {},
        )
    if "headers" in update_data and update_data["headers"] is not None:
        update_data["headers"] = restore_original_values(
            update_data["headers"],
            existing.headers or {},
        )


def _resolve_version(
    update_data: dict,
    existing_dump: dict,
    previous_version: str,
) -> tuple[str, str]:
    """R2 版本决策：返回 (final_version, bump_reason)。

    逻辑：
    - 显式指定 version → 使用它（explicit）
    - 内容有变化 → 自动 bump patch（auto）
    - 内容未变 → 保持原版本（unchanged）
    """
    explicit_version = update_data.pop("version", None)

    # 计算"内容是否真有变化"——比较除 version/updated_at 外的字段
    content_changed = False
    for k, v in update_data.items():
        if k in ("version", "updated_at"):
            continue
        if existing_dump.get(k) != v:
            content_changed = True
            break

    if explicit_version:
        return explicit_version, "explicit"
    if content_changed:
        return _bump_patch(previous_version), "auto"
    return previous_version, "unchanged"


@router.put("/{client_key}", response_model=MyMCPDetail)
async def update_my_mcp(
    request: Request,
    client_key: str = FastAPIPath(...),
    body: MyMCPUpdateRequest = Body(...),
) -> MyMCPDetail:
    """更新 MCP 配置。"""
    context, agent_config = load_agent_config_for_request(request)
    mark_request_state(request, context)

    log_params(
        logger,
        request.method,
        request.url.path,
        client_key=client_key,
        source_id=context.source_id,
    )

    if agent_config.mcp is None or client_key not in agent_config.mcp.clients:
        raise HTTPException(
            404,
            detail=_mcp_client_not_found_detail(client_key),
        )

    existing = agent_config.mcp.clients[client_key]
    update_data = body.model_dump(exclude_unset=True)

    _check_distributed_mcp_sensitive_fields(existing, update_data)

    merged_data = existing.model_dump(mode="json")
    previous_version = merged_data.get("version") or "1.0.0"

    _restore_sensitive_dicts(update_data, existing)

    # R2 版本决策
    final_version, bump_reason = _resolve_version(
        update_data,
        existing.model_dump(mode="json"),
        previous_version,
    )

    merged_data.update(update_data)
    merged_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    merged_data["version"] = final_version

    updated_client = MCPClientConfig.model_validate(merged_data)
    agent_config.mcp.clients[client_key] = updated_client
    save_agent_config_for_request(context, agent_config, request)

    await _log_my_mcp_operation(
        request,
        context,
        "edit",
        updated_client.name,
    )

    # 查询 tenant_name 和 bbk_id
    tenant_name = ""
    bbk_id = ""
    svc = request.app.state.marketplace
    if svc.db.is_connected:
        try:
            rows = await svc.db.fetch_all(
                _QUERY_USERS_BY_TENANT_IDS_SQL.format(placeholders="%s"),
                (context.source_id, context.user_id),
            )
            if rows:
                tenant_name = rows[0].get("tenant_name", "")
                bbk_id = rows[0].get("bbk_id", "")
        except Exception as e:
            logger.warning("Failed to query tenant info: %s", e)

    # 更新 swe_mcp_clients 表
    if svc.db.is_connected:
        try:
            registry = MCPRegistry(svc.db)
            await registry.upsert_mcp_client(
                client_key=client_key,
                mcp_name=updated_client.name,
                tenant_id=context.user_id,
                tenant_name=tenant_name,
                bbk_id=bbk_id,
                source=getattr(updated_client, "source", "") or "",
                source_id=context.source_id,
                transport=updated_client.transport,
                url=updated_client.url,
                enabled=updated_client.enabled,
                cn_name=getattr(updated_client, "name", "") or "",
            )
        except Exception as e:
            logger.warning("Failed to upsert swe_mcp_clients: %s", e)

    detail = _mask_sensitive_values(updated_client)
    detail.client_key = client_key
    detail.version_changed = final_version != previous_version
    detail.previous_version = previous_version
    detail.bump_reason = bump_reason
    return detail


@router.delete("/{client_key}", response_model=Dict[str, str])
async def delete_my_mcp(
    request: Request,
    client_key: str = FastAPIPath(...),
) -> Dict[str, str]:
    """删除 MCP 客户端配置。"""
    context, agent_config = load_agent_config_for_request(request)
    mark_request_state(request, context)

    log_params(
        logger,
        request.method,
        request.url.path,
        client_key=client_key,
        source_id=context.source_id,
    )

    if agent_config.mcp is None or client_key not in agent_config.mcp.clients:
        raise HTTPException(
            404,
            detail=_mcp_client_not_found_detail(client_key),
        )

    # Get client name before deletion for logging
    deleted_client = agent_config.mcp.clients[client_key]
    deleted_name = deleted_client.name

    del agent_config.mcp.clients[client_key]
    save_agent_config_for_request(context, agent_config, request)

    await _log_my_mcp_operation(
        request,
        context,
        "delete",
        deleted_name,
    )

    # 从 swe_mcp_clients 表中删除记录
    svc = request.app.state.marketplace
    if svc.db.is_connected:
        try:
            registry = MCPRegistry(svc.db)
            source_id_for_db = context.source_id
            await registry.delete_mcp_client(
                tenant_id=context.user_id,
                source_id=source_id_for_db,
                client_key=client_key,
            )
        except Exception as e:
            logger.warning("Failed to delete swe_mcp_clients: %s", e)

    return {"message": f"MCP client '{client_key}' deleted"}


@router.patch("/{client_key}/toggle", response_model=MyMCPDetail)
async def toggle_my_mcp(
    request: Request,
    client_key: str = FastAPIPath(...),
) -> MyMCPDetail:
    """启用/禁用 MCP。"""
    context, agent_config = load_agent_config_for_request(request)
    mark_request_state(request, context)

    log_params(
        logger,
        request.method,
        request.url.path,
        client_key=client_key,
        source_id=context.source_id,
    )

    if agent_config.mcp is None or client_key not in agent_config.mcp.clients:
        raise HTTPException(
            404,
            detail=_mcp_client_not_found_detail(client_key),
        )

    client = agent_config.mcp.clients[client_key]
    client.enabled = not client.enabled
    client.updated_at = datetime.now(timezone.utc).isoformat()
    save_agent_config_for_request(context, agent_config, request)

    # 查询 tenant_name 和 bbk_id
    tenant_name = ""
    bbk_id = ""
    svc = request.app.state.marketplace
    if svc.db.is_connected:
        try:
            rows = await svc.db.fetch_all(
                _QUERY_USERS_BY_TENANT_IDS_SQL.format(placeholders="%s"),
                (context.source_id, context.user_id),
            )
            if rows:
                tenant_name = rows[0].get("tenant_name", "")
                bbk_id = rows[0].get("bbk_id", "")
        except Exception as e:
            logger.warning("Failed to query tenant info: %s", e)

    # 更新 swe_mcp_clients 表中的 enabled 状态
    if svc.db.is_connected:
        try:
            registry = MCPRegistry(svc.db)
            # 从 client 获取 mcp_name 和 source（MCPClientConfig 是 Pydantic 模型，source 在 extra 中）
            mcp_name = getattr(client, "name", "") or ""
            source = getattr(client, "source", "") or ""
            # 使用 context.source_id（marketplace source UUID），与 distribute_mcp 写入时保持一致
            source_id_for_db = context.source_id
            if mcp_name and source_id_for_db:
                await registry.upsert_mcp_client(
                    client_key=client_key,
                    mcp_name=mcp_name,
                    tenant_id=context.user_id,
                    tenant_name=tenant_name,
                    bbk_id=bbk_id,
                    source=source,
                    source_id=source_id_for_db,
                    transport=getattr(client, "transport", None),
                    url=getattr(client, "url", None) or None,
                    enabled=client.enabled,
                    cn_name=mcp_name,
                )
        except Exception as e:
            logger.warning("Failed to update swe_mcp_clients: %s", e)

    detail = _mask_sensitive_values(client)
    detail.client_key = client_key
    return detail


def _require_manager(request: Request) -> None:
    """校验管理员权限。"""
    if request.headers.get("X-Manager", "").lower() != "true":
        raise HTTPException(403, detail=MANAGER_ACCESS_REQUIRED_DETAIL)


async def _publish_client_to_market(
    marketplace,
    publish_context: MarketPublishContext,
    client_key: str,
    client: MCPClientConfig,
    overwrite: bool = False,
) -> PublishMCPResult:
    """复用单个 MCP 的市场发布逻辑（透传 source_user_* / operator_*）."""
    item, version_unchanged = await marketplace.publish_mcp(
        publish_context.source_id,
        MarketPublishMCPRequest(
            client_key=client_key,
            name=client.name,
            description=client.description,
            creator_id=publish_context.user_id,
            creator_name=publish_context.user_name,
            category_id=publish_context.category_id,
            bbk_ids=publish_context.bbk_ids,
            config=client.model_dump(mode="json"),
            overwrite=overwrite,
            version=client.version,
            # MCP 路径：操作者 = 内容来源（同一人，spec §6.3）
            source_user_id=publish_context.user_id,
            source_user_name=publish_context.user_name,
            source_user_version=client.version,
            operator_id=publish_context.user_id,
            operator_name=publish_context.user_name,
        ),
    )
    return PublishMCPResult(
        client_key=client_key,
        success=True,
        item_id=item.item_id,
        version_unchanged=version_unchanged,
    )


@router.post("/{client_key}/publish", response_model=PublishSingleMCPResponse)
async def publish_single_my_mcp_to_market(
    request: Request,
    client_key: str = FastAPIPath(..., description="要发布的 client_key"),
    body: PublishSingleMCPRequest = Body(...),
) -> PublishSingleMCPResponse:
    """发布单个 MCP 到市场（管理员）。"""
    _require_manager(request)

    context, agent_config = load_agent_config_for_request(request)
    mark_request_state(request, context)
    source_id = context.source_id

    log_params(
        logger,
        request.method,
        request.url.path,
        client_key=client_key,
        category_id=body.category_id,
        bbk_ids=body.bbk_ids,
        source_id=source_id,
    )

    if agent_config.mcp is None:
        raise HTTPException(400, detail=NO_MCP_CLIENTS_CONFIGURED_DETAIL)

    client = agent_config.mcp.clients.get(client_key)
    if client is None:
        raise HTTPException(
            404,
            detail=_mcp_client_not_found_detail(client_key),
        )

    marketplace = request.app.state.marketplace
    publish_context = MarketPublishContext(
        source_id=source_id,
        user_id=context.user_id,
        user_name=unquote(request.headers.get("X-User-Name", "") or ""),
        category_id=body.category_id,
        bbk_ids=body.bbk_ids,
    )
    try:
        result = await _publish_client_to_market(
            marketplace,
            publish_context,
            client_key=client_key,
            client=client,
            overwrite=body.overwrite,
        )
    except MCPNameConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "existing_item_id": exc.existing_item_id,
                "existing_name": exc.existing_name,
                "existing_creator_id": exc.existing_creator_id,
                "existing_creator_name": exc.existing_creator_name,
                "existing_version": exc.existing_version,
            },
        ) from exc
    except MCPVersionConflictError as exc:
        # F3 修复：MCP 版本快照撞车不再静默吞掉
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MCP_VERSION_CONFLICT",
                "message": str(exc),
                "hint": "本次同步内容与已有版本撞车，请稍后重试或联系管理员",
            },
        ) from exc
    return PublishSingleMCPResponse(
        client_key=result.client_key,
        item_id=result.item_id or "",
        success=result.success,
        version_unchanged=result.version_unchanged,
    )


@router.post("/publish", response_model=PublishMCPResponse)
async def publish_my_mcp_to_market(
    request: Request,
    body: PublishMCPRequest = Body(...),
) -> PublishMCPResponse:
    """发布 MCP 到市场（管理员）。"""
    _require_manager(request)

    if not body.client_keys:
        raise HTTPException(400, detail=NO_CLIENT_KEYS_PROVIDED_DETAIL)

    context, agent_config = load_agent_config_for_request(request)
    mark_request_state(request, context)
    source_id = context.source_id

    log_params(
        logger,
        request.method,
        request.url.path,
        client_keys=body.client_keys,
        category_id=body.category_id,
        bbk_ids=body.bbk_ids,
        source_id=source_id,
    )

    if agent_config.mcp is None:
        raise HTTPException(400, detail=NO_MCP_CLIENTS_CONFIGURED_DETAIL)

    marketplace = request.app.state.marketplace
    publish_context = MarketPublishContext(
        source_id=source_id,
        user_id=context.user_id,
        user_name=unquote(request.headers.get("X-User-Name", "") or ""),
        category_id=body.category_id,
        bbk_ids=body.bbk_ids,
    )

    results: list[PublishMCPResult] = []
    for client_key in body.client_keys:
        client = agent_config.mcp.clients.get(client_key)
        if client is None:
            results.append(
                PublishMCPResult(
                    client_key=client_key,
                    success=False,
                    error=_mcp_client_not_found_detail(client_key),
                ),
            )
            continue

        try:
            result = await _publish_client_to_market(
                marketplace,
                publish_context,
                client_key=client_key,
                client=client,
                overwrite=body.overwrite,
            )
            results.append(result)
        except MCPNameConflictError as exc:
            results.append(
                PublishMCPResult(
                    client_key=client_key,
                    success=False,
                    error=str(exc),
                ),
            )
        except MCPVersionConflictError as exc:
            results.append(
                PublishMCPResult(
                    client_key=client_key,
                    success=False,
                    error=f"版本冲突：{exc}",
                ),
            )
        except Exception as exc:  # pylint: disable=broad-except
            results.append(
                PublishMCPResult(
                    client_key=client_key,
                    success=False,
                    error=str(exc),
                ),
            )

    return PublishMCPResponse(results=results)


class MCPTestResult(BaseModel):
    """测试连接结果。"""

    success: bool = Field(..., description="连接是否成功")
    tools: List[Dict[str, str]] = Field(
        default_factory=list,
        description="可用工具列表",
    )
    error: str = Field(default="", description="错误信息")


async def _test_mcp_connection(
    client: MCPClientConfig,
    timeout: float = 30.0,
) -> MCPTestResult:
    """测试 MCP 连接。"""
    mcp_client = None
    try:
        if client.transport == "stdio":
            mcp_client = StdIOStatefulClient(
                name="test-connection",
                command=client.command,
                args=client.args or [],
                env=client.env or None,
                cwd=client.cwd or None,
            )
        else:
            mcp_client = HttpStatefulClient(
                name="test-connection",
                transport=client.transport,
                url=client.url,
                headers=client.headers or None,
            )

        await mcp_client.connect(timeout=timeout)
        tools = await mcp_client.list_tools(timeout=timeout)
        await mcp_client.close()

        return MCPTestResult(
            success=True,
            tools=[
                {"name": tool.name, "description": tool.description or ""}
                for tool in tools
            ],
        )
    except asyncio.TimeoutError:
        if mcp_client:
            try:
                await mcp_client.close(ignore_errors=True)
            except Exception:
                pass
        return MCPTestResult(success=False, error="连接超时")
    except Exception as exc:  # pylint: disable=broad-except
        if mcp_client:
            try:
                await mcp_client.close(ignore_errors=True)
            except Exception:
                pass
        return MCPTestResult(success=False, error=str(exc))


def _build_draft_test_client(
    body: MyMCPDraftTestRequest,
    existing: MCPClientConfig | None = None,
) -> MCPClientConfig:
    """根据草稿请求构造临时 MCP 配置。"""
    draft_env = body.env
    draft_headers = body.headers
    if existing is not None:
        draft_env = restore_original_values(draft_env, existing.env or {})
        draft_headers = restore_original_values(
            draft_headers,
            existing.headers or {},
        )

    now = datetime.now(timezone.utc).isoformat()
    return MCPClientConfig(
        name=body.name or "test-connection",
        description=existing.description if existing else "",
        enabled=True,
        transport=body.transport,
        url=body.url,
        headers=draft_headers,
        command=body.command,
        args=body.args,
        env=draft_env,
        cwd=body.cwd,
        lazy_load=existing.lazy_load if existing else False,
        source=existing.source if existing else "",
        market_client_key=existing.market_client_key if existing else "",
        distributed_by=existing.distributed_by if existing else "",
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )


@router.post("/draft-test", response_model=MCPTestResult)
async def test_my_mcp_draft_connection(
    request: Request,
    body: MyMCPDraftTestRequest = Body(...),
) -> MCPTestResult:
    """测试弹窗中的草稿 MCP 配置。"""
    context, agent_config = load_agent_config_for_request(request)
    mark_request_state(request, context)

    log_params(
        logger,
        request.method,
        request.url.path,
        transport=body.transport,
        source_id=context.source_id,
    )

    existing: MCPClientConfig | None = None
    if body.baseline_client_key:
        if (
            agent_config.mcp is None
            or body.baseline_client_key not in agent_config.mcp.clients
        ):
            raise HTTPException(
                404,
                detail=_mcp_client_not_found_detail(
                    body.baseline_client_key,
                ),
            )
        existing = agent_config.mcp.clients[body.baseline_client_key]

    client = _build_draft_test_client(body, existing)
    with tenant_context(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        source_id=context.source_id,
    ):
        return await _test_mcp_connection(client)


@router.post("/{client_key}/test", response_model=MCPTestResult)
async def test_my_mcp_connection(
    request: Request,
    client_key: str = FastAPIPath(...),
) -> MCPTestResult:
    """测试 MCP 连接。"""
    context, agent_config = load_agent_config_for_request(request)
    mark_request_state(request, context)

    log_params(
        logger,
        request.method,
        request.url.path,
        client_key=client_key,
        source_id=context.source_id,
    )

    if agent_config.mcp is None or client_key not in agent_config.mcp.clients:
        raise HTTPException(
            404,
            detail=_mcp_client_not_found_detail(client_key),
        )

    client = agent_config.mcp.clients[client_key]
    with tenant_context(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        source_id=context.source_id,
    ):
        return await _test_mcp_connection(client)
