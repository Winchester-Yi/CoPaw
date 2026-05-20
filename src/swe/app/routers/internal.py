# -*- coding: utf-8 -*-
"""Internal API for service-to-service communication."""

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ...config.context import is_valid_identity_value, resolve_scope_id
from ...constant import WORKING_DIR

router = APIRouter(prefix="/internal", tags=["internal"])
public_router = APIRouter(prefix="/assets/text", tags=["assets"])
logger = logging.getLogger(__name__)

# 内部服务认证 Token（可选）
_INTERNAL_TOKEN = os.environ.get("SWE_INTERNAL_TOKEN", "")
_ASSET_ROOT_DIRNAME = "asset"
_DEFAULT_FILE_URL_BASE = "http://localhost:8088"
_STATIC_AGENT_ID = "default"
_INVALID_FILE_NAME_DETAIL = "Invalid file_name"
_ASSET_NOT_FOUND_DETAIL = "Asset file not found"
_ASSET_INVALID_UTF8_DETAIL = "Asset file is not valid UTF-8"
_CONTENT_INVALID_UTF8_DETAIL = "Content is not valid UTF-8"


class InternalErrorResponse(BaseModel):
    detail: str = Field(..., description="Error detail message.")


class InternalTextAssetReadResponse(BaseModel):
    success: bool = Field(default=True)
    file_name: str
    content: str


class InternalTextAssetWriteRequest(BaseModel):
    user_id: str
    source_id: str
    content: str


class InternalTextAssetWriteResponse(BaseModel):
    success: bool = Field(default=True)
    file_name: str
    scope_id: str
    public_url: str


def _verify_internal_token(token: Optional[str]) -> None:
    """验证内部服务 Token（如果配置了的话）."""
    if _INTERNAL_TOKEN:
        if not token or token != f"Bearer {_INTERNAL_TOKEN}":
            raise HTTPException(status_code=401, detail="Unauthorized")


def _validate_asset_file_name(file_name: str) -> str:
    path = Path(file_name)
    has_reserved_name = not file_name or file_name in {".", ".."}
    has_path_component = path.name != file_name or path.is_absolute()
    has_forbidden_character = any(
        char in file_name for char in ("/", "\\", "\x00")
    )
    if has_reserved_name or has_path_component or has_forbidden_character:
        raise HTTPException(
            status_code=400,
            detail=_INVALID_FILE_NAME_DETAIL,
        )
    return file_name


def _decode_utf8_content(raw_content: bytes) -> str:
    try:
        return raw_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=_ASSET_INVALID_UTF8_DETAIL,
        ) from exc


def _validate_utf8_text(content: str) -> str:
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=_CONTENT_INVALID_UTF8_DETAIL,
        ) from exc
    return content


def _generate_text_asset_file_name(user_id: str) -> str:
    timestamp = datetime.now(tz=timezone.utc).strftime(
        "%Y%m%d%H%M%S%f",
    )[:-3]
    return f"{user_id}-{timestamp}.html"


def _build_public_url(scope_id: str, file_name: str) -> str:
    base_url = os.getenv("FILE_URL", _DEFAULT_FILE_URL_BASE).rstrip("/")
    return f"{base_url}/static/{scope_id}/{_STATIC_AGENT_ID}/{file_name}"


def _get_asset_file_path(file_name: str) -> Path:
    return WORKING_DIR / _ASSET_ROOT_DIRNAME / file_name


def _get_scope_static_dir(scope_id: str) -> Path:
    return WORKING_DIR / scope_id / "workspaces" / _STATIC_AGENT_ID / "static"


def _read_text_asset(file_name: str) -> InternalTextAssetReadResponse:
    safe_file_name = _validate_asset_file_name(file_name)
    asset_file = _get_asset_file_path(safe_file_name)
    if not asset_file.exists() or not asset_file.is_file():
        raise HTTPException(status_code=404, detail=_ASSET_NOT_FOUND_DETAIL)
    content = _decode_utf8_content(asset_file.read_bytes())
    return InternalTextAssetReadResponse(
        file_name=safe_file_name,
        content=content,
    )


def _write_text_asset(
    payload: InternalTextAssetWriteRequest,
) -> InternalTextAssetWriteResponse:
    if not is_valid_identity_value(payload.user_id):
        raise HTTPException(status_code=400, detail="Invalid user_id")
    if not is_valid_identity_value(payload.source_id):
        raise HTTPException(status_code=400, detail="Invalid source_id")

    scope_id = resolve_scope_id(payload.user_id, payload.source_id)
    if scope_id is None:
        raise HTTPException(status_code=400, detail="Failed to resolve scope")

    file_name = _generate_text_asset_file_name(payload.user_id)
    content = _validate_utf8_text(payload.content)
    static_dir = _get_scope_static_dir(scope_id)
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / file_name).write_text(content, encoding="utf-8")

    return InternalTextAssetWriteResponse(
        file_name=file_name,
        scope_id=scope_id,
        public_url=_build_public_url(scope_id, file_name),
    )


@router.get(
    "/assets/text/read",
    response_model=InternalTextAssetReadResponse,
    responses={
        400: {"model": InternalErrorResponse},
        401: {"model": InternalErrorResponse},
        404: {"model": InternalErrorResponse},
    },
)
async def internal_read_text_asset(
    file_name: str,
    x_internal_token: Optional[str] = Header(
        default=None,
        alias="X-Internal-Token",
    ),
) -> InternalTextAssetReadResponse:
    _verify_internal_token(x_internal_token)
    return _read_text_asset(file_name)


@router.post(
    "/assets/text/write",
    response_model=InternalTextAssetWriteResponse,
    responses={
        400: {"model": InternalErrorResponse},
        401: {"model": InternalErrorResponse},
    },
)
async def internal_write_text_asset(
    payload: InternalTextAssetWriteRequest,
    x_internal_token: Optional[str] = Header(
        default=None,
        alias="X-Internal-Token",
    ),
) -> InternalTextAssetWriteResponse:
    _verify_internal_token(x_internal_token)
    return _write_text_asset(payload)


@public_router.get(
    "/read",
    response_model=InternalTextAssetReadResponse,
    responses={
        400: {"model": InternalErrorResponse},
        404: {"model": InternalErrorResponse},
    },
)
async def read_text_asset(
    file_name: str,
) -> InternalTextAssetReadResponse:
    return _read_text_asset(file_name)


@public_router.post(
    "/write",
    response_model=InternalTextAssetWriteResponse,
    responses={
        400: {"model": InternalErrorResponse},
    },
)
async def write_text_asset(
    payload: InternalTextAssetWriteRequest,
) -> InternalTextAssetWriteResponse:
    return _write_text_asset(payload)


@router.post("/agents/{agent_id}/reload")
async def internal_reload_agent(
    agent_id: str,
    request: Request,
    tenant_id: str = "default",
    source_id: Optional[str] = None,
    x_internal_token: Optional[str] = Header(
        default=None,
        alias="X-Internal-Token",
    ),
):
    """内部服务调用：重载指定 Agent.

    用于 market 服务修改技能配置后通知主服务重载 Agent。
    """
    _verify_internal_token(x_internal_token)

    manager = getattr(request.app.state, "multi_agent_manager", None)
    if manager is None:
        logger.warning("MultiAgentManager not initialized")
        raise HTTPException(status_code=503, detail="Manager not available")

    if not source_id:
        raise HTTPException(status_code=400, detail="source_id is required")
    if not is_valid_identity_value(tenant_id):
        raise HTTPException(status_code=400, detail="Invalid tenant_id")
    if not is_valid_identity_value(source_id):
        raise HTTPException(status_code=400, detail="Invalid source_id")

    scope_id = resolve_scope_id(tenant_id, source_id)
    if scope_id is None:
        raise HTTPException(status_code=400, detail="Failed to resolve scope")

    try:
        await manager.reload_agent(agent_id, tenant_id=scope_id)
        logger.info(
            f"Agent '{agent_id}' (scope={scope_id}) reloaded via internal API",
        )
        return {
            "success": True,
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "source_id": source_id,
            "scope_id": scope_id,
        }
    except Exception as e:
        logger.error(f"Failed to reload agent '{agent_id}': {e}")
        raise HTTPException(status_code=500, detail=str(e))
