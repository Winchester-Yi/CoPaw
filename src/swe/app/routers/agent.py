# -*- coding: utf-8 -*-
"""Agent file management API."""

# pylint: disable=no-name-in-module
import copy
import json
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..utils import schedule_agent_reload
from ...config import (
    load_config,
    save_config,
    AgentsRunningConfig,
)
from ...config.config import load_agent_config, save_agent_config
from ...config.context import resolve_request_effective_tenant_id
from ...config.utils import (
    get_tenant_request_working_dir,
    list_logical_tenant_ids,
)
from ...agents.memory.agent_md_manager import AgentMdManager
from ...agents.utils import copy_builtin_qa_md_files, copy_md_files
from ...constant import BUILTIN_QA_AGENT_ID
from ..agent_context import (
    agent_config_update_scope,
    get_agent_for_request,
    get_agent_and_config_for_request,
)
from ..workspace.tenant_initializer import TenantInitializer

router = APIRouter(prefix="/agent", tags=["agent"])


class MdFileInfo(BaseModel):
    """Markdown file metadata."""

    filename: str = Field(..., description="File name")
    path: str = Field(..., description="File path")
    size: int = Field(..., description="Size in bytes")
    created_time: str = Field(..., description="Created time")
    modified_time: str = Field(..., description="Modified time")


class MdFileContent(BaseModel):
    """Markdown file content."""

    content: str = Field(..., description="File content")


class AgentInitRequest(BaseModel):
    """Request model for appending initialization text to a working md."""

    model_config = ConfigDict(
        populate_by_name=False,
        json_schema_extra={
            "type": "object",
            "required": ["filename", "text", "agentId"],
            "properties": {
                "filename": {
                    "type": "string",
                    "title": "Filename",
                    "description": "Top-level markdown file name",
                },
                "text": {
                    "type": "string",
                    "title": "Text",
                    "description": "Text to append",
                },
                "agentId": {
                    "type": "string",
                    "title": "Agentid",
                    "description": "Agent ID",
                },
            },
        },
    )

    filename: object = Field(None, description="Top-level markdown file name")
    text: object = Field(None, description="Text to append")
    agent_id: object = Field(None, alias="agentId", description="Agent ID")


class AgentInitResponse(BaseModel):
    """Response model for init append endpoint."""

    appended: bool = Field(..., description="Whether append succeeded")
    filename: str = Field(..., description="Resolved markdown filename")
    agent_id: str = Field(..., description="Target agent ID")


def _require_string(value: object, detail: str) -> str:
    """Require a field value to be a string for endpoint-local validation."""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=detail)
    return value


def _normalize_top_level_md_filename(filename: str | None) -> str:
    """Validate and normalize a top-level markdown filename."""
    if filename is None or not filename.strip():
        raise HTTPException(status_code=400, detail="filename is required")

    normalized = filename.strip()
    if "/" in normalized or "\\" in normalized or ".." in normalized:
        raise HTTPException(
            status_code=400,
            detail="filename must be a top-level Markdown file name",
        )
    if any(ord(char) < 32 for char in normalized):
        raise HTTPException(
            status_code=400,
            detail="filename must be a top-level Markdown file name",
        )
    if normalized.startswith("."):
        raise HTTPException(
            status_code=400,
            detail="filename must be a top-level Markdown file name",
        )
    if normalized.endswith("."):
        raise HTTPException(
            status_code=400,
            detail="filename must be a top-level Markdown file name",
        )

    path = Path(normalized)
    if path.name != normalized:
        raise HTTPException(
            status_code=400,
            detail="filename must be a top-level Markdown file name",
        )

    suffix = path.suffix.lower()
    if suffix and suffix != ".md":
        raise HTTPException(
            status_code=400,
            detail="filename must be a top-level Markdown file name",
        )
    if not suffix:
        normalized = f"{normalized}.md"
    elif path.suffix != ".md":
        normalized = f"{path.stem}.md"
    if ".." in normalized:
        raise HTTPException(
            status_code=400,
            detail="filename must be a top-level Markdown file name",
        )

    return normalized


@router.post(
    "/init",
    response_model=AgentInitResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["filename", "text", "agentId"],
                        "properties": {
                            "filename": {"type": "string"},
                            "text": {"type": "string"},
                            "agentId": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
)
async def append_init_text(
    request: Request,
) -> dict:
    """Append initialization text to a working markdown file."""
    try:
        if "agentId" in request.path_params:
            raise HTTPException(status_code=404, detail="Not Found")

        raw_body = await request.body()
        if not raw_body:
            raise HTTPException(
                status_code=400,
                detail="request body is required",
            )

        try:
            body = json.loads(raw_body)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="request body must be a JSON object",
            ) from exc

        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail="request body must be a JSON object",
            )

        raw_filename = _require_string(
            body.get("filename"),
            "filename is required",
        )
        filename = _normalize_top_level_md_filename(raw_filename)
        text = _require_string(body.get("text"), "text is required")

        raw_agent_id = _require_string(
            body.get("agentId"),
            "agentId is required",
        )
        agent_id = raw_agent_id.strip()
        if not agent_id:
            raise HTTPException(status_code=400, detail="agentId is required")

        workspace = await get_agent_for_request(request, agent_id=agent_id)
        workspace_manager = AgentMdManager(str(workspace.workspace_dir))
        workspace_manager.append_working_md(filename, text)
        return {
            "appended": True,
            "filename": filename,
            "agent_id": agent_id,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/files",
    response_model=list[MdFileInfo],
    summary="List working files",
    description="List all working files (uses active agent)",
)
async def list_working_files(
    request: Request,
) -> list[MdFileInfo]:
    """List working directory markdown files."""
    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
        )
        files = [
            MdFileInfo.model_validate(file)
            for file in workspace_manager.list_working_mds()
        ]
        return files
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/files/{md_name}",
    response_model=MdFileContent,
    summary="Read a working file",
    description="Read a working markdown file (uses active agent)",
)
async def read_working_file(
    md_name: str,
    request: Request,
) -> MdFileContent:
    """Read a working directory markdown file."""
    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
        )
        content = workspace_manager.read_working_md(md_name)
        return MdFileContent(content=content)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put(
    "/files/{md_name}",
    response_model=dict,
    summary="Write a working file",
    description="Create or update a working file (uses active agent)",
)
async def write_working_file(
    md_name: str,
    body: MdFileContent,
    request: Request,
) -> dict:
    """Write a working directory markdown file."""
    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
        )
        workspace_manager.write_working_md(md_name, body.content)
        return {"written": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/memory",
    response_model=list[MdFileInfo],
    summary="List memory files",
    description="List all memory files (uses active agent)",
)
async def list_memory_files(
    request: Request,
) -> list[MdFileInfo]:
    """List memory directory markdown files."""
    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
        )
        files = [
            MdFileInfo.model_validate(file)
            for file in workspace_manager.list_memory_mds()
        ]
        return files
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/memory/{md_name}",
    response_model=MdFileContent,
    summary="Read a memory file",
    description="Read a memory markdown file (uses active agent)",
)
async def read_memory_file(
    md_name: str,
    request: Request,
) -> MdFileContent:
    """Read a memory directory markdown file."""
    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
        )
        content = workspace_manager.read_memory_md(md_name)
        return MdFileContent(content=content)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put(
    "/memory/{md_name}",
    response_model=dict,
    summary="Write a memory file",
    description="Create or update a memory file (uses active agent)",
)
async def write_memory_file(
    md_name: str,
    body: MdFileContent,
    request: Request,
) -> dict:
    """Write a memory directory markdown file."""
    try:
        workspace = await get_agent_for_request(request)
        workspace_manager = AgentMdManager(
            str(workspace.workspace_dir),
        )
        workspace_manager.write_memory_md(md_name, body.content)
        return {"written": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/language",
    summary="Get agent language",
    description="Get the language setting for agent MD files (en/zh/ru)",
)
async def get_agent_language(request: Request) -> dict:
    """Get agent language setting for current agent."""
    workspace, agent_config = await get_agent_and_config_for_request(request)
    return {
        "language": agent_config.language,
        "agent_id": workspace.agent_id,
    }


@router.put(
    "/language",
    summary="Update agent language",
    description=(
        "Update the language for agent MD files (en/zh/ru). "
        "Optionally copies MD files for the new language to agent workspace."
    ),
)
async def put_agent_language(
    request: Request,
    body: dict = Body(
        ...,
        description='Language setting, e.g. {"language": "zh"}',
    ),
) -> dict:
    """
    Update agent language and optionally re-copy MD files to agent workspace.
    """
    language = (body.get("language") or "").strip().lower()
    valid = {"zh", "en", "ru"}
    if language not in valid:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid language '{language}'. "
                f"Must be one of: {', '.join(sorted(valid))}"
            ),
        )

    async with agent_config_update_scope(request) as (workspace, agent_config):
        agent_id = workspace.agent_id
        old_language = agent_config.language
        agent_config.language = language
        save_agent_config(
            agent_id,
            agent_config,
            tenant_id=workspace.tenant_id,
        )

    copied_files: list[str] = []
    if old_language != language:
        # Builtin QA: persona from md_files/qa/; MEMORY/HEARTBEAT from lang
        # pack; never BOOTSTRAP (remove if wrongly copied earlier).
        if agent_id == BUILTIN_QA_AGENT_ID:
            copied_files = copy_builtin_qa_md_files(
                language,
                workspace.workspace_dir,
                only_if_missing=False,
            )
        else:
            copied_files = (
                copy_md_files(
                    language,
                    workspace_dir=workspace.workspace_dir,
                )
                or []
            )

    return {
        "language": language,
        "copied_files": copied_files,
        "agent_id": agent_id,
    }


@router.get(
    "/audio-mode",
    summary="Get audio mode",
    description=(
        "Get the audio handling mode for incoming voice messages. "
        'Values: "auto", "native".'
    ),
)
async def get_audio_mode() -> dict:
    """Get audio mode setting."""
    config = load_config()
    return {"audio_mode": config.agents.audio_mode}


@router.put(
    "/audio-mode",
    summary="Update audio mode",
    description=(
        "Update how incoming audio/voice messages are handled. "
        '"auto": transcribe if provider available, else file placeholder; '
        '"native": send audio directly to model (may need ffmpeg).'
    ),
)
async def put_audio_mode(
    body: dict = Body(
        ...,
        description='Audio mode, e.g. {"audio_mode": "auto"}',
    ),
) -> dict:
    """Update audio mode setting."""
    raw = body.get("audio_mode")
    audio_mode = (str(raw) if raw is not None else "").strip().lower()
    valid = {"auto", "native"}
    if audio_mode not in valid:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid audio_mode '{audio_mode}'. "
                f"Must be one of: {', '.join(sorted(valid))}"
            ),
        )
    config = load_config()
    config.agents.audio_mode = audio_mode
    save_config(config)
    return {"audio_mode": audio_mode}


@router.get(
    "/transcription-provider-type",
    summary="Get transcription provider type",
    description=(
        "Get the transcription provider type. "
        'Values: "disabled", "whisper_api", "local_whisper".'
    ),
)
async def get_transcription_provider_type() -> dict:
    """Get transcription provider type setting."""
    config = load_config()
    return {
        "transcription_provider_type": (
            config.agents.transcription_provider_type
        ),
    }


@router.get(
    "/running-config",
    response_model=AgentsRunningConfig,
    summary="Get agent running config",
    description="Get running configuration for active agent",
)
async def get_agents_running_config(
    request: Request,
) -> AgentsRunningConfig:
    """Get agent running configuration."""
    _, agent_config = await get_agent_and_config_for_request(request)
    return agent_config.running or AgentsRunningConfig()


@router.put(
    "/running-config",
    response_model=AgentsRunningConfig,
    summary="Update agent running config",
    description="Update running configuration for active agent",
)
async def put_agents_running_config(
    running_config: AgentsRunningConfig = Body(
        ...,
        description="Updated agent running configuration",
    ),
    request: Request = None,
) -> AgentsRunningConfig:
    """Update agent running configuration."""
    async with agent_config_update_scope(request) as (workspace, agent_config):
        agent_config.running = running_config
        save_agent_config(
            workspace.agent_id,
            agent_config,
            tenant_id=workspace.tenant_id,
        )

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(
        request,
        workspace.agent_id,
        tenant_id=workspace.tenant_id,
    )

    return running_config


@router.get(
    "/system-prompt-files",
    response_model=list[str],
    summary="Get system prompt files",
    description="Get system prompt files for active agent",
)
async def get_system_prompt_files(
    request: Request,
) -> list[str]:
    """Get list of enabled system prompt files."""
    _, agent_config = await get_agent_and_config_for_request(request)
    return agent_config.system_prompt_files or []


@router.put(
    "/system-prompt-files",
    response_model=list[str],
    summary="Update system prompt files",
    description="Update system prompt files for active agent",
)
async def put_system_prompt_files(
    files: list[str] = Body(
        ...,
        description="Markdown filenames to load into system prompt",
    ),
    request: Request = None,
) -> list[str]:
    """Update list of enabled system prompt files."""
    async with agent_config_update_scope(request) as (workspace, agent_config):
        agent_config.system_prompt_files = files
        save_agent_config(
            workspace.agent_id,
            agent_config,
            tenant_id=workspace.tenant_id,
        )

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(
        request,
        workspace.agent_id,
        tenant_id=workspace.tenant_id,
    )

    return files


# ─── Agent 配置分发 ──────────────────────────────────────────────────────────

# 配置组到字段的映射：子对象组整体替换，扁平字段组按字段列表合并
CONFIG_GROUP_FIELDS: Dict[str, List[str]] = {
    "react_agent": [
        "max_iters",
        "max_input_length",
        "memory_manager_backend",
    ],
    "llm_retry": [
        "llm_retry_enabled",
        "llm_max_retries",
        "llm_backoff_base",
        "llm_backoff_cap",
    ],
    "query_retry": ["query_retry"],
    "llm_rate_limiter": [
        "llm_max_concurrent",
        "llm_chat_max_concurrent",
        "llm_cron_max_concurrent",
        "llm_max_qpm",
        "llm_rate_limit_pause",
        "llm_rate_limit_jitter",
        "llm_acquire_timeout",
        "llm_chat_acquire_timeout",
        "llm_cron_acquire_timeout",
    ],
    "context_compact": ["context_compact"],
    "tool_result_compact": ["tool_result_compact"],
    "memory_summary": ["memory_summary"],
    "embedding_config": ["embedding_config"],
}

# 子对象组：这些字段在 AgentsRunningConfig 中是嵌套的 BaseModel，
# 分发时整体替换而非逐字段合并
_CONFIG_SUB_OBJECT_GROUPS = {
    "query_retry",
    "context_compact",
    "tool_result_compact",
    "memory_summary",
    "embedding_config",
}


class AgentConfigDistributionRequest(BaseModel):
    """按配置组分发 Agent 运行配置到目标租户的请求体。"""

    config_groups: List[str] = Field(
        default_factory=list,
        description="要分发的配置组名称列表",
    )
    target_tenant_ids: List[str] = Field(
        default_factory=list,
        description="目标租户 ID 列表",
    )
    overwrite: bool = Field(
        default=True,
        description="True=覆盖目标配置，False=仅填充空值",
    )


class AgentConfigDistributionTenantResult(BaseModel):
    """单租户配置分发结果。"""

    tenant_id: str = Field(..., description="目标租户 ID")
    success: bool = Field(..., description="分发是否成功")
    updated_groups: List[str] = Field(
        default_factory=list,
        description="实际更新的配置组列表",
    )
    bootstrapped: bool = Field(
        default=False,
        description="目标租户是否在分发过程中被引导",
    )
    error: str = Field(default="", description="失败详情")


class AgentConfigDistributionResponse(BaseModel):
    """配置分发响应。"""

    results: List[AgentConfigDistributionTenantResult] = Field(
        default_factory=list,
        description="各租户分发结果",
    )


class AgentConfigDistributionTenantListResponse(BaseModel):
    """可分发目标租户列表响应。"""

    tenant_ids: List[str] = Field(default_factory=list)


def _validate_target_tenant_id(tenant_id: str) -> str:
    """校验目标租户 ID 格式，防止路径穿越。"""
    tenant_id = str(tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if len(tenant_id) > 256:
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    if ".." in tenant_id or "/" in tenant_id or "\\" in tenant_id:
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    if any(ord(c) < 32 for c in tenant_id):
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    return tenant_id


def _request_source_id(request: Request) -> str | None:
    """从请求上下文获取 source_id。"""
    return getattr(request.state, "source_id", None)


def _request_tenant_id(request: Request) -> str | None:
    """从请求上下文获取 tenant_id。"""
    return getattr(request.state, "tenant_id", None)


def _request_effective_tenant_id(request: Request) -> str | None:
    """计算当前请求的有效租户 ID。"""
    tenant_id = _request_tenant_id(request)
    if tenant_id is None:
        return None
    return resolve_request_effective_tenant_id(
        tenant_id,
        _request_source_id(request),
        getattr(request.state, "scope_id", None),
    )


def _request_tenant_working_dir(request: Request) -> Path:
    """获取当前请求的租户工作目录。"""
    return get_tenant_request_working_dir(_request_tenant_id(request))


def _get_multi_agent_manager(request: Request) -> Any:
    """获取 MultiAgentManager 实例。"""
    manager = getattr(request.app.state, "multi_agent_manager", None)
    if manager is None:
        raise RuntimeError("MultiAgentManager not initialized")
    return manager


def _merge_config_group(
    source_running: Dict[str, Any],
    target_running: Dict[str, Any],
    group: str,
    overwrite: bool,
) -> List[str]:
    """按配置组将源配置合并到目标配置。

    子对象组整体替换/填充；扁平字段组逐字段替换/填充。
    返回实际更新的字段名列表。
    """
    fields = CONFIG_GROUP_FIELDS[group]
    updated: List[str] = []

    for field_name in fields:
        if field_name not in source_running:
            continue

        # 子对象组：整体替换或仅填充默认值
        if group in _CONFIG_SUB_OBJECT_GROUPS:
            source_value = source_running[field_name]
            if overwrite:
                target_running[field_name] = copy.deepcopy(source_value)
                updated.append(field_name)
            else:
                # fill_empty：仅当目标值为 None 或不存在时填充
                target_value = target_running.get(field_name)
                if target_value is None:
                    target_running[field_name] = copy.deepcopy(source_value)
                    updated.append(field_name)
            continue

        # 扁平字段组：逐字段替换或仅填充空值
        source_value = source_running[field_name]
        if overwrite:
            target_running[field_name] = source_value
            updated.append(field_name)
        else:
            # fill_empty：仅当目标值为 None 时填充
            target_value = target_running.get(field_name)
            if target_value is None:
                target_running[field_name] = source_value
                updated.append(field_name)

    return updated


async def _distribute_config_to_tenant(
    request: Request,
    *,
    target_tenant_id: str,
    source_running: AgentsRunningConfig,
    config_groups: List[str],
    overwrite: bool,
) -> AgentConfigDistributionTenantResult:
    """将源租户的指定配置组分发到单个目标租户。

    流程：bootstrap → 加载目标配置 → 合并 → 保存 → 热重载（含回滚）。
    """
    initializer = TenantInitializer(
        _request_tenant_working_dir(request).parent,
        target_tenant_id,
        source_id=_request_source_id(request),
    )
    was_bootstrapped = initializer.has_seeded_bootstrap()
    if not was_bootstrapped:
        pool = getattr(
            getattr(getattr(request, "app", None), "state", request.state),
            "tenant_workspace_pool",
            None,
        )
        if pool is None:
            raise HTTPException(
                status_code=503,
                detail="Tenant pool not available",
            )
        await pool.ensure_bootstrap(
            target_tenant_id,
            source_id=_request_source_id(request),
        )

    effective_target_tenant_id = getattr(
        initializer,
        "effective_tenant_id",
        target_tenant_id,
    )
    target_config = load_agent_config(
        "default",
        tenant_id=effective_target_tenant_id,
    )
    original_target_config = target_config.model_copy(deep=True)

    # 将 running 配置转为 dict 进行合并
    source_dict = source_running.model_dump(mode="json")
    target_dict = (
        target_config.running.model_dump(mode="json")
        if target_config.running
        else AgentsRunningConfig().model_dump(mode="json")
    )

    all_updated_groups: List[str] = []
    for group in config_groups:
        updated = _merge_config_group(
            source_dict,
            target_dict,
            group,
            overwrite,
        )
        if updated:
            all_updated_groups.append(group)

    # 将合并后的 dict 回写到 Pydantic model
    target_config.running = AgentsRunningConfig.model_validate(target_dict)

    manager = _get_multi_agent_manager(request)
    try:
        save_agent_config(
            "default",
            target_config,
            tenant_id=effective_target_tenant_id,
        )
        await manager.reload_agent(
            "default",
            tenant_id=effective_target_tenant_id,
        )
    except Exception as exc:
        # 回滚到原始配置
        rollback_errors: List[str] = []
        try:
            save_agent_config(
                "default",
                original_target_config,
                tenant_id=effective_target_tenant_id,
            )
        except Exception as rollback_save_exc:
            rollback_errors.append(
                f"rollback save failed: {rollback_save_exc}",
            )
        else:
            try:
                await manager.reload_agent(
                    "default",
                    tenant_id=effective_target_tenant_id,
                )
            except Exception as rollback_reload_exc:
                rollback_errors.append(
                    f"rollback reload failed: {rollback_reload_exc}",
                )
        if rollback_errors:
            raise RuntimeError(
                f"{exc}; {'; '.join(rollback_errors)}",
            ) from exc
        raise

    return AgentConfigDistributionTenantResult(
        tenant_id=target_tenant_id,
        success=True,
        updated_groups=all_updated_groups,
        bootstrapped=not was_bootstrapped,
    )


@router.get(
    "/config/distribution/tenants",
    response_model=AgentConfigDistributionTenantListResponse,
    summary="获取可分发的目标租户列表",
    description="返回当前 source_id 下的所有租户 ID（排除源租户自身）",
)
async def list_agent_config_distribution_tenants(
    request: Request,
) -> AgentConfigDistributionTenantListResponse:
    """获取可分发 Agent 配置的目标租户列表。"""
    return AgentConfigDistributionTenantListResponse(
        tenant_ids=await list_logical_tenant_ids(
            _request_source_id(request),
            source_filter=True,
            include_templates=True,
        ),
    )


@router.post(
    "/config/distribute",
    response_model=AgentConfigDistributionResponse,
    summary="分发 Agent 运行配置到目标租户",
    description="按配置组将源租户的 Agent 运行配置分发到目标租户",
)
async def distribute_agent_config_to_tenants(
    request: Request,
    body: AgentConfigDistributionRequest = Body(...),
) -> AgentConfigDistributionResponse:
    """将源租户的指定配置组分发到目标租户。"""
    # 校验配置组名称
    invalid_groups = [
        g for g in body.config_groups if g not in CONFIG_GROUP_FIELDS
    ]
    if invalid_groups:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid config group(s): "
                f"{', '.join(invalid_groups)}. "
                f"Valid groups: {', '.join(sorted(CONFIG_GROUP_FIELDS.keys()))}"
            ),
        )
    if not body.config_groups:
        raise HTTPException(
            status_code=400,
            detail="No config groups provided",
        )
    if not body.target_tenant_ids:
        raise HTTPException(
            status_code=400,
            detail="No target tenant IDs provided",
        )

    # 获取源租户的 running 配置
    _, source_agent_config = await get_agent_and_config_for_request(request)
    source_running = source_agent_config.running or AgentsRunningConfig()

    results: List[AgentConfigDistributionTenantResult] = []
    for tenant_id in body.target_tenant_ids:
        try:
            validated_tenant_id = _validate_target_tenant_id(tenant_id)
            results.append(
                await _distribute_config_to_tenant(
                    request,
                    target_tenant_id=validated_tenant_id,
                    source_running=source_running,
                    config_groups=body.config_groups,
                    overwrite=body.overwrite,
                ),
            )
        except Exception as exc:
            results.append(
                AgentConfigDistributionTenantResult(
                    tenant_id=str(tenant_id),
                    success=False,
                    error=str(exc),
                ),
            )

    return AgentConfigDistributionResponse(results=results)
