# -*- coding: utf-8 -*-
"""Context variables for tenant isolation and workspace directory.

This module provides context variables to pass tenant identity, user identity,
and workspace directory to tool functions, enabling strict tenant isolation
in a multi-tenant environment.
"""

import base64
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger(__name__)

FILE_URL_NETWORK_OFFICE = "office"
FILE_URL_NETWORK_BUSINESS = "business"
_DEFAULT_FILE_URL_BASE = "http://localhost:8088"

# Context variable to store the current tenant ID
current_tenant_id: ContextVar[str | None] = ContextVar(
    "current_tenant_id",
    default=None,
)

# Context variable to store the current user ID
current_user_id: ContextVar[str | None] = ContextVar(
    "current_user_id",
    default=None,
)

# Context variable to store the current agent's workspace directory
current_workspace_dir: ContextVar[Path | None] = ContextVar(
    "current_workspace_dir",
    default=None,
)

# Context variable to store the current source ID (from X-Source-Id header)
current_source_id: ContextVar[str | None] = ContextVar(
    "current_source_id",
    default=None,
)

# Context variable to store the current runtime scope ID
current_scope_id: ContextVar[str | None] = ContextVar(
    "current_scope_id",
    default=None,
)

# 静态文件访问域名需要跟随请求网络环境，默认保持办公网兼容旧行为。
current_file_url_network: ContextVar[str] = ContextVar(
    "current_file_url_network",
    default=FILE_URL_NETWORK_OFFICE,
)

_LEGACY_SCOPE_ID_PREFIX = "scope.v1"
_IDENTITY_MAX_LENGTH = 256


def get_current_tenant_id() -> str | None:
    """Get the current tenant ID from context.

    Returns:
        The current tenant ID, or None if not set.
    """
    return current_tenant_id.get()


def set_current_tenant_id(tenant_id: str | None) -> Token:
    """Set the current tenant ID in context.

    Args:
        tenant_id: The tenant ID to set.

    Returns:
        Token for resetting the context variable.
    """
    return current_tenant_id.set(tenant_id)


def reset_current_tenant_id(token: Token) -> None:
    """Reset the current tenant ID using a token.

    Args:
        token: The token returned by set_current_tenant_id.
    """
    current_tenant_id.reset(token)


def get_current_user_id() -> str | None:
    """Get the current user ID from context.

    Returns:
        The current user ID, or None if not set.
    """
    return current_user_id.get()


def set_current_user_id(user_id: str | None) -> Token:
    """Set the current user ID in context.

    Args:
        user_id: The user ID to set.

    Returns:
        Token for resetting the context variable.
    """
    return current_user_id.set(user_id)


def reset_current_user_id(token: Token) -> None:
    """Reset the current user ID using a token.

    Args:
        token: The token returned by set_current_user_id.
    """
    current_user_id.reset(token)


def get_current_source_id() -> str | None:
    """Get the current source ID from context.

    Returns:
        The current source ID, or None if not set.
    """
    return current_source_id.get()


def get_current_scope_id() -> str | None:
    """Get the current runtime scope ID from context."""
    return current_scope_id.get()


def set_current_scope_id(scope_id: str | None) -> Token:
    """Set the current runtime scope ID in context."""
    return current_scope_id.set(scope_id)


def reset_current_scope_id(token: Token) -> None:
    """Reset the current runtime scope ID using a token."""
    current_scope_id.reset(token)


def normalize_file_url_network(value: Any) -> str:
    """规范化静态文件访问网络，未知值回退到办公网。"""
    network = str(value or "").strip().lower()
    if network in {FILE_URL_NETWORK_OFFICE, FILE_URL_NETWORK_BUSINESS}:
        return network
    if network:
        logger.warning(
            "Invalid file_url_network=%r, fallback to office network",
            value,
        )
    return FILE_URL_NETWORK_OFFICE


def get_current_file_url_network() -> str:
    """读取当前请求的静态文件访问网络。"""
    return current_file_url_network.get()


def set_current_file_url_network(value: Any) -> Token:
    """设置当前请求的静态文件访问网络。"""
    return current_file_url_network.set(normalize_file_url_network(value))


def reset_current_file_url_network(token: Token) -> None:
    """重置当前请求的静态文件访问网络。"""
    current_file_url_network.reset(token)


def _clean_file_url_base(value: str | None) -> str | None:
    """清理静态文件域名配置，空字符串视为未配置。"""
    base = (value or "").strip().rstrip("/")
    return base or None


def resolve_file_url_base(network: Any = None) -> tuple[str, str]:
    """解析静态文件访问域名，并返回实际生效的网络。"""
    requested_network = normalize_file_url_network(
        network if network is not None else get_current_file_url_network(),
    )
    office_base = (
        _clean_file_url_base(os.getenv("FILE_URL_OFFICE"))
        or _clean_file_url_base(os.getenv("FILE_URL"))
        or _DEFAULT_FILE_URL_BASE
    )
    if requested_network == FILE_URL_NETWORK_BUSINESS:
        business_base = _clean_file_url_base(os.getenv("FILE_URL_BUSINESS"))
        if business_base:
            return business_base, FILE_URL_NETWORK_BUSINESS
        logger.warning(
            "FILE_URL_BUSINESS is not configured, "
            "fallback to office network",
        )
    return office_base, FILE_URL_NETWORK_OFFICE


def is_valid_identity_value(identity: str) -> bool:
    """Validate a raw tenant/source identity component."""
    if not identity:
        return False
    if len(identity) < 1 or len(identity) > _IDENTITY_MAX_LENGTH:
        return False
    if ".." in identity or "/" in identity or "\\" in identity:
        return False
    if any(ord(char) < 32 for char in identity):
        return False
    return True


def _encode_scope_component(value: str) -> str:
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode(
        "ascii",
    )
    return encoded.rstrip("=")


def _decode_scope_component(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(
        (value + padding).encode("ascii"),
    ).decode("utf-8")


def encode_scope_id(tenant_id: str, source_id: str) -> str:
    """根据 tenant/source 生成可逆的 canonical runtime scope 标识。"""
    if not is_valid_identity_value(tenant_id):
        raise ValueError("Invalid tenant_id for scope encoding")
    if not is_valid_identity_value(source_id):
        raise ValueError("Invalid source_id for scope encoding")
    return ".".join(
        (
            _encode_scope_component(tenant_id),
            _encode_scope_component(source_id),
        ),
    )


def decode_scope_id(scope_id: str) -> tuple[str, str]:
    """把新旧两种 scope 标识还原为 logical tenant/source。"""
    prefix = f"{_LEGACY_SCOPE_ID_PREFIX}."
    payload = (
        scope_id[len(prefix) :] if scope_id.startswith(prefix) else scope_id
    )
    parts = payload.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("Invalid scope_id payload")
    return (
        _decode_scope_component(parts[0]),
        _decode_scope_component(parts[1]),
    )


def canonicalize_scope_id(scope_id: str) -> str:
    """把 legacy scope 输入统一收敛为 canonical 无前缀格式。"""
    tenant_id, source_id = decode_scope_id(scope_id)
    return encode_scope_id(tenant_id, source_id)


def resolve_scope_id(
    tenant_id: str | None,
    source_id: str | None,
) -> str | None:
    """在 tenant/source 同时存在时生成 canonical runtime scope 标识。"""
    if tenant_id is None or source_id is None:
        return None
    return encode_scope_id(tenant_id, source_id)


def _decode_runtime_scope_identity(
    runtime_tenant_id: str,
    source_id: str | None,
) -> tuple[str, str, str] | None:
    """在 source 未冲突时，把 runtime scope 还原为逻辑身份。"""
    try:
        tenant_id, decoded_source_id = decode_scope_id(runtime_tenant_id)
    except ValueError:
        return None
    if source_id is not None and source_id != decoded_source_id:
        return None
    return (
        tenant_id,
        decoded_source_id,
        canonicalize_scope_id(runtime_tenant_id),
    )


def resolve_runtime_tenant_id(
    tenant_id: str | None,
    source_id: str | None,
) -> str | None:
    """解析运行时存储和工作区使用的租户标识。

    该函数需要同时兼容逻辑租户和已编码的 scope。调用方可能已经通过
    middleware 解析过 ``scope_id``，这里必须保持幂等，避免把 scope 当作
    普通租户再次和 ``source_id`` 组合编码。
    """
    if tenant_id is None:
        return None
    if tenant_id == "default":
        if not source_id:
            return "default"
        return f"default_{source_id}"
    if tenant_id.startswith("default_"):
        return tenant_id
    legacy_prefix = f"{_LEGACY_SCOPE_ID_PREFIX}."
    if tenant_id.startswith(legacy_prefix):
        return canonicalize_scope_id(tenant_id)
    decoded_identity = _decode_runtime_scope_identity(
        tenant_id,
        source_id,
    )
    if decoded_identity is not None:
        return decoded_identity[2]
    scope_id = resolve_scope_id(tenant_id, source_id)
    if scope_id is not None:
        return scope_id
    return tenant_id


def resolve_storage_tenant_id(
    tenant_id: str | None,
    source_id: str | None,
    *,
    scope_id: str | None = None,
) -> str | None:
    """解析配置、目录与模板资产使用的存储租户标识。

    这里显式区分 runtime 与 storage 两种语义：

    - default + source：固定落到 ``default_{source}`` 模板目录
    - 非 default + source：保持当前 runtime scope 目录策略
    - 历史/显式 scope：继续 canonicalize 兼容

    Args:
        tenant_id: 逻辑 tenant 或已编码 scope。
        source_id: 来源标识。
        scope_id: 可选显式 runtime scope。

    Returns:
        用于目录和配置访问的存储租户标识。
    """
    if tenant_id is None:
        return None

    if tenant_id == "default":
        if not source_id:
            return "default"
        return f"default_{source_id}"
    if tenant_id.startswith("default_"):
        return tenant_id

    resolved_scope_id = scope_id
    if resolved_scope_id is not None:
        return canonicalize_scope_id(resolved_scope_id)

    legacy_prefix = f"{_LEGACY_SCOPE_ID_PREFIX}."
    if tenant_id.startswith(legacy_prefix):
        return canonicalize_scope_id(tenant_id)

    decoded_identity = _decode_runtime_scope_identity(
        tenant_id,
        source_id,
    )
    if decoded_identity is not None:
        return decoded_identity[2]

    runtime_tenant_id = resolve_runtime_tenant_id(tenant_id, source_id)
    return runtime_tenant_id or tenant_id


def resolve_scope_preferred_tenant_id(
    tenant_id: str | None,
    source_id: str | None = None,
    scope_id: str | None = None,
) -> str | None:
    """统一解析运行时隔离键，优先使用显式 scope_id。"""
    if scope_id is not None:
        return canonicalize_scope_id(scope_id)
    return resolve_runtime_tenant_id(tenant_id, source_id)


def resolve_request_effective_tenant_id(
    tenant_id: str | None,
    source_id: str | None = None,
    scope_id: str | None = None,
) -> str | None:
    """解析请求应访问的有效租户目录。

    规则：
    - ``default + source`` 固定访问 ``default_{source}``
    - 其他租户继续优先使用显式/当前 ``scope_id``
    - 无 ``source`` 时保持既有 runtime 解析行为
    """
    if tenant_id is None:
        return None

    if tenant_id == "default":
        return resolve_storage_tenant_id(tenant_id, source_id)

    return resolve_scope_preferred_tenant_id(
        tenant_id,
        source_id,
        scope_id,
    )


def resolve_runtime_identity(
    runtime_tenant_id: str | None,
    source_id: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """展开运行时身份，返回 logical tenant、source 与 scope。

    Args:
        runtime_tenant_id: 运行时租户标识，既可能是逻辑 tenant，
            也可能已经是编码后的 ``scope_id``。
        source_id: 显式来源标识；当 ``runtime_tenant_id`` 仍是逻辑
            tenant 时用于补齐 ``scope_id``；若与已编码 scope
            中的 source 不一致，则按 raw tenant 处理。

    Returns:
        ``(tenant_id, source_id, scope_id)`` 三元组。若传入的
        ``runtime_tenant_id`` 已经是编码后的 scope，则会优先解码并
        返回原始 tenant/source，避免后台任务只携带 opaque key。
    """
    if runtime_tenant_id is None:
        return (None, source_id, None)

    decoded_identity = _decode_runtime_scope_identity(
        runtime_tenant_id,
        source_id,
    )
    if decoded_identity is not None:
        return decoded_identity

    if source_id is not None:
        return (
            runtime_tenant_id,
            source_id,
            resolve_scope_id(runtime_tenant_id, source_id),
        )
    return (runtime_tenant_id, source_id, None)


def set_current_source_id(source_id: str | None) -> Token:
    """Set the current source ID in context.

    Args:
        source_id: The source ID to set.

    Returns:
        Token for resetting the context variable.
    """
    return current_source_id.set(source_id)


def reset_current_source_id(token: Token) -> None:
    """Reset the current source ID using a token.

    Args:
        token: The token returned by set_current_source_id.
    """
    current_source_id.reset(token)


def get_current_workspace_dir() -> Path | None:
    """Get the current agent's workspace directory from context.

    Returns:
        Path to the current agent's workspace directory, or None if not set.
    """
    return current_workspace_dir.get()


def get_current_effective_tenant_id() -> str | None:
    """Get the current runtime tenant ID with default+source isolation."""
    scope_id = get_current_scope_id()
    if scope_id is not None:
        return scope_id
    return resolve_runtime_tenant_id(
        get_current_tenant_id(),
        get_current_source_id(),
    )


def set_current_workspace_dir(workspace_dir: Path | None) -> Token:
    """Set the current agent's workspace directory in context.

    Args:
        workspace_dir: Path to the agent's workspace directory.

    Returns:
        Token for resetting the context variable.
    """
    return current_workspace_dir.set(workspace_dir)


def reset_current_workspace_dir(token: Token) -> None:
    """Reset the current workspace directory using a token.

    Args:
        token: The token returned by set_current_workspace_dir.
    """
    current_workspace_dir.reset(token)


class TenantContextError(RuntimeError):
    """Raised when tenant context is required but not available."""


def get_current_tenant_id_strict() -> str:
    """Get the current tenant ID, raising if not set.

    Returns:
        The current tenant ID.

    Raises:
        TenantContextError: If tenant ID is not set in context.
    """
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise TenantContextError(
            "Tenant ID is not set in context. "
            "Ensure this code runs within a tenant-scoped request or context.",
        )
    return tenant_id


def get_current_user_id_strict() -> str:
    """Get the current user ID, raising if not set.

    Returns:
        The current user ID.

    Raises:
        TenantContextError: If user ID is not set in context.
    """
    user_id = current_user_id.get()
    if user_id is None:
        raise TenantContextError(
            "User ID is not set in context. "
            "Ensure this code runs within a tenant-scoped request or context.",
        )
    return user_id


def get_current_workspace_dir_strict() -> Path:
    """Get the current workspace directory, raising if not set.

    Returns:
        Path to the current workspace directory.

    Raises:
        TenantContextError: If workspace directory is not set in context.
    """
    workspace_dir = current_workspace_dir.get()
    if workspace_dir is None:
        raise TenantContextError(
            "Workspace directory is not set in context. "
            "Ensure this code runs within a tenant-scoped request or context.",
        )
    return workspace_dir


@contextmanager
def tenant_context(
    tenant_id: str | None = None,
    user_id: str | None = None,
    workspace_dir: Path | None = None,
    source_id: str | None = None,
    scope_id: str | None = None,
) -> Generator[None, None, None]:
    """Context manager for binding tenant context.

    Temporarily sets tenant_id, user_id, workspace_dir, source_id, and
    scope_id
    in context, restoring previous values on exit.

    Args:
        tenant_id: The tenant ID to set.
        user_id: The user ID to set.
        workspace_dir: The workspace directory to set.
        source_id: The source ID to set.
        scope_id: The runtime scope ID to set. If omitted, resolve from
            tenant_id/source_id when possible.

    Yields:
        None

    Example:
        with tenant_context(tenant_id="acme", user_id="alice"):
            # Code here has access to tenant context
            process_request()
        # Context restored after exit
    """
    tokens: list[tuple[str, Token[Any]]] = []
    resolved_scope_id = (
        canonicalize_scope_id(scope_id)
        if scope_id is not None
        else resolve_scope_id(tenant_id, source_id)
    )
    try:
        if tenant_id is not None:
            tokens.append(("tenant", current_tenant_id.set(tenant_id)))
        if user_id is not None:
            tokens.append(("user", current_user_id.set(user_id)))
        if workspace_dir is not None:
            tokens.append(
                ("workspace", current_workspace_dir.set(workspace_dir)),
            )
        if source_id is not None:
            tokens.append(("source", current_source_id.set(source_id)))
        if resolved_scope_id is not None:
            tokens.append(("scope", current_scope_id.set(resolved_scope_id)))
        yield
    finally:
        for name, token in reversed(tokens):
            if name == "tenant":
                current_tenant_id.reset(token)
            elif name == "user":
                current_user_id.reset(token)
            elif name == "workspace":
                current_workspace_dir.reset(token)
            elif name == "source":
                current_source_id.reset(token)
            elif name == "scope":
                current_scope_id.reset(token)


# Context variable to store the recent_max_bytes limit
current_recent_max_bytes: ContextVar[int | None] = ContextVar(
    "current_recent_max_bytes",
    default=None,
)


def get_current_recent_max_bytes() -> int | None:
    """Get the current agent's recent_max_bytes limit from context.

    Returns:
        Byte limit for recent tool output truncation, or None if not set.
    """
    return current_recent_max_bytes.get()


def set_current_recent_max_bytes(max_bytes: int | None) -> None:
    """Set the current agent's recent_max_bytes limit in context.

    Args:
        max_bytes: Byte limit for recent tool output truncation.
    """
    current_recent_max_bytes.set(max_bytes)


# Context variable to store request-level passthrough headers for MCP
current_passthrough_headers: ContextVar[dict[str, str] | None] = ContextVar(
    "current_passthrough_headers",
    default=None,
)

# Context variables for explicit task progress updates
current_task_progress_tracker: ContextVar[Any | None] = ContextVar(
    "current_task_progress_tracker",
    default=None,
)
current_task_progress_chat_id: ContextVar[str | None] = ContextVar(
    "current_task_progress_chat_id",
    default=None,
)
current_task_progress_turn_id: ContextVar[str | None] = ContextVar(
    "current_task_progress_turn_id",
    default=None,
)


def get_current_passthrough_headers() -> dict[str, str] | None:
    """Get current passthrough headers from context.

    These headers are extracted from x-header-* HTTP headers and
    will be merged into MCP client HTTP requests.

    Returns:
        Dictionary of headers to passthrough, or None if not set.
    """
    return current_passthrough_headers.get()


def set_current_passthrough_headers(headers: dict[str, str] | None) -> Token:
    """Set current passthrough headers in context.

    Args:
        headers: Dictionary of headers to passthrough to MCP servers.

    Returns:
        Token for resetting the context variable.
    """
    return current_passthrough_headers.set(headers)


def reset_current_passthrough_headers(token: Token) -> None:
    """Reset passthrough headers using token.

    Args:
        token: The token returned by set_current_passthrough_headers.
    """
    current_passthrough_headers.reset(token)


def get_current_task_progress_tracker() -> Any | None:
    """Get current task progress tracker from context."""
    return current_task_progress_tracker.get()


def set_current_task_progress_tracker(tracker: Any | None) -> Token:
    """Set current task progress tracker in context."""
    return current_task_progress_tracker.set(tracker)


def reset_current_task_progress_tracker(token: Token) -> None:
    """Reset current task progress tracker using token."""
    current_task_progress_tracker.reset(token)


def get_current_task_progress_chat_id() -> str | None:
    """Get current task progress chat id from context."""
    return current_task_progress_chat_id.get()


def set_current_task_progress_chat_id(chat_id: str | None) -> Token:
    """Set current task progress chat id in context."""
    return current_task_progress_chat_id.set(chat_id)


def reset_current_task_progress_chat_id(token: Token) -> None:
    """Reset current task progress chat id using token."""
    current_task_progress_chat_id.reset(token)


def get_current_task_progress_turn_id() -> str | None:
    """Get current task progress turn id from context."""
    return current_task_progress_turn_id.get()


def set_current_task_progress_turn_id(turn_id: str | None) -> Token:
    """Set current task progress turn id in context."""
    return current_task_progress_turn_id.set(turn_id)


def reset_current_task_progress_turn_id(token: Token) -> None:
    """Reset current task progress turn id using token."""
    current_task_progress_turn_id.reset(token)


def resolve_effective_tenant_id(
    tenant_id: str,
    source_id: str | None,
) -> str:
    """兼容旧调用方的 runtime tenant 解析别名。

    新运行时统一使用编码后的 scope_id；保留该函数仅用于兼容历史导入，
    避免调用方通过旧名称重新引入 ``default_{source}`` 目录语义。
    """
    return resolve_runtime_tenant_id(tenant_id, source_id) or tenant_id


def capture_summary_context_values() -> list[tuple[ContextVar, Any]]:
    """Snapshot only runtime state required by detached summaries."""
    from swe.app.source_system_config.runtime import (
        capture_source_system_config_context_value,
    )
    from swe.config.llm_workload import capture_llm_workload_context_value

    variables: tuple[ContextVar[Any], ...] = (
        current_tenant_id,
        current_source_id,
        current_scope_id,
        current_user_id,
        current_workspace_dir,
        current_file_url_network,
    )
    values = [(variable, variable.get()) for variable in variables]
    headers = current_passthrough_headers.get()
    values.append(
        (
            current_passthrough_headers,
            dict(headers) if headers is not None else None,
        ),
    )
    values.append(capture_llm_workload_context_value())
    values.append(capture_source_system_config_context_value())
    return values
