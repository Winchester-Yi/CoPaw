# -*- coding: utf-8 -*-
"""市场 MCP 管理路由（管理员）。"""

import json
import asyncio
import logging
import uuid
import re
from typing import Annotated, Optional
from urllib.parse import unquote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)

from ...marketplace.schemas import (
    AsyncTaskSubmitResponse,
    MCPDistributionRequest,
    MCPDistributionResponse,
    MarketMCPDetail,
    MarketMCPItem,
    PublishMCPRequest,
    UpdateMarketMCPMetadataRequest,
    UploadMCPResponse,
)
from ...marketplace.service import (
    MCPNameConflictError,
    MCPVersionConflictError,
)
from ...marketplace.fs import (
    load_mcp_config,
    load_index,
    normalize_mcp_config_data,
)
from ...runtime.config_store import MCPClientConfig
from ...runtime.context import tenant_context
from ..deps import require_source_id
from .my_mcp import _test_mcp_connection

router = APIRouter()
logger = logging.getLogger(__name__)
MCP_NOT_FOUND_DETAIL = "MCP not found or already deleted"


def _require_manager(x_manager: Optional[str]) -> None:
    """校验管理员权限。"""
    if x_manager != "true":
        raise HTTPException(status_code=403, detail="Manager access required")


class UploadMCPFormData:
    """上传 MCP 的表单字段。"""

    def __init__(
        self,
        name: Optional[str] = Form(default=None),
        chinese_name: Optional[str] = Form(default=""),
        description: Optional[str] = Form(default=""),
        guidance: Optional[str] = Form(default=""),
        bbk_ids: Optional[str] = Form(default=None),
        raw_json: Optional[str] = Form(default=None),
    ) -> None:
        self.name = name
        self.chinese_name = chinese_name
        self.description = description
        self.guidance = guidance
        self.bbk_ids = bbk_ids
        # 与 file 二选一：用户在前端选择"粘贴 JSON"模式时，原始 JSON 字符串走这里
        self.raw_json = raw_json


def _normalize_client_key(value: str) -> str:
    """规范化 client_key，保持与前端自动生成逻辑一致。"""
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    return normalized or "mcp"


def _new_async_task_id() -> str:
    """生成异步任务 ID。"""
    return str(uuid.uuid4())


def _get_async_task_store(request: Request):
    """创建 Market 异步任务写入器。"""
    from ..async_tasks import AsyncTaskStore

    db = request.app.state.marketplace.db
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(
            status_code=503,
            detail="Async task database connection is not available",
        )
    return AsyncTaskStore(db)


def _distribution_summary(kind: str, name: str, target_count: int) -> str:
    """构造包含分发对象的任务摘要。"""
    object_name = str(name or "").strip() or "-"
    return f"分发 {kind}「{object_name}」，目标 {target_count} 个用户"


def _find_market_mcp_item(svc, source_id: str, item_ref: str):
    """按 item_id、client_key 或名称解析市场 MCP 条目。"""
    items = load_index(svc.marketplace_root, source_id)
    return next(
        (
            candidate
            for candidate in items
            if candidate.item_type == "mcp"
            and item_ref
            in {
                candidate.item_id,
                candidate.client_key,
                candidate.name,
            }
        ),
        None,
    )


async def _run_mcp_distribution_task(
    *,
    task_id: str,
    store,
    svc,
    source_id: str,
    item_id: str,
    operator_id: str,
    operator_name: str,
    req: MCPDistributionRequest,
) -> None:
    """后台执行 MCP 分发并回写任务表。"""
    try:
        await store.mark_running(task_id)
        result = await svc.distribute_mcp(
            source_id,
            item_id,
            operator_id=operator_id,
            operator_name=operator_name,
            req=req,
        )
        done_count = 0
        failed_count = 0
        for item in result.results:
            await store.record_item_result(
                task_id=task_id,
                target_id=item.tenant_id,
                success=item.success,
                result={
                    **item.model_dump(),
                    "item_id": item_id,
                },
                error_message=item.error,
            )
            if item.success:
                done_count += 1
            else:
                failed_count += 1
        await store.finish_task(
            task_id=task_id,
            status=(
                "succeeded"
                if failed_count == 0
                else ("failed" if done_count == 0 else "partial_failed")
            ),
            done_count=done_count,
            failed_count=failed_count,
            error_message=None if failed_count == 0 else "部分目标分发失败",
            result={
                **result.model_dump(),
                "item_id": item_id,
            },
        )
    except Exception as exc:  # pylint: disable=broad-except
        for tenant_id in req.target_tenant_ids:
            try:
                await store.record_item_result(
                    task_id=task_id,
                    target_id=tenant_id,
                    success=False,
                    error_message=str(exc),
                )
            except Exception:
                logger.warning(
                    "Failed to record MCP distribution item failure: task_id=%s tenant_id=%s",
                    task_id,
                    tenant_id,
                    exc_info=True,
                )
        try:
            await store.finish_task(
                task_id=task_id,
                status="failed",
                done_count=0,
                failed_count=len(req.target_tenant_ids),
                error_message=str(exc),
                result={
                    "item_id": item_id,
                    "status": "failed",
                    "error": str(exc),
                },
            )
        except Exception:
            logger.warning(
                "Failed to finish MCP distribution task: task_id=%s",
                task_id,
                exc_info=True,
            )


def _infer_transport(config: dict) -> Optional[str]:
    """从兼容格式中推断 transport。"""
    raw_transport = (
        config.get("transport")
        or config.get("type")
        or (config.get("advanced") or {}).get("transport")
    )
    if isinstance(raw_transport, str):
        normalized = raw_transport.lower()
        if normalized == "stdio":
            return "stdio"
        if normalized == "sse":
            return "sse"
        if normalized in {"streamable_http", "streamable-http"}:
            return "streamable_http"
    if (
        isinstance(config.get("command"), str)
        and config.get("command", "").strip()
    ):
        return "stdio"
    if isinstance(config.get("url"), str) and config.get("url", "").strip():
        return "streamable_http"
    return None


def _build_upload_fallback_name(filename: str) -> str:
    """根据文件名生成上传名称兜底值。"""
    return re.sub(
        r"\.(json|mcp\.json)$",
        "",
        filename,
        flags=re.IGNORECASE,
    )


def _extract_mcp_servers_payload(
    file_data: dict,
) -> tuple[str, str, dict]:
    """从 mcpServers 结构中提取 client_key、name 和 config。"""
    mcp_servers = file_data.get("mcpServers")
    if not isinstance(mcp_servers, dict) or not mcp_servers:
        return "", "", {}

    first_key, first_value = next(iter(mcp_servers.items()))
    if not isinstance(first_value, dict):
        return "", "", {}

    config = dict(first_value)
    return str(first_key), str(config.get("name") or ""), config


def _extract_direct_payload(
    file_data: dict,
    current_client_key: str,
    current_name: str,
) -> tuple[str, str, dict]:
    """从扁平 config 或 config 包裹结构中提取数据。"""
    raw_config = file_data.get("config", file_data)
    if not isinstance(raw_config, dict):
        return current_client_key, current_name, {}

    config = dict(raw_config)
    client_key = str(file_data.get("client_key") or current_client_key or "")
    name = str(
        config.get("name") or file_data.get("name") or current_name or "",
    )
    return client_key, name, config


def _finalize_upload_payload(
    fallback_name: str,
    client_key: str,
    name: str,
    config: dict,
) -> tuple[str, str, dict]:
    """补全 transport/name/client_key，并校验最终结果。"""
    if not config:
        raise ValueError("文件格式不正确")

    transport = _infer_transport(config)
    if not transport:
        raise ValueError("文件格式不正确：无法识别连接方式")

    config["transport"] = transport
    final_name = name.strip() or fallback_name
    final_client_key = _normalize_client_key(
        client_key or final_name or fallback_name,
    )
    return final_client_key, final_name, config


def _extract_upload_payload(
    filename: str,
    file_data: dict,
) -> tuple[str, str, dict]:
    """从上传文件中提取 client_key、name 和规范化后的 config。"""
    fallback_name = _build_upload_fallback_name(filename)
    client_key, name, config = _extract_mcp_servers_payload(file_data)
    if not config:
        client_key, name, config = _extract_direct_payload(
            file_data,
            client_key,
            name,
        )
    return _finalize_upload_payload(
        fallback_name,
        client_key,
        name,
        config,
    )


@router.post(
    "/market/mcp",
    response_model=MarketMCPItem,
    status_code=status.HTTP_201_CREATED,
)
async def publish_mcp(
    req: PublishMCPRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """发布 MCP 到市场（管理员）。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    try:
        item, version_unchanged = await svc.publish_mcp(source_id, req)
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
    return MarketMCPItem(
        item_id=item.item_id,
        client_key=item.client_key,
        name=item.name,
        chinese_name=item.chinese_name,
        description=item.description,
        guidance=item.guidance,
        version=item.version,
        creator_id=item.creator_id,
        creator_name=item.creator_name,
        category_id=item.category_id,
        bbk_ids=item.bbk_ids,
        created_at=item.created_at,
        updated_at=item.updated_at,
        call_count=0,
        user_count=0,
        version_unchanged=version_unchanged,
    )


async def _parse_upload_json(
    file: Optional[UploadFile],
    form: UploadMCPFormData,
) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """解析上传的 JSON 数据（file 与 raw_json 二选一）。

    Returns:
        (file_data, source_filename, error_message) — 成功时 error_message 为 None。
    """
    has_file = file is not None and file.filename
    has_raw_json = bool(form.raw_json and form.raw_json.strip())
    if not has_file and not has_raw_json:
        return None, None, "Either file or raw_json is required"

    if has_file:
        assert file is not None  # 给类型检查器看的，has_file 已经保证
        if not file.filename.endswith(".json"):
            return None, None, "Only .json files are accepted"
        try:
            content = await file.read()
            file_data = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return None, None, f"Invalid JSON: {e}"
        return file_data, file.filename, None

    # 粘贴 JSON 路径
    try:
        file_data = json.loads(form.raw_json or "")
    except json.JSONDecodeError as e:
        return None, None, f"Invalid JSON: {e}"
    return file_data, "pasted.json", None


def _build_publish_request_from_upload(
    form: UploadMCPFormData,
    client_key: str,
    inferred_name: str,
    config: dict,
    x_user_id: Optional[str],
    x_user_name: Optional[str],
) -> PublishMCPRequest:
    """从上传数据构建 PublishMCPRequest。"""
    final_name = form.name or inferred_name
    uploaded_version = config.get("version", "")
    return PublishMCPRequest(
        client_key=client_key,
        name=final_name,
        chinese_name=form.chinese_name or "",
        description=form.description or config.get("description", ""),
        guidance=form.guidance or "",
        creator_id=x_user_id or "unknown",
        creator_name=unquote(x_user_name or ""),
        category_id=None,
        bbk_ids=json.loads(form.bbk_ids) if form.bbk_ids else [],
        config=config,
        version=uploaded_version,
        overwrite=True,  # 管理员手动上传即意图覆盖同名条目
        # admin zip 上传：source_user 留空，version=v0.0.0（spec R6）
        source_user_id="",
        source_user_name="",
        source_user_version="v0.0.0",
        operator_id=x_user_id or "",
        operator_name=unquote(x_user_name or ""),
    )


async def _do_upload_publish(
    svc,
    source_id: str,
    req: PublishMCPRequest,
) -> UploadMCPResponse:
    """执行市场发布并处理异常。"""
    try:
        _, version_unchanged = await svc.publish_mcp(source_id, req)
        return UploadMCPResponse(
            success=True,
            version_unchanged=version_unchanged,
        )
    except MCPNameConflictError as exc:
        return UploadMCPResponse(
            success=False,
            error=str(exc),
        )
    except MCPVersionConflictError as exc:
        return UploadMCPResponse(
            success=False,
            error=f"版本冲突：{exc}",
        )
    except Exception as e:
        return UploadMCPResponse(success=False, error=str(e))


@router.post(
    "/market/mcp/upload",
    response_model=UploadMCPResponse,
)
async def upload_mcp(
    request: Request,
    file: Optional[UploadFile] = File(default=None),
    form: UploadMCPFormData = Depends(),
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
):
    """上传 MCP 连接器到市场（管理员）。

    支持两种入口（二选一）：
    - file: 上传 .json 文件（multipart 文件字段）
    - form.raw_json: 直接粘贴的 JSON 字符串（form 字段）
    """
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)

    file_data, source_filename, error = await _parse_upload_json(file, form)
    if error is not None:
        return UploadMCPResponse(success=False, error=error)

    # _parse_upload_json 返回 None 仅在 error 非 None 时，此处已排除 error
    assert file_data is not None and source_filename is not None

    try:
        client_key, inferred_name, config = _extract_upload_payload(
            source_filename,
            file_data,
        )
    except ValueError as e:
        return UploadMCPResponse(success=False, error=str(e))

    req = _build_publish_request_from_upload(
        form,
        client_key,
        inferred_name,
        config,
        x_user_id,
        x_user_name,
    )

    svc = request.app.state.marketplace
    return await _do_upload_publish(svc, source_id, req)


@router.post(
    "/market/mcp/{item_id}/distribute",
    response_model=AsyncTaskSubmitResponse,
)
async def distribute_mcp(
    item_id: str,
    req: MCPDistributionRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
) -> AsyncTaskSubmitResponse:
    """分发 MCP（管理员）。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace

    # 前端可能传市场 item_id，也可能传业务侧 client_key 或名称。
    item = _find_market_mcp_item(svc, source_id, item_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=MCP_NOT_FOUND_DETAIL,
        )

    if not getattr(svc.db, "is_connected", False):
        raise HTTPException(
            status_code=503,
            detail="Async task database connection is not available",
        )

    task_id = _new_async_task_id()
    store = _get_async_task_store(request)
    await store.start_task(
        task_id=task_id,
        service="market",
        task_type="market.mcp.distribute",
        source_id=source_id,
        actor_user_id=x_user_id or "",
        actor_user_name=unquote(x_user_name or ""),
        target_ids=req.target_tenant_ids,
        summary=_distribution_summary(
            "MCP",
            item.name or item.client_key or item_id,
            len(req.target_tenant_ids),
        ),
    )
    asyncio.create_task(
        _run_mcp_distribution_task(
            task_id=task_id,
            store=store,
            svc=svc,
            source_id=source_id,
            item_id=item.item_id,
            operator_id=x_user_id or "",
            operator_name=unquote(x_user_name or ""),
            req=req,
        ),
    )
    return AsyncTaskSubmitResponse(task_id=task_id)


@router.delete(
    "/market/mcp/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_mcp(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
):
    """删除市场 MCP（管理员）。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace

    # 检查条目是否存在
    items = load_index(svc.marketplace_root, source_id)
    item = next(
        (i for i in items if i.item_id == item_id and i.item_type == "mcp"),
        None,
    )
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=MCP_NOT_FOUND_DETAIL,
        )

    ok = await svc.delete_mcp(
        source_id,
        item_id,
        operator_id=x_user_id or "",
        operator_name=unquote(x_user_name or ""),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="MCP not found")


@router.put("/market/mcp/{item_id}/metadata", response_model=MarketMCPDetail)
async def update_market_mcp_metadata(
    item_id: str,
    payload: UpdateMarketMCPMetadataRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """更新 MCP 市场条目的展示元数据。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    user_bbk_id = x_bbk_id or "100"
    svc = request.app.state.marketplace
    try:
        svc.update_mcp_metadata(
            source_id=source_id,
            item_id=item_id,
            chinese_name=payload.chinese_name,
            description=payload.description,
            guidance=payload.guidance,
            bbk_ids=payload.bbk_ids,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    detail = await svc.get_mcp_detail(source_id, item_id, user_bbk_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="MCP not found")
    return detail


@router.post("/market/mcp/{item_id}/test")
async def test_market_mcp(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """测试市场 MCP 连接。"""
    source_id = require_source_id(x_source_id)
    svc = request.app.state.marketplace

    # 获取 MCP 配置
    items = load_index(svc.marketplace_root, source_id)
    item = next(
        (i for i in items if i.item_id == item_id and i.item_type == "mcp"),
        None,
    )
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=MCP_NOT_FOUND_DETAIL,
        )

    mcp_config = load_mcp_config(svc.marketplace_root, source_id, item_id)
    if mcp_config is None:
        raise HTTPException(status_code=404, detail="MCP config not found")

    config_data = normalize_mcp_config_data(
        mcp_config.get("config", {}),
    )
    if not config_data.get("name"):
        config_data["name"] = item.name or item.client_key or "market-mcp"
    config_data.setdefault("description", item.description or "")
    config_data.setdefault("enabled", True)
    client_config = MCPClientConfig(**config_data)

    # 与 MyMCP 测试连接保持同一实现，避免两处逻辑继续漂移。
    tenant_id = x_user_id or "default"
    with tenant_context(
        tenant_id=tenant_id,
        user_id=tenant_id,
        source_id=source_id,
    ):
        return await _test_mcp_connection(client_config)


@router.get(
    "/market/mcp/{item_id}/distributions",
)
async def get_mcp_distributions(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """查询 MCP 分发记录（管理员）."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    distributions = await svc.get_distributions(source_id, item_id, "mcp")
    return distributions


@router.post(
    "/market/mcp/recall",
)
async def recall_mcp_by_name(
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
) -> dict:
    """按 MCP 名称撤回（管理员）."""
    from ...marketplace.schemas import RecallRequest

    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace

    # 解析请求体
    body = await request.json()
    target_user_ids = body.get("target_user_ids")
    mcp_name = body.get("mcp_name")
    req = RecallRequest(
        target_user_ids=target_user_ids,
        mcp_name=mcp_name,
    )

    try:
        result = await svc.recall_mcp(
            source_id,
            None,
            operator_id=x_user_id or "",
            operator_name=unquote(x_user_name or ""),
            req=req,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result.model_dump()


@router.post(
    "/market/mcp/{item_id}/recall",
)
async def recall_mcp(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
) -> dict:
    """撤回已分发的 MCP（管理员）."""
    from ...marketplace.schemas import RecallRequest

    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace

    # 解析请求体
    body = await request.json()
    target_user_ids = body.get("target_user_ids")
    force = body.get("force", False)
    req = RecallRequest(
        target_user_ids=target_user_ids,
        force=force,
    )

    try:
        result = await svc.recall_mcp(
            source_id,
            item_id,
            operator_id=x_user_id or "",
            operator_name=unquote(x_user_name or ""),
            req=req,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result.model_dump()
