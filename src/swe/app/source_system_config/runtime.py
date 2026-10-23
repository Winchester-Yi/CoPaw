# -*- coding: utf-8 -*-
"""Source 系统配置请求上下文。"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Generator

from swe.config.config import QueryRetryConfig, ToolResultCompactConfig
from swe.providers.retry_chat_model import RateLimitConfig

from .registry import (
    ARCHIVE_MAINTENANCE_CRON_SETTING,
    ARCHIVE_MAINTENANCE_ENABLED_SETTING,
    ARCHIVE_MAINTENANCE_MAX_FILES_PER_RUN_SETTING,
    ARCHIVE_MAINTENANCE_MAX_FILES_PER_WORKSPACE_SETTING,
    ARCHIVE_MAINTENANCE_MAX_WORKSPACES_PER_RUN_SETTING,
    ARCHIVE_MAINTENANCE_OLD_ORPHAN_DAYS_SETTING,
    ARCHIVE_MAINTENANCE_TIMEOUT_SECONDS_SETTING,
    APPROVAL_NOTIFICATIONS_ZHAOHU_TOOL_GUARD_ENABLED_SETTING,
    CRON_NOTIFICATIONS_SKIP_WEEKEND_ZHAOHU_ENABLED_SETTING,
    CRON_TASK_SESSION_CLEANUP_CRON_SETTING,
    CRON_TASK_SESSION_CLEANUP_ENABLED_SETTING,
    CRON_TASK_SESSION_CLEANUP_RETENTION_DAYS_SETTING,
    CRON_UNREAD_AUTO_PAUSE_ENABLED_SETTING,
    CRON_UNREAD_AUTO_PAUSE_THRESHOLD_SETTING,
    LLM_ACQUIRE_TIMEOUT_SETTING,
    LLM_CHAT_ACQUIRE_TIMEOUT_SETTING,
    LLM_CHAT_MAX_CONCURRENT_SETTING,
    LLM_CRON_ACQUIRE_TIMEOUT_SETTING,
    LLM_CRON_MAX_CONCURRENT_SETTING,
    LLM_MAX_CONCURRENT_SETTING,
    LLM_MAX_QPM_SETTING,
    LLM_RATE_LIMIT_JITTER_SETTING,
    LLM_RATE_LIMIT_PAUSE_SETTING,
    QUERY_RETRY_BACKOFF_BASE_SETTING,
    QUERY_RETRY_BACKOFF_CAP_SETTING,
    QUERY_RETRY_ENABLED_SETTING,
    QUERY_RETRY_MAX_RETRIES_SETTING,
    get_system_prompt_injections as _get_system_prompt_injections,
    merge_source_system_config_with_defaults,
    normalize_registered_setting_values,
)
from .models import EffectiveSourceSystemConfig

_current_source_system_config: ContextVar[
    EffectiveSourceSystemConfig | None
] = ContextVar("current_source_system_config", default=None)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CronUnreadAutoPauseConfig:
    """定时任务未读自动暂停的运行时配置。"""

    enabled: bool
    threshold: int


@dataclass(frozen=True)
class CronTaskSessionCleanupConfig:
    """定时任务会话历史清理的运行时配置。"""

    enabled: bool
    retention_days: int
    cron: str


@dataclass(frozen=True)
class ArchiveMaintenanceConfig:
    """source 绾ф枃浠跺綊妗ｇ淮鎶ょ殑杩愯鏃堕厤缃€?"""

    enabled: bool
    cron: str
    old_orphan_days: int
    max_workspaces_per_run: int
    max_files_per_workspace: int
    max_files_per_run: int
    timeout_seconds: int


_RATE_LIMIT_SOURCE_TO_RUNTIME_FIELDS = {
    "llm_max_concurrent": "max_concurrent",
    "llm_chat_max_concurrent": "chat_max_concurrent",
    "llm_cron_max_concurrent": "cron_max_concurrent",
    "llm_max_qpm": "max_qpm",
    "llm_rate_limit_pause": "pause_seconds",
    "llm_rate_limit_jitter": "jitter_range",
    "llm_acquire_timeout": "acquire_timeout",
    "llm_chat_acquire_timeout": "chat_acquire_timeout",
    "llm_cron_acquire_timeout": "cron_acquire_timeout",
}


@dataclass(frozen=True)
class CronNotificationConfig:
    """Runtime config for scheduled-task completion notifications."""

    skip_weekend_zhaohu_enabled: bool


@contextmanager
def bind_source_system_config(
    config: EffectiveSourceSystemConfig,
) -> Generator[None, None, None]:
    """在当前执行上下文绑定 source 系统配置。"""
    token = _current_source_system_config.set(config)
    try:
        yield
    finally:
        _current_source_system_config.reset(token)


def set_current_source_system_config(
    config: EffectiveSourceSystemConfig,
) -> Token[EffectiveSourceSystemConfig | None]:
    """设置当前 source 系统配置并返回 reset token。"""
    return _current_source_system_config.set(config)


def reset_current_source_system_config(
    token: Token[EffectiveSourceSystemConfig | None],
) -> None:
    """使用 token 还原当前 source 系统配置。"""
    _current_source_system_config.reset(token)


def get_current_source_system_config() -> EffectiveSourceSystemConfig | None:
    """读取当前上下文中的 source 系统配置。"""
    return _current_source_system_config.get()


def capture_source_system_config_context_value() -> tuple[
    ContextVar[EffectiveSourceSystemConfig | None],
    EffectiveSourceSystemConfig | None,
]:
    """Capture an independently owned source config snapshot for a child task."""
    config = _current_source_system_config.get()
    return _current_source_system_config, (
        config.model_copy(deep=True) if config is not None else None
    )


def resolve_tool_result_compact_config(
    base_config: ToolResultCompactConfig,
    source_config: Any | None = None,
) -> ToolResultCompactConfig:
    """合成 Agent 运行配置和当前 source 的显式工具结果压缩覆盖。"""
    source_payload = _extract_tool_result_compact_override(source_config)
    if not source_payload:
        return base_config.model_copy(deep=True)

    payload = base_config.model_dump(mode="python")
    payload.update(source_payload)
    if payload["recent_max_bytes"] < payload["old_max_bytes"]:
        logger.warning(
            "Invalid source tool_result_compact thresholds resolved for "
            "source %s: recent_max_bytes=%s, old_max_bytes=%s; adjusted "
            "recent_max_bytes to old_max_bytes",
            _get_source_config_id(source_config),
            payload["recent_max_bytes"],
            payload["old_max_bytes"],
        )
        payload["recent_max_bytes"] = payload["old_max_bytes"]
    return ToolResultCompactConfig.model_validate(payload)


def resolve_query_retry_config(
    base_config: QueryRetryConfig | Any,
    source_config: Any | None = None,
) -> QueryRetryConfig:
    """合成 Agent 运行配置和当前 source 的显式 Query 重试覆盖。"""
    base = _normalize_query_retry_config(base_config)
    source_payload = _extract_registered_section_override(
        "query_retry",
        source_config,
    )
    if not source_payload:
        return base.model_copy(deep=True)

    payload = base.model_dump(mode="python")
    payload.update(source_payload)
    if payload["backoff_cap"] < payload["backoff_base"]:
        logger.warning(
            "Invalid source query_retry backoff resolved for source %s: "
            "backoff_cap=%s, backoff_base=%s; adjusted backoff_cap to "
            "backoff_base",
            _get_source_config_id(source_config),
            payload["backoff_cap"],
            payload["backoff_base"],
        )
        payload["backoff_cap"] = payload["backoff_base"]
    return QueryRetryConfig.model_validate(payload)


def resolve_llm_rate_limiter_config(
    base_config: RateLimitConfig,
    source_config: Any | None = None,
) -> RateLimitConfig:
    """合成 Agent 运行配置和当前 source 的显式 LLM 限流覆盖。"""
    source_payload = _extract_registered_section_override(
        "llm_rate_limiter",
        source_config,
    )
    if not source_payload:
        return RateLimitConfig(
            max_concurrent=base_config.max_concurrent,
            chat_max_concurrent=base_config.chat_max_concurrent,
            cron_max_concurrent=base_config.cron_max_concurrent,
            max_qpm=base_config.max_qpm,
            pause_seconds=base_config.pause_seconds,
            jitter_range=base_config.jitter_range,
            acquire_timeout=base_config.acquire_timeout,
            chat_acquire_timeout=base_config.chat_acquire_timeout,
            cron_acquire_timeout=base_config.cron_acquire_timeout,
        )

    payload = {
        "max_concurrent": base_config.max_concurrent,
        "chat_max_concurrent": base_config.chat_max_concurrent,
        "cron_max_concurrent": base_config.cron_max_concurrent,
        "max_qpm": base_config.max_qpm,
        "pause_seconds": base_config.pause_seconds,
        "jitter_range": base_config.jitter_range,
        "acquire_timeout": base_config.acquire_timeout,
        "chat_acquire_timeout": base_config.chat_acquire_timeout,
        "cron_acquire_timeout": base_config.cron_acquire_timeout,
    }
    for source_key, value in source_payload.items():
        runtime_key = _RATE_LIMIT_SOURCE_TO_RUNTIME_FIELDS.get(source_key)
        if runtime_key is not None:
            payload[runtime_key] = value
    _adjust_rate_limiter_timeouts(payload, source_config)
    return RateLimitConfig(**payload)


def resolve_cron_unread_auto_pause_config(
    source_config: Any | None = None,
) -> CronUnreadAutoPauseConfig:
    """解析当前 source 的定时任务未读自动暂停配置。"""
    raw_config = _extract_config_payload(source_config)
    merged = merge_source_system_config_with_defaults(raw_config)
    raw_section = merged.get("cron_unread_auto_pause")
    section = raw_section if isinstance(raw_section, dict) else {}
    normalized = normalize_registered_setting_values(
        {"cron_unread_auto_pause": section},
    )
    normalized_section = normalized.get("cron_unread_auto_pause")
    if not isinstance(normalized_section, dict):
        normalized_section = {}
    return CronUnreadAutoPauseConfig(
        enabled=bool(
            normalized_section.get(
                "enabled",
                CRON_UNREAD_AUTO_PAUSE_ENABLED_SETTING.default_value,
            ),
        ),
        threshold=int(
            normalized_section.get(
                "threshold",
                CRON_UNREAD_AUTO_PAUSE_THRESHOLD_SETTING.default_value,
            ),
        ),
    )


def resolve_cron_task_session_cleanup_config(
    source_config: Any | None = None,
) -> CronTaskSessionCleanupConfig:
    """解析当前 source 的定时任务会话历史清理配置。"""
    raw_config = _extract_config_payload(source_config)
    merged = merge_source_system_config_with_defaults(raw_config)
    raw_section = merged.get("cron_task_session_cleanup")
    section = raw_section if isinstance(raw_section, dict) else {}
    normalized = normalize_registered_setting_values(
        {"cron_task_session_cleanup": section},
    )
    normalized_section = normalized.get("cron_task_session_cleanup")
    if not isinstance(normalized_section, dict):
        normalized_section = {}
    return CronTaskSessionCleanupConfig(
        enabled=bool(
            normalized_section.get(
                "enabled",
                CRON_TASK_SESSION_CLEANUP_ENABLED_SETTING.default_value,
            ),
        ),
        retention_days=int(
            normalized_section.get(
                "retention_days",
                CRON_TASK_SESSION_CLEANUP_RETENTION_DAYS_SETTING.default_value,
            ),
        ),
        cron=str(
            normalized_section.get(
                "cron",
                CRON_TASK_SESSION_CLEANUP_CRON_SETTING.default_value,
            ),
        ),
    )


def resolve_archive_maintenance_config(
    source_config: Any | None = None,
) -> ArchiveMaintenanceConfig:
    """瑙ｆ瀽褰撳墠 source 鐨勬枃浠跺綊妗ｇ淮鎶ら厤缃€?"""
    raw_config = _extract_config_payload(source_config)
    merged = merge_source_system_config_with_defaults(raw_config)
    raw_section = merged.get("archive_maintenance")
    section = raw_section if isinstance(raw_section, dict) else {}
    normalized = normalize_registered_setting_values(
        {"archive_maintenance": section},
    )
    normalized_section = normalized.get("archive_maintenance")
    if not isinstance(normalized_section, dict):
        normalized_section = {}
    return ArchiveMaintenanceConfig(
        enabled=bool(
            normalized_section.get(
                "enabled",
                ARCHIVE_MAINTENANCE_ENABLED_SETTING.default_value,
            ),
        ),
        cron=str(
            normalized_section.get(
                "cron",
                ARCHIVE_MAINTENANCE_CRON_SETTING.default_value,
            ),
        ),
        old_orphan_days=int(
            normalized_section.get(
                "old_orphan_days",
                ARCHIVE_MAINTENANCE_OLD_ORPHAN_DAYS_SETTING.default_value,
            ),
        ),
        max_workspaces_per_run=int(
            normalized_section.get(
                "max_workspaces_per_run",
                ARCHIVE_MAINTENANCE_MAX_WORKSPACES_PER_RUN_SETTING.default_value,
            ),
        ),
        max_files_per_workspace=int(
            normalized_section.get(
                "max_files_per_workspace",
                ARCHIVE_MAINTENANCE_MAX_FILES_PER_WORKSPACE_SETTING.default_value,
            ),
        ),
        max_files_per_run=int(
            normalized_section.get(
                "max_files_per_run",
                ARCHIVE_MAINTENANCE_MAX_FILES_PER_RUN_SETTING.default_value,
            ),
        ),
        timeout_seconds=int(
            normalized_section.get(
                "timeout_seconds",
                ARCHIVE_MAINTENANCE_TIMEOUT_SECONDS_SETTING.default_value,
            ),
        ),
    )


def resolve_cron_notification_config(
    source_config: Any | None = None,
) -> CronNotificationConfig:
    """Resolve scheduled-task completion notification runtime config."""
    raw_config = _extract_config_payload(source_config)
    merged = merge_source_system_config_with_defaults(raw_config)
    raw_section = merged.get("cron_notifications")
    section = raw_section if isinstance(raw_section, dict) else {}
    normalized = normalize_registered_setting_values(
        {"cron_notifications": section},
    )
    normalized_section = normalized.get("cron_notifications")
    if not isinstance(normalized_section, dict):
        normalized_section = {}
    return CronNotificationConfig(
        skip_weekend_zhaohu_enabled=bool(
            normalized_section.get(
                "skip_weekend_zhaohu_enabled",
                (
                    CRON_NOTIFICATIONS_SKIP_WEEKEND_ZHAOHU_ENABLED_SETTING.default_value
                ),
            ),
        ),
    )


def is_zhaohu_tool_guard_notification_enabled(
    source_config: Any | None = None,
) -> bool:
    """Return whether Tool Guard approvals should notify zhaohu."""
    raw_config = _extract_config_payload(source_config)
    merged = merge_source_system_config_with_defaults(raw_config)
    raw_section = merged.get("approval_notifications")
    section = raw_section if isinstance(raw_section, dict) else {}
    normalized = normalize_registered_setting_values(
        {"approval_notifications": section},
    )
    normalized_section = normalized.get("approval_notifications")
    default_value = (
        APPROVAL_NOTIFICATIONS_ZHAOHU_TOOL_GUARD_ENABLED_SETTING.default_value
    )
    if not isinstance(normalized_section, dict):
        return bool(default_value)
    return bool(
        normalized_section.get(
            "zhaohu_tool_guard_enabled",
            default_value,
        ),
    )


def get_system_prompt_injections(
    source_config: Any | None = None,
) -> list[str]:
    """读取当前 source 的系统提示词注入配置。"""
    config = (
        get_current_source_system_config()
        if source_config is None
        else source_config
    )
    return _get_system_prompt_injections(config)


def _get_source_config_id(source_config: Any | None) -> str:
    """尽量提取 source 标识，日志缺少上下文时回退为 unknown。"""
    config = (
        get_current_source_system_config()
        if source_config is None
        else source_config
    )
    source_id = getattr(config, "source_id", None)
    if isinstance(source_id, str) and source_id:
        return source_id
    return "unknown"


def _extract_tool_result_compact_override(
    source_config: Any | None,
) -> dict[str, Any]:
    """只读取 raw source 配置，避免 registered default 覆盖 Agent 配置。"""
    payload = _extract_raw_config_payload(source_config)
    if not payload:
        return {}

    tool_result_compact = payload.get("tool_result_compact")
    if not isinstance(tool_result_compact, dict):
        return {}
    normalized = normalize_registered_setting_values(
        {"tool_result_compact": tool_result_compact},
    )
    normalized_tool_result = normalized.get("tool_result_compact")
    if not isinstance(normalized_tool_result, dict):
        return {}
    return normalized_tool_result


def _extract_registered_section_override(
    section: str,
    source_config: Any | None,
) -> dict[str, Any]:
    """只读取 raw source 配置中的注册 section 覆盖。"""
    payload = _extract_raw_config_payload(source_config)
    if not payload:
        return {}
    raw_section = payload.get(section)
    if not isinstance(raw_section, dict):
        return {}
    normalized = normalize_registered_setting_values({section: raw_section})
    normalized_section = normalized.get(section)
    if not isinstance(normalized_section, dict):
        return {}
    return normalized_section


def _normalize_query_retry_config(
    base_config: QueryRetryConfig | Any,
) -> QueryRetryConfig:
    """兼容模型对象、dict 和测试替身，统一为 QueryRetryConfig。"""
    if isinstance(base_config, QueryRetryConfig):
        return base_config
    if isinstance(base_config, dict):
        return QueryRetryConfig.model_validate(base_config)
    return QueryRetryConfig(
        enabled=bool(getattr(base_config, "enabled", False)),
        max_retries=int(getattr(base_config, "max_retries", 0)),
        backoff_base=float(getattr(base_config, "backoff_base", 2.0)),
        backoff_cap=float(getattr(base_config, "backoff_cap", 30.0)),
    )


def _adjust_rate_limiter_timeouts(
    payload: dict[str, Any],
    source_config: Any | None,
) -> None:
    """Keep inherited/acquired timeout values above the resolved cooldown."""
    cooldown = float(payload["pause_seconds"]) + float(payload["jitter_range"])
    for key in (
        "acquire_timeout",
        "chat_acquire_timeout",
        "cron_acquire_timeout",
    ):
        value = payload.get(key)
        if value is None or value > cooldown:
            continue
        adjusted = cooldown + 1.0
        logger.warning(
            "Invalid source llm_rate_limiter timeout resolved for source %s: "
            "%s=%s, cooldown=%s; adjusted %s to %s",
            _get_source_config_id(source_config),
            key,
            value,
            cooldown,
            key,
            adjusted,
        )
        payload[key] = adjusted


def _extract_raw_config_payload(source_config: Any | None) -> dict[str, Any]:
    """从显式 raw 配置中提取 dict，避免 effective defaults 混入覆盖判断。"""
    config = (
        get_current_source_system_config()
        if source_config is None
        else source_config
    )
    if config is None:
        return {}
    if isinstance(config, EffectiveSourceSystemConfig):
        raw_config = config.raw_config
        if raw_config is None:
            return {}
        return _extract_raw_config_payload(raw_config)
    if hasattr(config, "raw_config"):
        raw_config = getattr(config, "raw_config")
        if raw_config is None:
            return {}
        return _extract_raw_config_payload(raw_config)
    if hasattr(config, "as_dict"):
        return config.as_dict()
    if hasattr(config, "config") and not isinstance(config, dict):
        return _extract_raw_config_payload(getattr(config, "config"))
    if isinstance(config, dict):
        return dict(config)
    return {}


def _extract_config_payload(source_config: Any | None) -> dict[str, Any]:
    """读取可包含默认值的 effective 配置，缺省时返回空对象。"""
    config = (
        get_current_source_system_config()
        if source_config is None
        else source_config
    )
    if config is None:
        return {}
    if isinstance(config, EffectiveSourceSystemConfig):
        return config.config.as_dict()
    if hasattr(config, "config") and not isinstance(config, dict):
        return _extract_config_payload(getattr(config, "config"))
    if hasattr(config, "as_dict"):
        return config.as_dict()
    if isinstance(config, dict):
        return dict(config)
    return {}
