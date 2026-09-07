# -*- coding: utf-8 -*-
"""Dream logs API router.

Provides REST API endpoints for dream optimization records.
"""

import asyncio
import hashlib
import json
import logging
import shutil
import base64
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

from ..continuous_governance.models import GOVERNANCE_ID_MAX_LENGTH

router = APIRouter(prefix="/dream-logs", tags=["dream-logs"])
logger = logging.getLogger(__name__)
TARGET_AGENT_ID_MAX_LENGTH = GOVERNANCE_ID_MAX_LENGTH


def _health_batch_entity_id(prefix: str, values: list[str]) -> str:
    """为批量待对账项生成长度稳定的实体标识。"""
    digest = hashlib.sha256(
        "\n".join(sorted(str(value) for value in values)).encode("utf-8"),
    ).hexdigest()[:24]
    return f"{prefix}:{digest}"


@dataclass
class DreamArchiveMaintenanceResult:
    """dream 后置文件治理维护产生的结构化变更。"""

    archived_items: list[Any] = field(default_factory=list)
    purged_archive_item_ids: list[str] = field(default_factory=list)
    purged_paths: list[str] = field(default_factory=list)
    purged_size_bytes: int = 0


# 治理任务运行状态（模块级共享，兼容线程+协程）
_current_run_lock = threading.Lock()
_current_run: dict = {
    "running": False,
    "started_at": None,
    "trigger": None,
}


def _set_running(trigger: str) -> None:
    """标记治理任务开始运行"""
    with _current_run_lock:
        _current_run["running"] = True
        _current_run["started_at"] = datetime.now(timezone.utc).isoformat()
        _current_run["trigger"] = trigger


def _clear_running() -> None:
    """标记治理任务运行结束"""
    with _current_run_lock:
        _current_run["running"] = False
        _current_run["started_at"] = None
        _current_run["trigger"] = None


def _get_running_status() -> dict:
    """获取当前运行状态（线程安全）"""
    with _current_run_lock:
        return dict(_current_run)


# ------------------------------------------------------------------
# Response models
# ------------------------------------------------------------------


class FileStats(BaseModel):
    """File statistics before and after optimization."""

    size_before: int
    size_after: int
    size_saved: int
    lines_before: int
    lines_after: int
    lines_removed: int
    backup_path: str


class DreamLogRecord(BaseModel):
    """Single dream optimization record."""

    id: str
    timestamp: str
    trigger: str  # "cron" or "manual"
    status: str  # "success", "failed", "rollback"
    files_optimized: list[str]
    file_stats: dict[str, FileStats]
    total_size_saved: int
    total_files_changed: int
    duration_ms: int
    model_used: str
    input_tokens: int
    output_tokens: int
    summary: str
    error: Optional[str] = None


class DreamLogsStats(BaseModel):
    """Aggregate statistics for dream optimization."""

    total_executions: int
    success_count: int
    failed_count: int
    total_size_saved: int
    total_files_changed: int
    avg_duration_ms: int = 0
    last_execution: Optional[str] = None


class DreamLogsResponse(BaseModel):
    """Response for listing dream logs."""

    records: list[DreamLogRecord]
    stats: DreamLogsStats
    total: int
    page: int
    page_size: int


class DreamLogReportSummary(BaseModel):
    """持续治理分析汇总指标。"""

    covered_users: int
    governed_users: int
    ungoverned_users: int
    total_executions: int
    success_count: int
    failed_count: int
    success_rate: float
    total_files_changed: int
    total_size_saved: int
    avg_duration_ms: int
    last_execution: Optional[str] = None


class DreamLogReportTrendPoint(BaseModel):
    """持续治理趋势点。"""

    date: str
    executions: int
    manual_count: int = 0
    cron_count: int = 0
    success_count: int
    failed_count: int
    total_size_saved: int


class DreamLogReportStatusBucket(BaseModel):
    """持续治理状态分布。"""

    status: str
    count: int


class DreamLogReportBbkBucket(BaseModel):
    """持续治理机构分布。"""

    bbk_id: str
    user_count: int
    governed_users: int
    executions: int
    success_rate: float


class DreamLogReportUserRow(BaseModel):
    """持续治理用户明细行。"""

    user_id: str
    user_name: Optional[str] = None
    bbk_id: Optional[str] = None
    agents: list[str]
    executions: int
    success_rate: float
    failed_count: int
    total_files_changed: int
    total_size_saved: int
    last_execution: Optional[str] = None
    latest_error: Optional[str] = None


class DreamLogReportRecord(BaseModel):
    """持续治理用户下钻记录。"""

    id: str
    timestamp: str
    trigger: str
    status: str
    agent_id: str
    files_optimized: list[str]
    total_size_saved: int
    total_files_changed: int
    duration_ms: int
    model_used: str
    input_tokens: int
    output_tokens: int
    summary: str
    error: Optional[str] = None


class ReconcileHealthInfo(BaseModel):
    """持续治理待补偿或待对账健康状态。"""

    source_id: str
    target_user_id: str
    target_agent_id: str
    entity_type: str
    entity_id: str
    status: str
    reason: str
    error: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    updated_at: Optional[datetime] = None


class DreamLogReportResponse(BaseModel):
    """持续治理分析报表响应。"""

    summary: DreamLogReportSummary
    trends: list[DreamLogReportTrendPoint]
    status_distribution: list[DreamLogReportStatusBucket]
    bbk_distribution: list[DreamLogReportBbkBucket]
    users: list[DreamLogReportUserRow]
    total: int
    page: int
    page_size: int
    health: list[ReconcileHealthInfo] = Field(default_factory=list)


class DreamLogUserRecordsResponse(BaseModel):
    """持续治理用户下钻记录响应。"""

    records: list[DreamLogReportRecord]
    total: int
    page: int
    page_size: int


class DiffResponse(BaseModel):
    """Response for file diff."""

    filename: str
    content_before: str
    content_after: str
    size_before: int
    size_after: int
    size_saved: int


class TriggerResponse(BaseModel):
    """Response for manual trigger."""

    success: bool
    message: str
    record_id: Optional[str] = None


class DreamStatusResponse(BaseModel):
    """治理任务运行状态"""

    running: bool
    started_at: Optional[str] = None
    trigger: Optional[str] = None  # "cron" 或 "manual"


class RollbackResponse(BaseModel):
    """Response for rollback operation."""

    success: bool
    message: str
    files_rolled_back: list[str]


class RollbackRequest(BaseModel):
    """Request body for rollback operation."""

    files: Optional[list[str]] = None


class BackupFileInfo(BaseModel):
    """Information about a backup file."""

    filename: str
    original_file: str
    record_id: str
    timestamp: str
    size: int
    created_at: str


class BackupListResponse(BaseModel):
    """Response for listing backup files."""

    files: list[BackupFileInfo]
    total_size: int
    total_files: int


class DeleteBackupResponse(BaseModel):
    """Response for deleting backup files."""

    success: bool
    message: str
    files_deleted: list[str]


class BackupContentResponse(BaseModel):
    """Response for backup file content preview."""

    filename: str
    content: str
    size: int
    original_file: str


class OrphanFileInfo(BaseModel):
    """Information about an orphan file."""

    filename: str
    size: int
    created_at: str
    modified_at: str
    path: str  # Relative path (filename only for workspace root files)
    full_path: str  # Absolute path


class OrphanFilesResponse(BaseModel):
    """Response for listing orphan files."""

    files: list[OrphanFileInfo]
    total_size: int
    total_files: int
    workspace_dir: str


class OrphanFileContentResponse(BaseModel):
    """Response for orphan file content preview."""

    filename: str
    content: str
    size: int
    file_type: str  # "text", "image", "binary", "error"
    is_loadable: bool
    error_message: Optional[str] = None


class ArchiveFileRequest(BaseModel):
    """归档当前工作区孤立文件的请求。"""

    files: list[str]
    reason: str = "manual"


class ArchiveItem(BaseModel):
    """归档区中可恢复文件的元数据。"""

    id: str
    original_path: str
    archive_path: str
    size_bytes: int
    mtime: str
    archived_at: str
    archived_by: str
    archive_reason: str
    target_user_id: Optional[str] = None
    target_agent_id: Optional[str] = None
    expired: bool = False


class ArchiveOperationResponse(BaseModel):
    """归档操作响应。"""

    success: bool
    message: str
    files_archived: list[str]
    items: list[ArchiveItem]


class ArchiveItemsResponse(BaseModel):
    """管理员归档列表响应。"""

    items: list[ArchiveItem]
    total: int
    page: int
    page_size: int


class ArchiveRestoreRequest(BaseModel):
    """管理员恢复归档文件的请求。"""

    archive_item_id: str
    target_user_id: str
    target_agent_id: str = "default"
    protect_after_restore: bool = False


class ArchiveRestoreResponse(BaseModel):
    """管理员恢复归档文件的响应。"""

    success: bool
    message: str
    restored_path: str
    protected: bool = False


class ArchivePurgeRequest(BaseModel):
    """管理员清理归档文件的请求。"""

    archive_item_ids: list[str]
    target_user_id: str
    target_agent_id: str = "default"
    reason: str = "manual_clear"


class ArchivePurgeExpiredRequest(BaseModel):
    """管理员清理超过保留期归档文件的请求。"""

    target_user_id: Optional[str] = None
    target_agent_id: Optional[str] = None
    reason: str = "expired_10_days"


class ArchivePurgeResponse(BaseModel):
    """管理员清理归档文件的响应。"""

    success: bool
    message: str
    files_deleted: list[str]
    files_count: int
    total_size_bytes: int
    audit_event_id: str


class ProtectedFileInfo(BaseModel):
    """管理员保护文件查询行。"""

    target_user_id: str
    target_agent_id: str
    path: str
    protected_at: str
    protected_by: str
    reason: str
    exists: bool
    size_bytes: Optional[int] = None
    mtime: Optional[str] = None


class ProtectedFilesResponse(BaseModel):
    """管理员保护文件查询响应。"""

    items: list[ProtectedFileInfo]
    total: int
    page: int
    page_size: int


class ProtectedFileRemoveRequest(BaseModel):
    """管理员取消保护文件的请求。"""

    target_user_id: str
    target_agent_id: str = "default"
    path: str


class ProtectedFileRemoveResponse(BaseModel):
    """管理员取消保护文件的响应。"""

    success: bool
    message: str
    removed_path: str


class ArchiveAdminAuditRecord(BaseModel):
    """管理员归档清理审计记录。"""

    event_id: str
    timestamp: str
    operation: str
    status: str
    actor_user_id: str
    actor_role: str
    source_id: str
    source_name: Optional[str] = None
    target_user_id: str
    target_agent_id: str
    scope: str
    files_count: int
    total_size_bytes: int
    reason: str
    error: Optional[str] = None


class ArchiveAdminAuditSummary(BaseModel):
    """管理员归档清理审计汇总。"""

    total_operations: int
    success_operations: int
    failed_operations: int
    partial_success_operations: int
    manual_operations: int
    auto_operations: int
    total_files_cleared: int
    total_size_cleared_bytes: int
    last_operation_at: Optional[str] = None


class ArchiveAdminAuditsResponse(BaseModel):
    """管理员归档清理审计分页响应。"""

    summary: ArchiveAdminAuditSummary
    items: list[ArchiveAdminAuditRecord]
    total: int
    page: int
    page_size: int


class ArchiveReportSummary(BaseModel):
    """当前渠道归档治理统计。"""

    archived_files: int
    archived_size_bytes: int
    pending_purge_files: int
    pending_purge_size_bytes: int
    protected_files: int
    protected_existing_files: int
    protected_missing_files: int
    purge_operations: int
    purge_success_operations: int
    purge_failed_operations: int
    purged_files: int
    purged_size_bytes: int
    last_purge_at: Optional[str] = None


class ArchiveReportResponse(BaseModel):
    """持续治理分析页归档统计响应。"""

    summary: ArchiveReportSummary
    health: list[ReconcileHealthInfo] = Field(default_factory=list)


# Keep list - files and directories that should NOT be listed as orphan
KEEP_FILES = {
    ".bootstrap_completed",
    "MEMORY.md",
    "AGENTS.md",
    "SOUL.md",
    "PROFILE.md",
    "HEARTBEAT.md",
    "BOOTSTRAP.md",
    "agent.json",
    "chats.json",
    "jobs.json",
    "token_usage.json",
    "dream_logs.json",
    "swe_file_metadata.json",
    "skill.json",
}

KEEP_DIRS = {
    "agents",
    ".disabled_skills",
    "hooks",
    "memory",
    "sessions",
    "backup",
    "skills",
    "governance",
    "dialog",
}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

DREAM_LOGS_FILE = "dream_logs.json"
BACKUP_DIR = "backup"

# Image file extensions for preview
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
ARCHIVE_DIR = "governance/archive"
ARCHIVE_FILES_DIR = "governance/archive/files"
ARCHIVE_INDEX_FILE = "governance/archive/index.json"
PROTECTED_PATHS_FILE = "governance/archive/protected_paths.json"
ARCHIVE_ADMIN_AUDIT_FILE = "governance/archive_admin_audit.jsonl"
AUTO_ARCHIVE_DAYS = 3
ARCHIVE_PURGE_DAYS = 10


def _utc_now() -> datetime:
    """返回统一的 UTC 当前时间，便于归档判断和审计记录。"""
    return datetime.now(timezone.utc)


def _isoformat(dt: datetime) -> str:
    """把时间统一输出为带 Z 的 UTC ISO 字符串。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _file_mtime_iso(path: Path) -> str:
    """读取文件最后修改时间并转换成 UTC ISO 字符串。"""
    return _isoformat(
        datetime.fromtimestamp(path.stat().st_mtime, timezone.utc),
    )


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """原子写入 JSON，避免并发或异常导致索引文件半写入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _load_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """读取 JSON 文件，文件缺失或损坏时返回默认结构。"""
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except Exception as exc:
        logger.warning("Failed to load json file %s: %s", path, exc)
        return default


def _archive_index_path(workspace_dir: Path) -> Path:
    """返回当前工作区归档索引路径。"""
    return workspace_dir / ARCHIVE_INDEX_FILE


def _protected_paths_path(workspace_dir: Path) -> Path:
    """返回当前工作区保护名单路径。"""
    return workspace_dir / PROTECTED_PATHS_FILE


def _load_archive_index(workspace_dir: Path) -> dict[str, Any]:
    """读取归档索引，保证返回结构包含 items。"""
    data = _load_json_file(
        _archive_index_path(workspace_dir),
        {"version": 1, "items": []},
    )
    items = data.get("items")
    if not isinstance(items, list):
        data["items"] = []
    data["version"] = 1
    return data


def _save_archive_index(workspace_dir: Path, data: dict[str, Any]) -> None:
    """保存归档索引。"""
    data["version"] = 1
    if not isinstance(data.get("items"), list):
        data["items"] = []
    _atomic_write_json(_archive_index_path(workspace_dir), data)


def _load_protected_paths(workspace_dir: Path) -> dict[str, Any]:
    """读取保护名单，保证返回结构包含 paths。"""
    data = _load_json_file(
        _protected_paths_path(workspace_dir),
        {"version": 1, "paths": []},
    )
    paths = data.get("paths")
    if not isinstance(paths, list):
        data["paths"] = []
    data["version"] = 1
    return data


def _save_protected_paths(workspace_dir: Path, data: dict[str, Any]) -> None:
    """保存保护名单。"""
    data["version"] = 1
    if not isinstance(data.get("paths"), list):
        data["paths"] = []
    _atomic_write_json(_protected_paths_path(workspace_dir), data)


def _normalise_workspace_relative_path(filepath: str) -> str:
    """归一化工作区相对路径，并拒绝绝对路径和穿越路径。"""
    raw_path = Path(filepath)
    if raw_path.is_absolute():
        raise HTTPException(status_code=403, detail="Access denied")
    parts = [part for part in raw_path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(status_code=403, detail="Access denied")
    if any(part.startswith(".") for part in parts):
        raise HTTPException(status_code=403, detail="Access denied")
    return "/".join(parts)


def _is_root_keep_file(relative_path: str) -> bool:
    """判断相对路径是否命中根目录保留文件。"""
    parts = relative_path.split("/")
    return len(parts) == 1 and parts[0] in KEEP_FILES


def _is_keep_dir_path(relative_path: str) -> bool:
    """判断相对路径是否位于根目录保留目录下。"""
    first = relative_path.split("/", 1)[0]
    return first in KEEP_DIRS


def _protected_path_set(workspace_dir: Path) -> set[str]:
    """返回当前工作区受保护相对路径集合。"""
    data = _load_protected_paths(workspace_dir)
    protected: set[str] = set()
    for item in data.get("paths", []):
        if isinstance(item, dict) and item.get("path"):
            try:
                protected.add(
                    _normalise_workspace_relative_path(str(item["path"])),
                )
            except HTTPException:
                continue
    return protected


def _is_protected_path(workspace_dir: Path, relative_path: str) -> bool:
    """判断文件是否在恢复保护名单中。"""
    return relative_path in _protected_path_set(workspace_dir)


def _resolve_workspace_file(
    workspace_dir: Path,
    filepath: str,
    *,
    allow_protected: bool = False,
) -> tuple[str, Path]:
    """解析工作区文件路径，并执行治理保留路径保护。"""
    relative_path = _normalise_workspace_relative_path(filepath)
    if _is_root_keep_file(relative_path) or _is_keep_dir_path(relative_path):
        raise HTTPException(status_code=403, detail="Protected path")
    if not allow_protected and _is_protected_path(
        workspace_dir,
        relative_path,
    ):
        raise HTTPException(status_code=409, detail="File is protected")
    file_path = workspace_dir / Path(*relative_path.split("/"))
    try:
        file_path.resolve().relative_to(workspace_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    return relative_path, file_path


def _archive_item_expired(
    item: dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    """判断归档项是否超过 10 天清理期限。"""
    archived_at = str(item.get("archived_at") or "")
    if not archived_at:
        return False
    try:
        archived_dt = datetime.fromisoformat(
            archived_at.replace("Z", "+00:00"),
        )
    except ValueError:
        return False
    if archived_dt.tzinfo is None:
        archived_dt = archived_dt.replace(tzinfo=timezone.utc)
    return (now or _utc_now()) - archived_dt.astimezone(
        timezone.utc,
    ) >= timedelta(
        days=ARCHIVE_PURGE_DAYS,
    )


def _archive_item_model(
    item: dict[str, Any],
    *,
    target_user_id: Optional[str] = None,
    target_agent_id: Optional[str] = None,
) -> ArchiveItem:
    """把索引记录转换成 API 响应模型。"""
    return ArchiveItem(
        id=str(item.get("id") or ""),
        original_path=str(item.get("original_path") or ""),
        archive_path=str(item.get("archive_path") or ""),
        size_bytes=int(item.get("size_bytes") or 0),
        mtime=str(item.get("mtime") or ""),
        archived_at=str(item.get("archived_at") or ""),
        archived_by=str(item.get("archived_by") or ""),
        archive_reason=str(item.get("archive_reason") or ""),
        target_user_id=target_user_id,
        target_agent_id=target_agent_id,
        expired=_archive_item_expired(item),
    )


def _validate_target_agent_id(target_agent_id: str) -> str:
    """校验可作为 workspace 路径片段的 agent 标识。"""
    from ...config.context import is_valid_identity_value

    if len(
        target_agent_id,
    ) > TARGET_AGENT_ID_MAX_LENGTH or not is_valid_identity_value(
        target_agent_id,
    ):
        raise HTTPException(status_code=400, detail="Invalid target agent id")
    return target_agent_id


def _optional_target_agent_id(target_agent_id: str | None) -> str | None:
    """校验可选的 agent 过滤条件。"""
    if target_agent_id is None:
        return None
    return _validate_target_agent_id(target_agent_id)


def _is_valid_target_agent_id(target_agent_id: str) -> bool:
    """判断历史 workspace 目录名是否可作为治理读模型 agent 标识。"""
    try:
        _validate_target_agent_id(target_agent_id)
    except HTTPException:
        return False
    return True


def _request_actor(request: Request) -> tuple[str, str]:
    """读取当前请求操作者和角色，用于管理员审计。"""
    actor = (
        getattr(request.state, "user", None)
        or request.headers.get("X-User-Id")
        or _get_tenant_id(request)
        or "unknown"
    )
    role = request.headers.get("X-User-Role", "").strip().lower() or "user"
    return str(actor), role


def _target_workspace_dir(
    workspace_root: Path,
    target_user_id: str,
    source_id: str,
    target_agent_id: str,
) -> Path:
    """根据逻辑用户、渠道和 Agent 定位目标工作区。"""
    safe_agent_id = _validate_target_agent_id(target_agent_id)
    workspaces_dir = (
        _resolve_tenant_dir(workspace_root, target_user_id, source_id)
        / "workspaces"
    ).resolve()
    workspace_dir = (workspaces_dir / safe_agent_id).resolve()
    try:
        workspace_dir.relative_to(workspaces_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    return workspace_dir


def _append_archive_admin_audit(
    admin_workspace_dir: Path,
    record: dict[str, Any],
) -> None:
    """把管理员归档清理审计追加写入操作者自己的工作区。"""
    audit_path = admin_workspace_dir / ARCHIVE_ADMIN_AUDIT_FILE
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        fh.write("\n")


def _read_archive_admin_audit(path: Path) -> list[dict[str, Any]]:
    """读取单个管理员清理审计文件，跳过损坏行。"""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _get_file_type(filepath: Path) -> str:
    """Determine file type based on extension."""
    ext = filepath.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in {
        ".md",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".css",
        ".js",
        ".ts",
        ".py",
        ".sh",
        ".log",
        ".toml",
    }:
        return "text"
    return "binary"


def _get_agent_id(request: Request) -> str:
    """Get agent_id from request header or default."""
    return _validate_target_agent_id(
        request.headers.get("X-Agent-Id", "default"),
    )


def _get_workspace_dir(request: Request) -> Path:
    """Get agent-level workspace directory from request state.

    Returns the agent workspace directory (e.g., /root/.swe/{tenant}/workspaces/{agent})
    where dream_logs.json is stored.
    """
    workspace = getattr(request.state, "workspace", None)
    if workspace is None:
        raise HTTPException(
            status_code=503,
            detail="Tenant workspace not available",
        )
    # workspace is TenantWorkspaceContext with tenant-level directory
    # Add workspaces/{agent_id} to get agent-level directory
    tenant_dir = workspace.workspace_dir
    agent_id = _get_agent_id(request)
    return tenant_dir / "workspaces" / agent_id


def _get_tenant_id(request: Request) -> str:
    """Get runtime tenant identity from request scope or tenant header."""
    request_state = getattr(request, "state", None)
    if request_state is not None:
        from ...config.context import resolve_request_effective_tenant_id

        tenant_id = resolve_request_effective_tenant_id(
            getattr(request_state, "tenant_id", None),
            getattr(request_state, "source_id", None),
            getattr(request_state, "scope_id", None),
        )
        if tenant_id:
            return tenant_id
    return request.headers.get("X-Tenant-Id", "default")


def _get_logical_tenant_id(request: Request) -> str:
    """获取当前请求的原始租户标识，避免把编码后的 scope 展示给管理员。"""
    request_state = getattr(request, "state", None)
    source_id = _get_report_source_id(request)
    runtime_tenant_id = None
    if request_state is not None:
        runtime_tenant_id = getattr(request_state, "tenant_id", None)
        scope_id = getattr(request_state, "scope_id", None)
        if scope_id and not runtime_tenant_id:
            runtime_tenant_id = scope_id
    runtime_tenant_id = runtime_tenant_id or request.headers.get("X-Tenant-Id")
    if not runtime_tenant_id:
        return "default"
    from ...config.context import resolve_runtime_identity

    tenant_id, _, _ = resolve_runtime_identity(runtime_tenant_id, source_id)
    return tenant_id or str(runtime_tenant_id)


def _parse_backup_filename(filename: str) -> str:
    """Parse backup filename to get original file name.

    Args:
        filename: Backup filename like "memory_backup_20260428_104646.md"

    Returns:
        Original filename like "MEMORY.md"
    """
    stem = Path(filename).stem
    if "_backup_" in stem:
        prefix = stem.split("_backup_")[0]
        return prefix.upper() + ".md"
    # Fallback: just capitalize the stem
    return stem.replace("_backup_", "").replace("_", "").upper() + ".md"


def _load_dream_logs(workspace_dir: Path) -> dict:
    """Load dream_logs.json from workspace directory."""
    log_path = workspace_dir / DREAM_LOGS_FILE
    if not log_path.exists():
        return {
            "records": [],
            "stats": {
                "total_executions": 0,
                "success_count": 0,
                "failed_count": 0,
                "total_size_saved": 0,
                "total_files_changed": 0,
                "last_execution": None,
            },
        }
    try:
        with open(log_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load dream logs: {e}")
        return {
            "records": [],
            "stats": {
                "total_executions": 0,
                "success_count": 0,
                "failed_count": 0,
                "total_size_saved": 0,
                "total_files_changed": 0,
                "last_execution": None,
            },
        }


def _save_dream_logs(workspace_dir: Path, data: dict) -> None:
    """Save dream_logs.json to workspace directory."""
    log_path = workspace_dir / DREAM_LOGS_FILE
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save dream logs: {e}")


def _format_size(size_bytes: int) -> str:
    """Format size in bytes to human readable format."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


REPORT_ALLOWED_ROLES = {"manager", "admin"}
DEFAULT_REPORT_AGENT_ID = "default"
MAX_REPORT_PAGE_SIZE = 100


def _ensure_report_permission(request: Request) -> None:
    """校验持续治理分析只允许管理角色访问。"""
    role = request.headers.get("X-User-Role", "").strip().lower()
    if role not in REPORT_ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="Permission denied")


def _get_report_source_id(request: Request) -> str:
    """获取报表统计的当前来源标识。"""
    request_state = getattr(request, "state", None)
    source_id = None
    if request_state is not None:
        source_id = getattr(request_state, "source_id", None)
    source_id = source_id or request.headers.get("X-Source-Id")
    if not source_id:
        raise HTTPException(status_code=400, detail="source_id is required")
    return source_id


def _get_workspace_root(request: Request) -> Path:
    """获取所有 source-scoped 租户目录所在的根目录。"""
    workspace = getattr(request.state, "workspace", None)
    if workspace is None:
        raise HTTPException(
            status_code=503,
            detail="Tenant workspace not available",
        )
    return Path(workspace.workspace_dir).parent


def _resolve_tenant_dir(
    workspace_root: Path,
    tenant_id: str,
    source_id: str,
) -> Path:
    """把逻辑用户和 source 映射到运行时租户目录。"""
    from ...config.context import resolve_runtime_tenant_id

    runtime_tenant_id = resolve_runtime_tenant_id(tenant_id, source_id)
    return workspace_root / (runtime_tenant_id or tenant_id)


def _iter_agent_workspace_dirs(
    tenant_dir: Path,
    agent_id: Optional[str] = None,
) -> list[tuple[str, Path]]:
    """列出需要纳入统计的 agent 工作区目录。"""
    workspaces_dir = tenant_dir / "workspaces"
    if agent_id:
        safe_agent_id = _validate_target_agent_id(agent_id)
        workspace_dir = (workspaces_dir.resolve() / safe_agent_id).resolve()
        try:
            workspace_dir.relative_to(workspaces_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied")
        return [(safe_agent_id, workspace_dir)]
    if not workspaces_dir.exists():
        return [
            (
                DEFAULT_REPORT_AGENT_ID,
                workspaces_dir / DEFAULT_REPORT_AGENT_ID,
            ),
        ]
    return [
        (path.name, path)
        for path in sorted(workspaces_dir.iterdir())
        if path.is_dir() and _is_valid_target_agent_id(path.name)
    ]


def _parse_report_datetime(
    value: Optional[str],
    *,
    is_end: bool = False,
) -> Optional[datetime]:
    """解析报表筛选时间，日期输入按自然日边界处理。"""
    if not value:
        return None
    try:
        if len(value) == 10:
            parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
            dt = datetime.combine(
                parsed_date,
                time.max if is_end else time.min,
            )
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid datetime: {value}",
        ) from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _record_datetime(record: dict[str, Any]) -> Optional[datetime]:
    """解析治理记录时间，异常记录不参与时间排序和趋势。"""
    timestamp = str(record.get("timestamp") or "")
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _record_matches_report_filters(
    record: dict[str, Any],
    *,
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    status: Optional[str],
    trigger: Optional[str],
) -> bool:
    """判断治理记录是否命中报表筛选条件。"""
    if status and record.get("status") != status:
        return False
    if trigger and record.get("trigger") != trigger:
        return False
    record_dt = _record_datetime(record)
    if start_dt is not None and (record_dt is None or record_dt < start_dt):
        return False
    if end_dt is not None and (record_dt is None or record_dt > end_dt):
        return False
    return True


def _normalise_report_record(
    record: dict[str, Any],
    *,
    agent_id: str,
) -> dict[str, Any]:
    """把原始 dream log 记录收敛为报表只读记录。"""
    return {
        "id": str(record.get("id") or ""),
        "timestamp": str(record.get("timestamp") or ""),
        "trigger": str(record.get("trigger") or ""),
        "status": str(record.get("status") or ""),
        "agent_id": agent_id,
        "files_optimized": list(record.get("files_optimized") or []),
        "total_size_saved": int(record.get("total_size_saved") or 0),
        "total_files_changed": int(record.get("total_files_changed") or 0),
        "duration_ms": int(record.get("duration_ms") or 0),
        "model_used": str(record.get("model_used") or ""),
        "input_tokens": int(record.get("input_tokens") or 0),
        "output_tokens": int(record.get("output_tokens") or 0),
        "summary": str(record.get("summary") or ""),
        "error": record.get("error"),
    }


def _records_sort_value(record: dict[str, Any]) -> tuple[str, str]:
    """为记录倒序排序提供稳定键。"""
    return (str(record.get("timestamp") or ""), str(record.get("id") or ""))


def _normalise_page(page: int, page_size: int) -> tuple[int, int]:
    """限制报表分页参数，避免一次请求扫描后返回过多行。"""
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), MAX_REPORT_PAGE_SIZE)
    return safe_page, safe_page_size


async def _load_source_tenants(source_id: str) -> list[dict[str, Any]]:
    """读取当前 source 下可管理的用户清单。"""
    from ..workspace.tenant_init_source_store import (
        get_tenant_init_source_store,
    )

    store = get_tenant_init_source_store()
    if store is None:
        raise HTTPException(status_code=503, detail="Database not available")
    rows = await store.get_by_source(source_id)
    return list(rows)


def _get_continuous_governance_service(request: Request) -> Any:
    """读取持续治理数据库读模型服务。"""
    service = getattr(
        request.app.state,
        "continuous_governance_service",
        None,
    )
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Continuous governance database service not available",
        )
    store = getattr(service, "store", None)
    if store is not None and not getattr(store, "is_available", True):
        raise HTTPException(
            status_code=503,
            detail="Continuous governance database service not available",
        )
    return service


def _get_optional_continuous_governance_service(
    request: Request,
) -> Any | None:
    """当前 workspace 操作尽力获取数据库读模型服务。"""
    service = getattr(
        request.app.state,
        "continuous_governance_service",
        None,
    )
    if service is None:
        return None
    store = getattr(service, "store", None)
    if store is not None and not getattr(store, "is_available", True):
        return None
    return service


def _get_optional_source_id(request: Request) -> str | None:
    """读取可用于双写的 source_id，缺失时不影响当前 workspace 操作。"""
    request_state = getattr(request, "state", None)
    source_id = None
    if request_state is not None:
        source_id = getattr(request_state, "source_id", None)
    source_id = source_id or request.headers.get("X-Source-Id")
    return str(source_id) if source_id else None


async def _record_dual_write_health(
    request: Request,
    *,
    source_id: str,
    target_user_id: str,
    target_agent_id: str,
    entity_type: str,
    entity_id: str,
    error: Exception,
    payload: dict[str, Any],
) -> None:
    """双写失败时尽力登记待对账状态。"""
    service = _get_optional_continuous_governance_service(request)
    if service is None:
        return
    await _record_service_reconcile_health(
        service,
        source_id=source_id,
        target_user_id=target_user_id,
        target_agent_id=target_agent_id,
        entity_type=entity_type,
        entity_id=entity_id,
        error=error,
        payload=payload,
    )


async def _record_service_reconcile_health(
    service: Any,
    *,
    source_id: str,
    target_user_id: str,
    target_agent_id: str,
    entity_type: str,
    entity_id: str,
    error: Exception,
    payload: dict[str, Any],
) -> None:
    """在无 Request 的定时任务边界登记待对账状态。"""
    try:
        await service.record_reconcile_health(
            source_id=source_id,
            target_user_id=target_user_id,
            target_agent_id=target_agent_id,
            entity_type=entity_type,
            entity_id=entity_id,
            reason="workspace write succeeded but db write failed",
            error=str(error),
            payload=payload,
        )
    except Exception as exc:
        logger.warning(
            "Failed to record continuous governance health: %s",
            exc,
        )


async def dual_write_dream_archive_maintenance_result(
    *,
    service: Any,
    source_id: str,
    target_user_id: str,
    target_agent_id: str,
    maintenance: DreamArchiveMaintenanceResult,
    actor: str,
    source_name: str | None = None,
) -> None:
    """把 dream 后置文件治理维护结果写入数据库读模型。"""
    if maintenance.archived_items:
        payload_items = [
            item.model_dump()
            for item in maintenance.archived_items
            if hasattr(item, "model_dump")
        ]
        try:
            await service.upsert_archive_items(
                source_id=source_id,
                target_user_id=target_user_id,
                target_agent_id=target_agent_id,
                items=payload_items,
            )
        except Exception as exc:
            await _record_service_reconcile_health(
                service,
                source_id=source_id,
                target_user_id=target_user_id,
                target_agent_id=target_agent_id,
                entity_type="archive_items",
                entity_id=_health_batch_entity_id(
                    "archive_items",
                    [str(item.get("id") or "") for item in payload_items],
                ),
                error=exc,
                payload={"items": payload_items},
            )
    if maintenance.purged_archive_item_ids:
        audit = _build_purge_audit_payload(
            event_id=uuid.uuid4().hex,
            operation="purge_expired_archive",
            actor_user_id=actor,
            actor_role="system",
            source_id=source_id,
            source_name=source_name or source_id,
            target_user_id=target_user_id,
            target_agent_id=target_agent_id,
            scope="dream_auto_expired_10_days",
            files_count=len(maintenance.purged_paths),
            total_size_bytes=maintenance.purged_size_bytes,
            reason="dream_auto_expired_10_days",
        )
        try:
            await service.delete_archive_items(
                source_id=source_id,
                target_user_id=target_user_id,
                target_agent_id=target_agent_id,
                archive_item_ids=maintenance.purged_archive_item_ids,
            )
            await service.upsert_cleanup_audit(audit)
        except Exception as exc:
            await _record_service_reconcile_health(
                service,
                source_id=source_id,
                target_user_id=target_user_id,
                target_agent_id=target_agent_id,
                entity_type="cleanup_audit",
                entity_id=audit["event_id"],
                error=exc,
                payload={
                    "audit": audit,
                    "archive_item_ids": maintenance.purged_archive_item_ids,
                },
            )


async def _dual_write_archive_items(
    request: Request,
    *,
    source_id: str,
    target_user_id: str,
    target_agent_id: str,
    items: list[ArchiveItem],
) -> None:
    """归档操作成功后写入文件治理状态读模型。"""
    if not items:
        return
    service = _get_optional_continuous_governance_service(request)
    if service is None:
        return
    payload_items = [item.model_dump() for item in items]
    try:
        await service.upsert_archive_items(
            source_id=source_id,
            target_user_id=target_user_id,
            target_agent_id=target_agent_id,
            items=payload_items,
        )
    except Exception as exc:
        await _record_dual_write_health(
            request,
            source_id=source_id,
            target_user_id=target_user_id,
            target_agent_id=target_agent_id,
            entity_type="archive_items",
            entity_id=_health_batch_entity_id(
                "archive_items",
                [item.id for item in items],
            ),
            error=exc,
            payload={"items": payload_items},
        )


async def _dual_write_workspace_governance_records(
    request: Request,
    *,
    workspace_dir: Path,
    target_user_id: str,
    target_agent_id: str,
    before_record_ids: set[str],
) -> None:
    """dream 完成后把新增 workspace 记录写入数据库读模型。"""
    source_id = _get_optional_source_id(request)
    service = _get_optional_continuous_governance_service(request)
    if not source_id or service is None:
        return
    tenants = await _load_source_tenants(source_id)
    tenant = next(
        (
            row
            for row in tenants
            if str(row.get("tenant_id") or "") == target_user_id
        ),
        {},
    )
    data = _load_dream_logs(workspace_dir)
    for record in data.get("records", []):
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("id") or "")
        if not record_id or record_id in before_record_ids:
            continue
        await service.upsert_workspace_governance_record_with_health(
            source_id=source_id,
            target_user_id=target_user_id,
            target_user_name=tenant.get("tenant_name"),
            bbk_id=tenant.get("bbk_id"),
            target_agent_id=target_agent_id,
            record=record,
        )


def _filter_source_tenants(
    tenants: list[dict[str, Any]],
    *,
    bbk_id: Optional[str],
    user_search: Optional[str],
) -> list[dict[str, Any]]:
    """应用机构和用户关键字筛选。"""
    keyword = (user_search or "").strip().lower()
    filtered = []
    for tenant in tenants:
        tenant_id = str(tenant.get("tenant_id") or "")
        tenant_name = str(tenant.get("tenant_name") or "")
        if bbk_id and tenant.get("bbk_id") != bbk_id:
            continue
        if (
            keyword
            and keyword not in tenant_id.lower()
            and keyword not in tenant_name.lower()
        ):
            continue
        filtered.append(tenant)
    return filtered


def _collect_tenant_report_records(
    workspace_root: Path,
    tenant: dict[str, Any],
    source_id: str,
    *,
    agent_id: Optional[str],
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    status: Optional[str],
    trigger: Optional[str],
) -> list[dict[str, Any]]:
    """读取单个用户在筛选条件下的治理记录。"""
    tenant_id = str(tenant.get("tenant_id") or "")
    tenant_dir = _resolve_tenant_dir(workspace_root, tenant_id, source_id)
    report_records: list[dict[str, Any]] = []
    for current_agent_id, workspace_dir in _iter_agent_workspace_dirs(
        tenant_dir,
        agent_id,
    ):
        data = _load_dream_logs(workspace_dir)
        for record in data.get("records", []):
            if not isinstance(record, dict):
                continue
            if not _record_matches_report_filters(
                record,
                start_dt=start_dt,
                end_dt=end_dt,
                status=status,
                trigger=trigger,
            ):
                continue
            report_records.append(
                _normalise_report_record(record, agent_id=current_agent_id),
            )
    report_records.sort(key=_records_sort_value, reverse=True)
    return report_records


def _build_report_summary(
    users: list[DreamLogReportUserRow],
    records: list[dict[str, Any]],
) -> DreamLogReportSummary:
    """从用户行和记录构建汇总指标。"""
    total_executions = len(records)
    success_count = sum(
        1 for record in records if record["status"] == "success"
    )
    failed_count = sum(1 for record in records if record["status"] == "failed")
    total_duration_ms = sum(record["duration_ms"] for record in records)
    return DreamLogReportSummary(
        covered_users=len(users),
        governed_users=sum(1 for user in users if user.executions > 0),
        ungoverned_users=sum(1 for user in users if user.executions == 0),
        total_executions=total_executions,
        success_count=success_count,
        failed_count=failed_count,
        success_rate=(
            round(
                success_count * 100 / total_executions,
                2,
            )
            if total_executions
            else 0
        ),
        total_files_changed=sum(
            record["total_files_changed"] for record in records
        ),
        total_size_saved=sum(record["total_size_saved"] for record in records),
        avg_duration_ms=(
            total_duration_ms // total_executions if total_executions else 0
        ),
        last_execution=max(
            (record["timestamp"] for record in records),
            default=None,
        ),
    )


def _build_report_trends(
    records: list[dict[str, Any]],
) -> list[DreamLogReportTrendPoint]:
    """按日期聚合治理趋势。"""
    buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "executions": 0,
            "manual_count": 0,
            "cron_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "total_size_saved": 0,
        },
    )
    for record in records:
        record_dt = _record_datetime(record)
        if record_dt is None:
            continue
        bucket = buckets[record_dt.date().isoformat()]
        bucket["executions"] += 1
        if record.get("trigger") == "cron":
            bucket["cron_count"] += 1
        else:
            bucket["manual_count"] += 1
        bucket["success_count"] += 1 if record["status"] == "success" else 0
        bucket["failed_count"] += 1 if record["status"] == "failed" else 0
        bucket["total_size_saved"] += record["total_size_saved"]
    return [
        DreamLogReportTrendPoint(date=date_key, **values)
        for date_key, values in sorted(buckets.items())
    ]


def _build_status_distribution(
    records: list[dict[str, Any]],
) -> list[DreamLogReportStatusBucket]:
    """按治理状态聚合分布。"""
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[record["status"] or "unknown"] += 1
    return [
        DreamLogReportStatusBucket(status=status, count=count)
        for status, count in sorted(counts.items())
    ]


def _build_bbk_distribution(
    users: list[DreamLogReportUserRow],
) -> list[DreamLogReportBbkBucket]:
    """按机构聚合用户覆盖和治理成功率。"""
    buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "user_count": 0,
            "governed_users": 0,
            "executions": 0,
            "success_count": 0,
        },
    )
    for user in users:
        bucket = buckets[user.bbk_id or "unassigned"]
        bucket["user_count"] += 1
        bucket["governed_users"] += 1 if user.executions else 0
        bucket["executions"] += user.executions
        bucket["success_count"] += round(
            user.executions * user.success_rate / 100,
        )

    return [
        DreamLogReportBbkBucket(
            bbk_id=bbk_id,
            user_count=values["user_count"],
            governed_users=values["governed_users"],
            executions=values["executions"],
            success_rate=(
                round(
                    values["success_count"] * 100 / values["executions"],
                    2,
                )
                if values["executions"]
                else 0
            ),
        )
        for bbk_id, values in sorted(buckets.items())
    ]


def _build_user_row(
    tenant: dict[str, Any],
    records: list[dict[str, Any]],
) -> DreamLogReportUserRow:
    """构建持续治理用户明细行。"""
    executions = len(records)
    success_count = sum(
        1 for record in records if record["status"] == "success"
    )
    failed_count = sum(1 for record in records if record["status"] == "failed")
    latest_error = next(
        (record.get("error") for record in records if record.get("error")),
        None,
    )
    return DreamLogReportUserRow(
        user_id=str(tenant.get("tenant_id") or ""),
        user_name=tenant.get("tenant_name"),
        bbk_id=tenant.get("bbk_id"),
        agents=sorted({record["agent_id"] for record in records}),
        executions=executions,
        success_rate=(
            round(success_count * 100 / executions, 2) if executions else 0
        ),
        failed_count=failed_count,
        total_files_changed=sum(
            record["total_files_changed"] for record in records
        ),
        total_size_saved=sum(record["total_size_saved"] for record in records),
        last_execution=max(
            (record["timestamp"] for record in records),
            default=None,
        ),
        latest_error=latest_error,
    )


# ------------------------------------------------------------------
# API endpoints
# ------------------------------------------------------------------


@router.get("/report", response_model=DreamLogReportResponse)
async def get_dream_logs_report(
    request: Request,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    bbk_id: Optional[str] = None,
    user_search: Optional[str] = None,
    status: Optional[str] = None,
    trigger: Optional[str] = None,
    agent_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> DreamLogReportResponse:
    """统计当前 source 下所有可管理用户的持续治理情况。"""
    _ensure_report_permission(request)
    source_id = _get_report_source_id(request)
    start_dt = _parse_report_datetime(start_time)
    end_dt = _parse_report_datetime(end_time, is_end=True)
    safe_agent_id = _optional_target_agent_id(agent_id)
    service = _get_continuous_governance_service(request)
    report = await service.build_governance_report(
        source_id=source_id,
        tenants=await _load_source_tenants(source_id),
        start_time=start_dt,
        end_time=end_dt,
        bbk_id=bbk_id,
        user_search=user_search,
        status=status,
        trigger=trigger,
        agent_id=safe_agent_id,
        page=page,
        page_size=page_size,
    )
    return DreamLogReportResponse(
        summary=DreamLogReportSummary(**report.summary.model_dump()),
        trends=[
            DreamLogReportTrendPoint(**item.model_dump())
            for item in report.trends
        ],
        status_distribution=[
            DreamLogReportStatusBucket(**item.model_dump())
            for item in report.status_distribution
        ],
        bbk_distribution=[
            DreamLogReportBbkBucket(**item.model_dump())
            for item in report.bbk_distribution
        ],
        users=[
            DreamLogReportUserRow(**item.model_dump()) for item in report.users
        ],
        total=report.total,
        page=report.page,
        page_size=report.page_size,
        health=[
            ReconcileHealthInfo(**item.model_dump()) for item in report.health
        ],
    )


@router.get(
    "/report/users/{user_id}/records",
    response_model=DreamLogUserRecordsResponse,
)
async def get_dream_log_report_user_records(
    request: Request,
    user_id: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    status: Optional[str] = None,
    trigger: Optional[str] = None,
    agent_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> DreamLogUserRecordsResponse:
    """查询当前 source 内单个用户的持续治理记录。"""
    _ensure_report_permission(request)
    source_id = _get_report_source_id(request)
    start_dt = _parse_report_datetime(start_time)
    end_dt = _parse_report_datetime(end_time, is_end=True)
    safe_agent_id = _optional_target_agent_id(agent_id)
    service = _get_continuous_governance_service(request)
    tenants = await _load_source_tenants(source_id)
    result = await service.list_user_records(
        source_id=source_id,
        tenants=tenants,
        user_id=user_id,
        start_time=start_dt,
        end_time=end_dt,
        status=status,
        trigger=trigger,
        agent_id=safe_agent_id,
        page=page,
        page_size=page_size,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="User not found")
    return DreamLogUserRecordsResponse(
        records=[
            DreamLogReportRecord(**record.model_dump())
            for record in result.records
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get("", response_model=DreamLogsResponse)
async def list_dream_logs(
    request: Request,
    page: int = 1,
    page_size: int = 20,
) -> DreamLogsResponse:
    """List dream optimization records.

    Args:
        request: FastAPI request.
        page: Page number (1-indexed).
        page_size: Number of records per page.

    Returns:
        DreamLogsResponse with records and stats.
    """
    workspace_dir = _get_workspace_dir(request)

    data = _load_dream_logs(workspace_dir)
    records = data.get("records", [])
    stats = data.get("stats", {})

    # Calculate avg_duration_ms
    total_executions = stats.get("total_executions", 0)
    total_duration_ms = stats.get("total_duration_ms", 0)
    stats["avg_duration_ms"] = (
        total_duration_ms // total_executions if total_executions > 0 else 0
    )

    # Sort records by timestamp (most recent first)
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    # Paginate records
    total = len(records)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paginated_records = records[start_idx:end_idx]

    # Convert to response models
    record_models = []
    for r in paginated_records:
        file_stats_dict = {}
        for filename, fs in r.get("file_stats", {}).items():
            file_stats_dict[filename] = FileStats(**fs)
        record_models.append(
            DreamLogRecord(
                id=r["id"],
                timestamp=r["timestamp"],
                trigger=r["trigger"],
                status=r["status"],
                files_optimized=r.get("files_optimized", []),
                file_stats=file_stats_dict,
                total_size_saved=r.get("total_size_saved", 0),
                total_files_changed=r.get("total_files_changed", 0),
                duration_ms=r.get("duration_ms", 0),
                model_used=r.get("model_used", ""),
                input_tokens=r.get("input_tokens", 0),
                output_tokens=r.get("output_tokens", 0),
                summary=r.get("summary", ""),
                error=r.get("error"),
            ),
        )

    return DreamLogsResponse(
        records=record_models,
        stats=DreamLogsStats(**stats),
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/stats", response_model=DreamLogsStats)
async def get_dream_logs_stats(request: Request) -> DreamLogsStats:
    """Get aggregate statistics for dream optimization.

    Args:
        request: FastAPI request.

    Returns:
        DreamLogsStats with aggregate stats.
    """
    workspace_dir = _get_workspace_dir(request)

    data = _load_dream_logs(workspace_dir)
    stats = data.get("stats", {})

    # Calculate avg_duration_ms
    total_executions = stats.get("total_executions", 0)
    total_duration_ms = stats.get("total_duration_ms", 0)
    avg_duration_ms = (
        total_duration_ms // total_executions if total_executions > 0 else 0
    )
    stats["avg_duration_ms"] = avg_duration_ms

    return DreamLogsStats(**stats)


@router.get("/diff/{record_id}/{filename}", response_model=DiffResponse)
async def get_file_diff(
    request: Request,
    record_id: str,
    filename: str,
) -> DiffResponse:
    """Get before/after diff for a specific file.

    Args:
        request: FastAPI request.
        record_id: Dream optimization record ID.
        filename: File name (e.g., "MEMORY.md").

    Returns:
        DiffResponse with before/after content.
    """
    workspace_dir = _get_workspace_dir(request)

    data = _load_dream_logs(workspace_dir)
    records = data.get("records", [])

    # Find the record
    record = None
    for r in records:
        if r["id"] == record_id:
            record = r
            break

    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    file_stats = record.get("file_stats", {}).get(filename)
    if not file_stats:
        raise HTTPException(status_code=404, detail="File stats not found")

    backup_path = workspace_dir / file_stats["backup_path"]
    current_path = workspace_dir / filename

    # Read before content (backup)
    content_before = ""
    if backup_path.exists():
        try:
            content_before = backup_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read backup file: {e}")

    # Read after content (current)
    content_after = ""
    if current_path.exists():
        try:
            content_after = current_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read current file: {e}")

    return DiffResponse(
        filename=filename,
        content_before=content_before,
        content_after=content_after,
        size_before=file_stats["size_before"],
        size_after=file_stats["size_after"],
        size_saved=file_stats["size_saved"],
    )


@router.post("/rollback/{record_id}", response_model=RollbackResponse)
async def rollback_dream_optimization(
    request: Request,
    record_id: str,
    body: Optional[RollbackRequest] = None,
) -> RollbackResponse:
    """Rollback specific files or all files from a dream optimization.

    Args:
        request: FastAPI request.
        record_id: Dream optimization record ID.
        body: Optional request body with list of files to rollback. If None or empty, rollback all.

    Returns:
        RollbackResponse with rollback status.
    """
    workspace_dir = _get_workspace_dir(request)

    data = _load_dream_logs(workspace_dir)
    records = data.get("records", [])

    # Find the record
    record_idx = None
    record = None
    for i, r in enumerate(records):
        if r["id"] == record_id:
            record_idx = i
            record = r
            break

    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    # Determine which files to rollback
    file_stats = record.get("file_stats", {})
    files_to_rollback = body.files if body and body.files else None
    if files_to_rollback:
        rollback_files = {
            f: file_stats[f] for f in files_to_rollback if f in file_stats
        }
    else:
        rollback_files = file_stats

    if not rollback_files:
        return RollbackResponse(
            success=False,
            message="No files to rollback",
            files_rolled_back=[],
        )

    rolled_back_files = []
    for filename, stats in rollback_files.items():
        backup_path = workspace_dir / stats["backup_path"]
        current_path = workspace_dir / filename

        if not backup_path.exists():
            logger.warning(f"Backup file not found: {backup_path}")
            continue

        try:
            shutil.copyfile(backup_path, current_path)
            rolled_back_files.append(filename)
            logger.info(f"Rolled back {filename} from {backup_path}")
        except Exception as e:
            logger.error(f"Failed to rollback {filename}: {e}")

    # Update record status
    if record_idx is not None and rolled_back_files:
        records[record_idx]["status"] = "rollback"
        records[record_idx]["rollback_timestamp"] = datetime.now().isoformat()
        records[record_idx]["rollback_files"] = rolled_back_files
        _save_dream_logs(workspace_dir, data)
        source_id = _get_optional_source_id(request)
        service = _get_optional_continuous_governance_service(request)
        if source_id and service is not None:
            target_user_id = _get_logical_tenant_id(request)
            target_agent_id = _get_agent_id(request)
            try:
                await service.mark_governance_record_rollback(
                    source_id=source_id,
                    target_user_id=target_user_id,
                    target_agent_id=target_agent_id,
                    record_id=record_id,
                    rollback_timestamp=records[record_idx][
                        "rollback_timestamp"
                    ],
                    rollback_files=rolled_back_files,
                )
            except Exception as exc:
                await _record_dual_write_health(
                    request,
                    source_id=source_id,
                    target_user_id=target_user_id,
                    target_agent_id=target_agent_id,
                    entity_type="governance_record",
                    entity_id=record_id,
                    error=exc,
                    payload={
                        "record_id": record_id,
                        "rollback_timestamp": records[record_idx][
                            "rollback_timestamp"
                        ],
                        "rollback_files": rolled_back_files,
                    },
                )

    return RollbackResponse(
        success=bool(rolled_back_files),
        message=f"Rolled back {len(rolled_back_files)} files",
        files_rolled_back=rolled_back_files,
    )


@router.post("/trigger", response_model=TriggerResponse)
async def trigger_dream_optimization(request: Request) -> TriggerResponse:
    """Manually trigger dream optimization (async, returns immediately).

    Args:
        request: FastAPI request.

    Returns:
        TriggerResponse with trigger status.
    """
    tenant_id = _get_tenant_id(request)

    # Get agent_id from request or use default
    agent_id = _validate_target_agent_id(
        request.headers.get("X-Agent-Id", "default"),
    )

    try:
        # Get MultiAgentManager from app state
        manager = getattr(request.app.state, "multi_agent_manager", None)
        if not manager:
            return TriggerResponse(
                success=False,
                message="MultiAgentManager not initialized",
            )

        workspace = await manager.get_agent(agent_id, tenant_id=tenant_id)

        if not workspace:
            return TriggerResponse(
                success=False,
                message=f"Workspace not found for agent {agent_id}",
            )

        runner = workspace.runner
        if not runner or not runner.memory_manager:
            return TriggerResponse(
                success=False,
                message="Memory manager not available",
            )

        # 如果已有任务在运行，拒绝重复触发
        status = _get_running_status()
        if status["running"]:
            return TriggerResponse(
                success=False,
                message="A governance task is already running",
            )

        async def _wrapped_dream():
            _set_running("manual")
            try:
                maintenance_workspace_value = getattr(
                    runner,
                    "workspace_dir",
                    None,
                )
                workspace_dir = (
                    Path(maintenance_workspace_value)
                    if maintenance_workspace_value
                    else _get_workspace_dir(request)
                )
                before_record_ids = {
                    str(record.get("id") or "")
                    for record in _load_dream_logs(workspace_dir).get(
                        "records",
                        [],
                    )
                    if isinstance(record, dict) and record.get("id")
                }
                await runner.memory_manager.dream_memory(
                    tenant_id=tenant_id,
                    trigger="manual",
                )
                await _dual_write_workspace_governance_records(
                    request,
                    workspace_dir=workspace_dir,
                    target_user_id=_get_logical_tenant_id(request),
                    target_agent_id=agent_id,
                    before_record_ids=before_record_ids,
                )
            finally:
                _clear_running()

        # Execute dream asynchronously in background (fire and forget)
        asyncio.create_task(_wrapped_dream())

        return TriggerResponse(
            success=True,
            message="Dream optimization started in background",
            record_id=None,  # Will be available after execution completes
        )

    except Exception as e:
        logger.error(f"Failed to trigger dream optimization: {e}")
        return TriggerResponse(
            success=False,
            message=f"Failed to trigger dream optimization: {str(e)}",
        )


# ------------------------------------------------------------------
# 治理任务运行状态
# ------------------------------------------------------------------


@router.get("/status", response_model=DreamStatusResponse)
async def get_governance_status(request: Request) -> DreamStatusResponse:
    """查询治理任务运行状态。

    由前端轮询调用，用于展示「执行中」提示。
    """
    return DreamStatusResponse(**_get_running_status())


# ------------------------------------------------------------------
# Backup endpoints
# ------------------------------------------------------------------


@router.get("/backups", response_model=BackupListResponse)
async def list_backup_files(request: Request) -> BackupListResponse:
    """List all backup files in the backup directory."""
    workspace_dir = _get_workspace_dir(request)
    backup_dir = workspace_dir / BACKUP_DIR

    if not backup_dir.exists():
        return BackupListResponse(files=[], total_size=0, total_files=0)

    data = _load_dream_logs(workspace_dir)
    records = data.get("records", [])

    backup_info_map: dict[str, dict] = {}
    for record in records:
        record_id = record.get("id", "")
        timestamp = record.get("timestamp", "")
        for filename, stats in record.get("file_stats", {}).items():
            backup_path = stats.get("backup_path", "")
            if backup_path:
                backup_info_map[backup_path] = {
                    "original_file": filename,
                    "record_id": record_id,
                    "timestamp": timestamp,
                }

    backup_files: list[BackupFileInfo] = []
    total_size = 0

    for backup_file in backup_dir.glob("*.md"):
        try:
            stat = backup_file.stat()
            backup_path_rel = str(backup_file.relative_to(workspace_dir))
            info = backup_info_map.get(backup_path_rel, {})
            backup_files.append(
                BackupFileInfo(
                    filename=backup_file.name,
                    original_file=info.get(
                        "original_file",
                        _parse_backup_filename(backup_file.name),
                    ),
                    record_id=info.get("record_id", ""),
                    timestamp=info.get("timestamp", ""),
                    size=stat.st_size,
                    created_at=datetime.fromtimestamp(
                        stat.st_ctime,
                    ).isoformat(),
                ),
            )
            total_size += stat.st_size
        except Exception as e:
            logger.error(f"Failed to read backup file {backup_file}: {e}")

    backup_files.sort(key=lambda x: x.created_at, reverse=True)
    return BackupListResponse(
        files=backup_files,
        total_size=total_size,
        total_files=len(backup_files),
    )


@router.delete("/backups", response_model=DeleteBackupResponse)
async def delete_all_backups(request: Request) -> DeleteBackupResponse:
    """Delete all backup files."""
    workspace_dir = _get_workspace_dir(request)
    backup_dir = workspace_dir / BACKUP_DIR

    if not backup_dir.exists():
        return DeleteBackupResponse(
            success=True,
            message="No backup directory found",
            files_deleted=[],
        )

    deleted_files: list[str] = []
    for backup_file in backup_dir.glob("*.md"):
        try:
            backup_file.unlink()
            deleted_files.append(backup_file.name)
            logger.info(f"Deleted backup file: {backup_file}")
        except Exception as e:
            logger.error(f"Failed to delete backup file {backup_file}: {e}")

    return DeleteBackupResponse(
        success=True,
        message=f"Deleted {len(deleted_files)} backup files",
        files_deleted=deleted_files,
    )


@router.delete("/backups/{filename}", response_model=DeleteBackupResponse)
async def delete_single_backup(
    request: Request,
    filename: str,
) -> DeleteBackupResponse:
    """Delete a specific backup file."""
    workspace_dir = _get_workspace_dir(request)
    backup_file = workspace_dir / BACKUP_DIR / filename

    if not backup_file.exists():
        raise HTTPException(status_code=404, detail="Backup file not found")

    try:
        backup_file.unlink()
        logger.info(f"Deleted backup file: {backup_file}")
        return DeleteBackupResponse(
            success=True,
            message=f"Deleted backup file: {filename}",
            files_deleted=[filename],
        )
    except Exception as e:
        logger.error(f"Failed to delete backup file {backup_file}: {e}")
        return DeleteBackupResponse(
            success=False,
            message=f"Failed to delete backup file: {str(e)}",
            files_deleted=[],
        )


@router.get(
    "/backups/{filename}/content",
    response_model=BackupContentResponse,
)
async def get_backup_content(
    request: Request,
    filename: str,
) -> BackupContentResponse:
    """Get content of a specific backup file for preview."""
    workspace_dir = _get_workspace_dir(request)
    backup_file = workspace_dir / BACKUP_DIR / filename

    if not backup_file.exists():
        raise HTTPException(status_code=404, detail="Backup file not found")

    try:
        content = backup_file.read_text(encoding="utf-8")
        stat = backup_file.stat()

        # Find original file from dream logs
        data = _load_dream_logs(workspace_dir)
        records = data.get("records", [])
        original_file = ""
        for record in records:
            for fname, stats in record.get("file_stats", {}).items():
                if stats.get("backup_path", "").endswith(filename):
                    original_file = fname
                    break
            if original_file:
                break

        if not original_file:
            # Guess from filename pattern
            original_file = _parse_backup_filename(filename)

        return BackupContentResponse(
            filename=filename,
            content=content,
            size=stat.st_size,
            original_file=original_file,
        )
    except Exception as e:
        logger.error(f"Failed to read backup file {backup_file}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read backup file: {str(e)}",
        )


# ------------------------------------------------------------------
# Orphan files endpoints
# ------------------------------------------------------------------


def _scan_orphan_files(workspace_dir: Path) -> list[OrphanFileInfo]:
    """Scan workspace directory for orphan files.

    Returns files that are NOT in the keep list and NOT hidden files.
    Also scans static directory which is considered cleanup target.
    """
    orphan_files: list[OrphanFileInfo] = []

    if not workspace_dir.exists():
        return orphan_files

    def scan_directory(dir_path: Path, relative_base: Path) -> None:
        """Recursively scan a directory for orphan files."""
        try:
            for item in dir_path.iterdir():
                relative_path = item.relative_to(relative_base).as_posix()
                if item.name.startswith("."):
                    continue

                # Skip directories in keep list (at root level only)
                if item.is_dir():
                    if item.name in KEEP_DIRS and dir_path == workspace_dir:
                        continue
                    # Recursively scan subdirectories (including static)
                    scan_directory(item, relative_base)
                    continue

                # Skip files in keep list (at root level only)
                if item.is_file() and dir_path == workspace_dir:
                    if item.name in KEEP_FILES:
                        continue

                # Process files
                if item.is_file():
                    try:
                        stat = item.stat()
                        if relative_path in _protected_path_set(workspace_dir):
                            continue
                        orphan_files.append(
                            OrphanFileInfo(
                                filename=item.name,
                                size=stat.st_size,
                                created_at=datetime.fromtimestamp(
                                    stat.st_ctime,
                                ).isoformat(),
                                modified_at=datetime.fromtimestamp(
                                    stat.st_mtime,
                                ).isoformat(),
                                path=relative_path,  # Relative to workspace_dir
                                full_path=str(item),  # Absolute path
                            ),
                        )
                    except Exception as e:
                        logger.error(f"Failed to read file {item}: {e}")
        except Exception as e:
            logger.error(f"Failed to scan directory {dir_path}: {e}")

    # Start scanning from workspace root
    scan_directory(workspace_dir, workspace_dir)

    # Sort by modified time (most recent first)
    orphan_files.sort(key=lambda x: x.modified_at, reverse=True)
    return orphan_files


@router.get("/orphan-files", response_model=OrphanFilesResponse)
async def list_orphan_files(request: Request) -> OrphanFilesResponse:
    """List orphan files in workspace directory.

    Orphan files are files that are NOT in the standard keep list
    (core config files, system data files, and standard directories).
    """
    workspace_dir = _get_workspace_dir(request)
    orphan_files = _scan_orphan_files(workspace_dir)

    total_size = sum(f.size for f in orphan_files)
    return OrphanFilesResponse(
        files=orphan_files,
        total_size=total_size,
        total_files=len(orphan_files),
        workspace_dir=str(workspace_dir),
    )


def _archive_workspace_files(
    workspace_dir: Path,
    filepaths: list[str],
    *,
    actor: str,
    reason: str,
) -> list[ArchiveItem]:
    """把工作区文件移动到归档区并写入归档索引。"""
    index = _load_archive_index(workspace_dir)
    items = list(index.get("items") or [])
    archived_items: list[ArchiveItem] = []
    now = _isoformat(_utc_now())
    for filepath in filepaths:
        relative_path, file_path = _resolve_workspace_file(
            workspace_dir,
            filepath,
        )
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        stat = file_path.stat()
        item_id = uuid.uuid4().hex
        archive_relative_path = f"{ARCHIVE_FILES_DIR}/{item_id}"
        archive_file = workspace_dir / archive_relative_path
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(file_path), str(archive_file))
        item = {
            "id": item_id,
            "original_path": relative_path,
            "archive_path": archive_relative_path,
            "size_bytes": stat.st_size,
            "mtime": _isoformat(
                datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            ),
            "archived_at": now,
            "archived_by": actor,
            "archive_reason": reason,
        }
        items.append(item)
        archived_items.append(_archive_item_model(item))

    index["items"] = items
    _save_archive_index(workspace_dir, index)
    return archived_items


def _old_orphan_file_candidates(workspace_dir: Path) -> list[str]:
    """找出超过自动归档阈值且未受保护的孤立文件。"""
    cutoff = _utc_now() - timedelta(days=AUTO_ARCHIVE_DAYS)
    candidates: list[str] = []
    for orphan_file in _scan_orphan_files(workspace_dir):
        file_path = workspace_dir / Path(*orphan_file.path.split("/"))
        try:
            mtime = datetime.fromtimestamp(
                file_path.stat().st_mtime,
                timezone.utc,
            )
        except OSError:
            continue
        if mtime <= cutoff:
            candidates.append(orphan_file.path)
    return candidates


def _expired_archive_item_ids(workspace_dir: Path) -> set[str]:
    """找出超过归档保留期的归档条目。"""
    index = _load_archive_index(workspace_dir)
    return {
        str(item.get("id") or "")
        for item in index.get("items", [])
        if isinstance(item, dict)
        and item.get("id")
        and _archive_item_expired(item)
    }


def run_dream_archive_maintenance(
    workspace_dir: Path,
    *,
    actor: str = "dream",
) -> DreamArchiveMaintenanceResult:
    """在 dream 完成后执行当前工作区的归档维护。"""
    archived_items = _archive_workspace_files(
        workspace_dir,
        _old_orphan_file_candidates(workspace_dir),
        actor=actor,
        reason="dream_auto_mtime_3_days",
    )
    logger.info(
        "Dream archive maintenance completed: archived=%d purged=0",
        len(archived_items),
    )
    return DreamArchiveMaintenanceResult(
        archived_items=archived_items,
    )


@router.post(
    "/orphan-files/archive",
    response_model=ArchiveOperationResponse,
)
async def archive_orphan_files(
    request: Request,
    body: ArchiveFileRequest,
) -> ArchiveOperationResponse:
    """手动归档当前工作区的孤立文件。"""
    workspace_dir = _get_workspace_dir(request)
    actor, _ = _request_actor(request)
    if not body.files:
        raise HTTPException(status_code=400, detail="No files provided")
    items = _archive_workspace_files(
        workspace_dir,
        body.files,
        actor=actor,
        reason=body.reason or "manual",
    )
    source_id = _get_optional_source_id(request)
    if source_id:
        await _dual_write_archive_items(
            request,
            source_id=source_id,
            target_user_id=_get_logical_tenant_id(request),
            target_agent_id=_get_agent_id(request),
            items=items,
        )
    return ArchiveOperationResponse(
        success=True,
        message="Archived files",
        files_archived=[item.original_path for item in items],
        items=items,
    )


@router.post(
    "/orphan-files/archive-auto-run",
    response_model=ArchiveOperationResponse,
)
async def archive_old_orphan_files(
    request: Request,
) -> ArchiveOperationResponse:
    """自动归档超过 3 天未修改的孤立文件。"""
    workspace_dir = _get_workspace_dir(request)
    candidates = _old_orphan_file_candidates(workspace_dir)

    actor, _ = _request_actor(request)
    items = _archive_workspace_files(
        workspace_dir,
        candidates,
        actor=actor,
        reason="auto_mtime_3_days",
    )
    source_id = _get_optional_source_id(request)
    if source_id:
        await _dual_write_archive_items(
            request,
            source_id=source_id,
            target_user_id=_get_logical_tenant_id(request),
            target_agent_id=_get_agent_id(request),
            items=items,
        )
    return ArchiveOperationResponse(
        success=True,
        message="Auto archived files",
        files_archived=[item.original_path for item in items],
        items=items,
    )


@router.get(
    "/orphan-files/{filepath:path}/content",
    response_model=OrphanFileContentResponse,
)
async def get_orphan_file_content(
    request: Request,
    filepath: str,
) -> OrphanFileContentResponse:
    """Get content of an orphan file for preview."""
    workspace_dir = _get_workspace_dir(request)
    _, file_path = _resolve_workspace_file(workspace_dir, filepath)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Security check: ensure file is within workspace_dir
    try:
        file_path.resolve().relative_to(workspace_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    stat = file_path.stat()
    file_type = _get_file_type(file_path)

    try:
        if file_type == "image":
            # Read image as base64
            content_bytes = file_path.read_bytes()
            content = base64.b64encode(content_bytes).decode("utf-8")
            return OrphanFileContentResponse(
                filename=filepath,
                content=content,
                size=stat.st_size,
                file_type="image",
                is_loadable=True,
            )
        elif file_type == "text":
            # Read text file
            content = file_path.read_text(encoding="utf-8")
            return OrphanFileContentResponse(
                filename=filepath,
                content=content,
                size=stat.st_size,
                file_type="text",
                is_loadable=True,
            )
        else:
            # Binary file - cannot preview
            return OrphanFileContentResponse(
                filename=filepath,
                content="",
                size=stat.st_size,
                file_type="binary",
                is_loadable=False,
                error_message="Binary file cannot be previewed",
            )
    except UnicodeDecodeError:
        # Text file with non-UTF8 encoding
        return OrphanFileContentResponse(
            filename=filepath,
            content="",
            size=stat.st_size,
            file_type="text",
            is_loadable=False,
            error_message="File encoding is not UTF-8, cannot preview",
        )
    except Exception as e:
        logger.error(f"Failed to read orphan file {file_path}: {e}")
        return OrphanFileContentResponse(
            filename=filepath,
            content="",
            size=stat.st_size,
            file_type="error",
            is_loadable=False,
            error_message=f"Failed to read file: {str(e)}",
        )


@router.delete(
    "/orphan-files/{filepath:path}",
    response_model=DeleteBackupResponse,
)
async def delete_orphan_file(
    request: Request,
    filepath: str,
) -> DeleteBackupResponse:
    """Delete an orphan file."""
    workspace_dir = _get_workspace_dir(request)
    _, file_path = _resolve_workspace_file(workspace_dir, filepath)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Security check: ensure file is within workspace_dir
    try:
        file_path.resolve().relative_to(workspace_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    # Extra check: ensure file is NOT in keep list
    if file_path.name in KEEP_FILES:
        raise HTTPException(
            status_code=403,
            detail="Cannot delete protected file",
        )

    try:
        file_path.unlink()
        logger.info(f"Deleted orphan file: {file_path}")
        return DeleteBackupResponse(
            success=True,
            message=f"Deleted file: {filepath}",
            files_deleted=[filepath],
        )
    except Exception as e:
        logger.error(f"Failed to delete orphan file {file_path}: {e}")
        return DeleteBackupResponse(
            success=False,
            message=f"Failed to delete file: {str(e)}",
            files_deleted=[],
        )


async def _source_archive_workspaces(
    request: Request,
    *,
    agent_id: Optional[str] = None,
) -> list[tuple[str, str, Path]]:
    """列出当前渠道下可管理用户的 Agent 工作区。"""
    source_id = _get_report_source_id(request)
    workspace_root = _get_workspace_root(request)
    try:
        tenants = await _load_source_tenants(source_id)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        tenants = [{"tenant_id": _get_logical_tenant_id(request)}]
    workspaces: list[tuple[str, str, Path]] = []
    for tenant in tenants:
        tenant_id = str(tenant.get("tenant_id") or "")
        if not tenant_id:
            continue
        tenant_dir = _resolve_tenant_dir(workspace_root, tenant_id, source_id)
        for current_agent_id, workspace_dir in _iter_agent_workspace_dirs(
            tenant_dir,
            agent_id=agent_id,
        ):
            workspaces.append((tenant_id, current_agent_id, workspace_dir))
    return workspaces


async def _ensure_target_user_in_current_source(
    request: Request,
    target_user_id: str,
) -> None:
    """确认目标用户属于当前渠道可管理范围。"""
    source_id = _get_report_source_id(request)
    try:
        tenants = await _load_source_tenants(source_id)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        if target_user_id == _get_logical_tenant_id(request):
            return
        raise
    allowed_user_ids = {
        str(tenant.get("tenant_id") or "") for tenant in tenants
    }
    if target_user_id not in allowed_user_ids:
        raise HTTPException(status_code=403, detail="Target user out of scope")


async def _file_report_target_user_ids(
    request: Request,
    *,
    source_id: str,
    target_user_id: Optional[str],
    bbk_id: Optional[str],
    user_search: Optional[str],
) -> list[str]:
    """按用户维度收窄文件治理报表，不接收记录维度过滤。"""
    tenants = _filter_source_tenants(
        await _load_source_tenants(source_id),
        bbk_id=bbk_id,
        user_search=user_search,
    )
    allowed_user_ids = {str(row.get("tenant_id") or "") for row in tenants}
    if target_user_id:
        await _ensure_target_user_in_current_source(request, target_user_id)
        return [target_user_id] if target_user_id in allowed_user_ids else []
    return sorted(allowed_user_ids)


def _archive_db_row_to_response(row: Any) -> ArchiveItem:
    """把数据库归档状态行转换为管理侧响应模型。"""
    return ArchiveItem(
        id=row.archive_item_id,
        original_path=row.original_path,
        archive_path=row.archive_path,
        size_bytes=row.size_bytes,
        mtime=row.mtime,
        archived_at=row.archived_at,
        archived_by=row.archived_by,
        archive_reason=row.archive_reason,
        target_user_id=row.target_user_id,
        target_agent_id=row.target_agent_id,
        expired=_archive_item_expired({"archived_at": row.archived_at}),
    )


def _archive_row_value(
    row: Any,
    item: dict[str, Any],
    row_field: str,
    item_field: str,
    default: Any,
) -> Any:
    """按读模型优先、本地 raw_item 兜底的顺序读取归档字段。"""
    value = getattr(row, row_field, None)
    if value:
        return value
    value = item.get(item_field)
    if value:
        return value
    return default


def _archive_row_text(
    row: Any,
    item: dict[str, Any],
    row_field: str,
    item_field: str,
) -> str:
    """读取归档文本字段，并保持旧逻辑的字符串化行为。"""
    return str(_archive_row_value(row, item, row_field, item_field, ""))


def _archive_row_int(
    row: Any,
    item: dict[str, Any],
    row_field: str,
    item_field: str,
) -> int:
    """读取归档数字字段，并保持旧逻辑的 int 转换行为。"""
    return int(_archive_row_value(row, item, row_field, item_field, 0))


def _archive_db_row_to_index_item(row: Any) -> dict[str, Any]:
    """把读模型归档行转换为本地索引条目结构，用于补齐清理目标。"""
    raw_item = getattr(row, "raw_item", None)
    item = dict(raw_item) if isinstance(raw_item, dict) else {}
    item.update(
        {
            "id": _archive_row_text(row, item, "archive_item_id", "id"),
            "original_path": _archive_row_text(
                row,
                item,
                "original_path",
                "original_path",
            ),
            "archive_path": _archive_row_text(
                row,
                item,
                "archive_path",
                "archive_path",
            ),
            "size_bytes": _archive_row_int(
                row,
                item,
                "size_bytes",
                "size_bytes",
            ),
            "mtime": _archive_row_text(row, item, "mtime", "mtime"),
            "archived_at": _archive_row_text(
                row,
                item,
                "archived_at",
                "archived_at",
            ),
            "archived_by": _archive_row_text(
                row,
                item,
                "archived_by",
                "archived_by",
            ),
            "archive_reason": _archive_row_text(
                row,
                item,
                "archive_reason",
                "archive_reason",
            ),
        },
    )
    return item


async def _load_archive_read_model_rows(
    service: Any,
    *,
    source_id: str,
    target_user_ids: list[str],
    target_agent_id: Optional[str],
) -> list[Any]:
    """读取归档读模型，失败时退回本地索引清理路径。"""
    if service is None or not target_user_ids:
        return []
    store = getattr(service, "store", None)
    list_archive_items = getattr(store, "list_archive_items", None)
    if list_archive_items is None:
        return []
    try:
        return await list_archive_items(
            source_id,
            target_user_ids=target_user_ids,
            target_agent_id=target_agent_id,
        )
    except Exception as exc:
        logger.warning("Failed to load archive read model rows: %s", exc)
        return []


def _archive_index_expired_item_ids(workspace_dir: Path) -> set[str]:
    """按过期清理入口的旧语义从本地索引读取过期条目 id。"""
    return {
        str(item.get("id") or "")
        for item in _load_archive_index(workspace_dir).get("items", [])
        if _archive_item_expired(item)
    }


def _archive_purge_group(
    workspace_dir: Path,
    archive_item_ids: set[str],
) -> dict[str, Any]:
    """构造同一用户和 Agent 下的一组待清理归档条目。"""
    return {
        "workspace_dir": workspace_dir,
        "archive_item_ids": set(archive_item_ids),
        "fallback_items": [],
    }


def _expired_archive_groups_from_indexes(
    workspace_entries: list[tuple[str, str, Path]],
    target_user_id: Optional[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """从各工作区本地索引收集已过期的归档清理分组。"""
    pending_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for tenant_id, agent_id, workspace_dir in workspace_entries:
        if target_user_id and tenant_id != target_user_id:
            continue
        expired_ids = _archive_index_expired_item_ids(workspace_dir)
        if not expired_ids:
            continue
        pending_groups[(tenant_id, agent_id)] = _archive_purge_group(
            workspace_dir,
            expired_ids,
        )
    return pending_groups


def _archive_read_model_target_user_ids(
    workspace_entries: list[tuple[str, str, Path]],
    target_user_id: Optional[str],
) -> list[str]:
    """计算允许从读模型补齐归档记录的目标用户范围。"""
    allowed_target_user_ids = sorted(
        {tenant_id for tenant_id, _, _ in workspace_entries},
    )
    if target_user_id is None:
        return allowed_target_user_ids
    if target_user_id in allowed_target_user_ids:
        return [target_user_id]
    return []


def _expired_archive_row_context(
    request: Request,
    source_id: str,
    row: Any,
) -> Optional[tuple[str, str, Path, str, dict[str, Any]]]:
    """把已过期的读模型归档行转换为清理分组所需上下文。"""
    if not _archive_item_expired(
        {"archived_at": getattr(row, "archived_at", "")},
    ):
        return None
    archive_item_id = str(getattr(row, "archive_item_id", "") or "")
    if not archive_item_id:
        return None
    tenant_id = str(getattr(row, "target_user_id", "") or "")
    agent_id = str(
        getattr(row, "target_agent_id", None) or DEFAULT_REPORT_AGENT_ID,
    )
    workspace_dir = _target_workspace_dir(
        _get_workspace_root(request),
        tenant_id,
        source_id,
        agent_id,
    )
    return (
        tenant_id,
        agent_id,
        workspace_dir,
        archive_item_id,
        _archive_db_row_to_index_item(row),
    )


def _append_expired_archive_read_model_rows(
    request: Request,
    source_id: str,
    pending_groups: dict[tuple[str, str], dict[str, Any]],
    read_model_rows: list[Any],
) -> None:
    """用读模型补齐本地索引缺失的过期归档记录。"""
    for row in read_model_rows:
        row_context = _expired_archive_row_context(request, source_id, row)
        if row_context is None:
            continue
        tenant_id, agent_id, workspace_dir, archive_item_id, item = row_context
        group = pending_groups.setdefault(
            (tenant_id, agent_id),
            _archive_purge_group(workspace_dir, set()),
        )
        group["archive_item_ids"].add(archive_item_id)
        group["fallback_items"].append(item)


def _protected_db_row_to_response(row: Any) -> ProtectedFileInfo:
    """把数据库保护文件状态行转换为管理侧响应模型。"""
    return ProtectedFileInfo(
        target_user_id=row.target_user_id,
        target_agent_id=row.target_agent_id,
        path=row.path,
        protected_at=row.protected_at,
        protected_by=row.protected_by,
        reason=row.reason,
        exists=row.exists,
        size_bytes=row.size_bytes,
        mtime=row.mtime,
    )


def _find_archive_item(
    workspace_dir: Path,
    archive_item_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """在归档索引中查找指定归档项。"""
    index = _load_archive_index(workspace_dir)
    for item in index.get("items", []):
        if str(item.get("id") or "") == archive_item_id:
            return index, item
    raise HTTPException(status_code=404, detail="Archive item not found")


def _remove_archive_items_from_index(
    workspace_dir: Path,
    ids_to_remove: set[str],
) -> None:
    """从归档索引移除指定归档项。"""
    index = _load_archive_index(workspace_dir)
    index["items"] = [
        item
        for item in index.get("items", [])
        if str(item.get("id") or "") not in ids_to_remove
    ]
    _save_archive_index(workspace_dir, index)


def _add_protected_path(
    workspace_dir: Path,
    relative_path: str,
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """把恢复后的文件路径加入保护名单。"""
    data = _load_protected_paths(workspace_dir)
    paths = list(data.get("paths") or [])
    now = _isoformat(_utc_now())
    normalised_path = _normalise_workspace_relative_path(relative_path)
    next_paths = [
        item
        for item in paths
        if not (
            isinstance(item, dict)
            and _normalise_workspace_relative_path(str(item.get("path") or ""))
            == normalised_path
        )
    ]
    protected_item = {
        "path": normalised_path,
        "protected_at": now,
        "protected_by": actor,
        "reason": reason,
    }
    next_paths.append(protected_item)
    data["paths"] = next_paths
    _save_protected_paths(workspace_dir, data)
    return protected_item


def _remove_protected_path(workspace_dir: Path, relative_path: str) -> bool:
    """从保护名单中移除指定工作区相对路径。"""
    data = _load_protected_paths(workspace_dir)
    normalised_path = _normalise_workspace_relative_path(relative_path)
    paths = list(data.get("paths") or [])
    next_paths = [
        item
        for item in paths
        if not (
            isinstance(item, dict)
            and _normalise_workspace_relative_path(str(item.get("path") or ""))
            == normalised_path
        )
    ]
    if len(next_paths) == len(paths):
        return False
    data["paths"] = next_paths
    _save_protected_paths(workspace_dir, data)
    return True


@router.get("/archive/items", response_model=ArchiveItemsResponse)
async def list_archive_items(
    request: Request,
    target_user_id: Optional[str] = None,
    target_agent_id: Optional[str] = None,
    bbk_id: Optional[str] = None,
    user_search: Optional[str] = None,
    expired: Optional[bool] = None,
    page: int = 1,
    page_size: int = 20,
) -> ArchiveItemsResponse:
    """管理员查询当前渠道下的归档文件。"""
    _ensure_report_permission(request)
    safe_page, safe_page_size = _normalise_page(page, page_size)
    source_id = _get_report_source_id(request)
    service = _get_continuous_governance_service(request)
    safe_agent_id = _optional_target_agent_id(target_agent_id)
    target_user_ids = await _file_report_target_user_ids(
        request,
        source_id=source_id,
        target_user_id=target_user_id,
        bbk_id=bbk_id,
        user_search=user_search,
    )
    rows, total = await service.store.list_archive_items_page(
        source_id,
        target_user_ids=target_user_ids,
        target_agent_id=safe_agent_id,
        expired=expired,
        limit=safe_page_size,
        offset=(safe_page - 1) * safe_page_size,
    )
    items = [_archive_db_row_to_response(row) for row in rows]
    return ArchiveItemsResponse(
        items=items,
        total=total,
        page=safe_page,
        page_size=safe_page_size,
    )


@router.get("/archive/protected-files", response_model=ProtectedFilesResponse)
async def list_protected_files(
    request: Request,
    target_user_id: Optional[str] = None,
    target_agent_id: Optional[str] = None,
    bbk_id: Optional[str] = None,
    user_search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> ProtectedFilesResponse:
    """管理员查询当前渠道下的保护文件。"""
    _ensure_report_permission(request)
    safe_page, safe_page_size = _normalise_page(page, page_size)
    source_id = _get_report_source_id(request)
    service = _get_continuous_governance_service(request)
    safe_agent_id = _optional_target_agent_id(target_agent_id)
    target_user_ids = await _file_report_target_user_ids(
        request,
        source_id=source_id,
        target_user_id=target_user_id,
        bbk_id=bbk_id,
        user_search=user_search,
    )
    protected_rows = await service.store.list_protected_files(
        source_id,
        target_user_ids=target_user_ids,
        target_agent_id=safe_agent_id,
    )
    rows = [_protected_db_row_to_response(row) for row in protected_rows]
    rows.sort(key=lambda item: item.protected_at, reverse=True)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    return ProtectedFilesResponse(
        items=rows[start:end],
        total=len(rows),
        page=safe_page,
        page_size=safe_page_size,
    )


@router.delete(
    "/archive/protected-files",
    response_model=ProtectedFileRemoveResponse,
)
async def remove_protected_file(
    request: Request,
    body: ProtectedFileRemoveRequest,
) -> ProtectedFileRemoveResponse:
    """管理员取消指定路径的保护，后续扫描和归档会重新纳入该文件。"""
    _ensure_report_permission(request)
    await _ensure_target_user_in_current_source(request, body.target_user_id)
    relative_path = _normalise_workspace_relative_path(body.path)
    workspace_dir = _target_workspace_dir(
        _get_workspace_root(request),
        body.target_user_id,
        _get_report_source_id(request),
        body.target_agent_id,
    )
    removed = _remove_protected_path(workspace_dir, relative_path)
    if not removed:
        raise HTTPException(status_code=404, detail="Protected file not found")
    source_id = _get_report_source_id(request)
    service = _get_optional_continuous_governance_service(request)
    if service is not None:
        try:
            await service.delete_protected_file(
                source_id=source_id,
                target_user_id=body.target_user_id,
                target_agent_id=body.target_agent_id,
                path=relative_path,
            )
        except Exception as exc:
            await _record_dual_write_health(
                request,
                source_id=source_id,
                target_user_id=body.target_user_id,
                target_agent_id=body.target_agent_id,
                entity_type="protected_file",
                entity_id=relative_path,
                error=exc,
                payload={"path": relative_path, "operation": "remove"},
            )
    return ProtectedFileRemoveResponse(
        success=True,
        message="Protected file removed",
        removed_path=relative_path,
    )


@router.post("/archive/restore", response_model=ArchiveRestoreResponse)
async def restore_archive_item(
    request: Request,
    body: ArchiveRestoreRequest,
) -> ArchiveRestoreResponse:
    """管理员恢复归档文件，可选择恢复后加入保护名单。"""
    _ensure_report_permission(request)
    await _ensure_target_user_in_current_source(request, body.target_user_id)
    source_id = _get_report_source_id(request)
    workspace_dir = _target_workspace_dir(
        _get_workspace_root(request),
        body.target_user_id,
        source_id,
        body.target_agent_id,
    )
    actor, _ = _request_actor(request)
    original_path, protected_payload = _restore_archive_item_locally(
        workspace_dir,
        body.archive_item_id,
        actor=actor,
        protect_after_restore=body.protect_after_restore,
    )
    service = _get_optional_continuous_governance_service(request)
    await _sync_archive_restore_to_service(
        request,
        service,
        source_id,
        body,
        original_path,
        protected_payload,
    )
    return ArchiveRestoreResponse(
        success=True,
        message="Restored archive item",
        restored_path=original_path,
        protected=body.protect_after_restore,
    )


def _build_restored_protected_payload(
    restore_path: Path,
    protected_item: dict[str, Any] | None,
    actor: str,
) -> dict[str, Any]:
    stat = restore_path.stat() if restore_path.exists() else None
    return {
        "protected_at": str(
            (protected_item or {}).get("protected_at") or "",
        ),
        "protected_by": str(
            (protected_item or {}).get("protected_by") or actor,
        ),
        "exists": restore_path.exists(),
        "size_bytes": stat.st_size if stat else None,
        "mtime": _file_mtime_iso(restore_path) if stat else None,
    }


def _restore_archive_item_locally(
    workspace_dir: Path,
    archive_item_id: str,
    *,
    actor: str,
    protect_after_restore: bool,
) -> tuple[str, dict[str, Any]]:
    index, item = _find_archive_item(workspace_dir, archive_item_id)
    original_path = _normalise_workspace_relative_path(
        str(item.get("original_path") or ""),
    )
    archive_path = _normalise_workspace_relative_path(
        str(item.get("archive_path") or ""),
    )
    archive_file = workspace_dir / Path(*archive_path.split("/"))
    restore_path = workspace_dir / Path(*original_path.split("/"))
    if not archive_file.exists():
        raise HTTPException(status_code=404, detail="Archived file not found")
    if restore_path.exists():
        raise HTTPException(status_code=409, detail="Restore target exists")

    restore_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(archive_file), str(restore_path))
    index["items"] = [
        row
        for row in index.get("items", [])
        if str(row.get("id") or "") != archive_item_id
    ]
    _save_archive_index(workspace_dir, index)

    if not protect_after_restore:
        return original_path, {}

    protected_item = _add_protected_path(
        workspace_dir,
        original_path,
        actor=actor,
        reason="restored_from_archive",
    )
    return original_path, _build_restored_protected_payload(
        restore_path,
        protected_item,
        actor,
    )


async def _sync_archive_restore_to_service(
    request: Request,
    service: Any,
    source_id: str,
    body: ArchiveRestoreRequest,
    original_path: str,
    protected_payload: dict[str, Any],
) -> None:
    if service is None:
        return
    try:
        await service.delete_archive_items(
            source_id=source_id,
            target_user_id=body.target_user_id,
            target_agent_id=body.target_agent_id,
            archive_item_ids=[body.archive_item_id],
        )
        if body.protect_after_restore:
            await service.upsert_protected_file(
                source_id=source_id,
                target_user_id=body.target_user_id,
                target_agent_id=body.target_agent_id,
                path=original_path,
                protected_at=protected_payload["protected_at"],
                protected_by=protected_payload["protected_by"],
                reason="restored_from_archive",
                exists=protected_payload["exists"],
                size_bytes=protected_payload["size_bytes"],
                mtime=protected_payload["mtime"],
            )
    except Exception as exc:
        await _record_dual_write_health(
            request,
            source_id=source_id,
            target_user_id=body.target_user_id,
            target_agent_id=body.target_agent_id,
            entity_type="archive_restore",
            entity_id=body.archive_item_id,
            error=exc,
            payload={
                "archive_item_id": body.archive_item_id,
                "original_path": original_path,
                "protect_after_restore": body.protect_after_restore,
                **protected_payload,
            },
        )


def _purge_archive_items(
    workspace_dir: Path,
    archive_item_ids: set[str],
    *,
    fallback_items: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[str], int]:
    """删除归档文件并返回原路径列表和释放大小。"""
    index = _load_archive_index(workspace_dir)
    items = list(index.get("items", []))
    indexed_ids = {str(item.get("id") or "") for item in items}
    for item in fallback_items or []:
        item_id = str(item.get("id") or "")
        if item_id in archive_item_ids and item_id not in indexed_ids:
            items.append(item)
            indexed_ids.add(item_id)
    deleted_paths: list[str] = []
    total_size = 0
    found_ids: set[str] = set()
    for item in items:
        item_id = str(item.get("id") or "")
        if item_id not in archive_item_ids:
            continue
        found_ids.add(item_id)
        archive_path = _normalise_workspace_relative_path(
            str(item.get("archive_path") or ""),
        )
        archive_file = workspace_dir / Path(*archive_path.split("/"))
        if archive_file.exists():
            archive_file.unlink()
        deleted_paths.append(str(item.get("original_path") or ""))
        total_size += int(item.get("size_bytes") or 0)
    if found_ids != archive_item_ids:
        raise HTTPException(status_code=404, detail="Archive item not found")
    _remove_archive_items_from_index(workspace_dir, archive_item_ids)
    return deleted_paths, total_size


def _build_purge_audit_record(
    request: Request,
    *,
    event_id: str,
    operation: str,
    target_user_id: str,
    target_agent_id: str,
    files_count: int,
    total_size_bytes: int,
    reason: str,
    status: str = "success",
    error: Optional[str] = None,
) -> dict[str, Any]:
    """构造管理员归档清理审计事件。"""
    actor, role = _request_actor(request)
    source_id = _get_report_source_id(request)
    source_name = request.headers.get("X-Source-Name") or source_id
    return _build_purge_audit_payload(
        event_id=event_id,
        operation=operation,
        actor_user_id=actor,
        actor_role=role,
        source_id=source_id,
        source_name=source_name,
        target_user_id=target_user_id,
        target_agent_id=target_agent_id,
        scope="selected",
        files_count=files_count,
        total_size_bytes=total_size_bytes,
        reason=reason,
        status=status,
        error=error,
    )


def _build_purge_audit_payload(
    *,
    event_id: str,
    operation: str,
    actor_user_id: str,
    actor_role: str,
    source_id: str,
    source_name: str,
    target_user_id: str,
    target_agent_id: str,
    scope: str,
    files_count: int,
    total_size_bytes: int,
    reason: str,
    status: str = "success",
    error: Optional[str] = None,
) -> dict[str, Any]:
    """构造可跨请求和定时任务复用的清理审计 payload。"""
    return {
        "event_id": event_id,
        "timestamp": _isoformat(_utc_now()),
        "operation": operation,
        "status": status,
        "actor_user_id": actor_user_id,
        "actor_role": actor_role,
        "source_id": source_id,
        "source_name": source_name,
        "target_user_id": target_user_id,
        "target_agent_id": target_agent_id,
        "scope": scope,
        "files_count": files_count,
        "total_size_bytes": total_size_bytes,
        "reason": reason,
        "error": error,
    }


async def _purge_expired_archive_group(
    request: Request,
    service: Any,
    source_id: str,
    tenant_id: str,
    agent_id: str,
    group: dict[str, Any],
    reason: str,
) -> tuple[list[str], int, str]:
    """清理单个用户和 Agent 组合下的过期归档并同步审计状态。"""
    expired_ids = group["archive_item_ids"]
    deleted_paths, deleted_size = _purge_archive_items(
        group["workspace_dir"],
        expired_ids,
        fallback_items=group["fallback_items"],
    )
    event_id = uuid.uuid4().hex
    audit = _build_purge_audit_record(
        request,
        event_id=event_id,
        operation="auto_purge_archive",
        target_user_id=tenant_id,
        target_agent_id=agent_id,
        files_count=len(deleted_paths),
        total_size_bytes=deleted_size,
        reason=reason,
    )
    audit["scope"] = "expired_10_days"
    _append_archive_admin_audit(_get_workspace_dir(request), audit)
    if service is not None:
        try:
            await service.delete_archive_items(
                source_id=source_id,
                target_user_id=tenant_id,
                target_agent_id=agent_id,
                archive_item_ids=list(expired_ids),
            )
            await service.upsert_cleanup_audit(audit)
        except Exception as exc:
            await _record_dual_write_health(
                request,
                source_id=source_id,
                target_user_id=tenant_id,
                target_agent_id=agent_id,
                entity_type="cleanup_audit",
                entity_id=event_id,
                error=exc,
                payload={
                    "audit": audit,
                    "archive_item_ids": list(expired_ids),
                },
            )
    return deleted_paths, deleted_size, event_id


async def _purge_expired_archive_groups(
    request: Request,
    service: Any,
    source_id: str,
    pending_groups: dict[tuple[str, str], dict[str, Any]],
    reason: str,
) -> ArchivePurgeResponse:
    """逐组清理过期归档，并汇总为接口响应。"""
    all_deleted: list[str] = []
    total_size = 0
    last_event_id = uuid.uuid4().hex
    for (tenant_id, agent_id), group in pending_groups.items():
        deleted_paths, deleted_size, event_id = (
            await _purge_expired_archive_group(
                request,
                service,
                source_id,
                tenant_id,
                agent_id,
                group,
                reason,
            )
        )
        all_deleted.extend(deleted_paths)
        total_size += deleted_size
        last_event_id = event_id
    return ArchivePurgeResponse(
        success=True,
        message="Purged expired archive items",
        files_deleted=all_deleted,
        files_count=len(all_deleted),
        total_size_bytes=total_size,
        audit_event_id=last_event_id,
    )


async def _purge_archive_items_for_request(
    request: Request,
    body: ArchivePurgeRequest,
) -> ArchivePurgeResponse:
    """管理员手动清理指定归档文件并写入清理审计。"""
    _ensure_report_permission(request)
    if not body.archive_item_ids:
        raise HTTPException(
            status_code=400,
            detail="No archive items provided",
        )
    await _ensure_target_user_in_current_source(request, body.target_user_id)
    source_id = _get_report_source_id(request)
    workspace_dir = _target_workspace_dir(
        _get_workspace_root(request),
        body.target_user_id,
        source_id,
        body.target_agent_id,
    )
    service = _get_optional_continuous_governance_service(request)
    archive_item_ids = set(body.archive_item_ids)
    fallback_rows = await _load_archive_read_model_rows(
        service,
        source_id=source_id,
        target_user_ids=[body.target_user_id],
        target_agent_id=body.target_agent_id,
    )
    fallback_items = [
        _archive_db_row_to_index_item(row)
        for row in fallback_rows
        if str(getattr(row, "archive_item_id", "") or "") in archive_item_ids
    ]
    deleted_paths, total_size = _purge_archive_items(
        workspace_dir,
        archive_item_ids,
        fallback_items=fallback_items,
    )
    event_id = uuid.uuid4().hex
    audit = _build_purge_audit_record(
        request,
        event_id=event_id,
        operation="purge_archive",
        target_user_id=body.target_user_id,
        target_agent_id=body.target_agent_id,
        files_count=len(deleted_paths),
        total_size_bytes=total_size,
        reason=body.reason,
    )
    _append_archive_admin_audit(_get_workspace_dir(request), audit)
    if service is not None:
        try:
            await service.delete_archive_items(
                source_id=source_id,
                target_user_id=body.target_user_id,
                target_agent_id=body.target_agent_id,
                archive_item_ids=body.archive_item_ids,
            )
            await service.upsert_cleanup_audit(audit)
        except Exception as exc:
            await _record_dual_write_health(
                request,
                source_id=source_id,
                target_user_id=body.target_user_id,
                target_agent_id=body.target_agent_id,
                entity_type="cleanup_audit",
                entity_id=event_id,
                error=exc,
                payload={
                    "audit": audit,
                    "archive_item_ids": body.archive_item_ids,
                },
            )
    return ArchivePurgeResponse(
        success=True,
        message="Purged archive items",
        files_deleted=deleted_paths,
        files_count=len(deleted_paths),
        total_size_bytes=total_size,
        audit_event_id=event_id,
    )


@router.delete("/archive/items", response_model=ArchivePurgeResponse)
async def purge_archive_items(
    request: Request,
    body: ArchivePurgeRequest,
) -> ArchivePurgeResponse:
    """兼容旧版 DELETE 调用的手动归档清理入口。"""
    return await _purge_archive_items_for_request(request, body)


@router.post("/archive/items", response_model=ArchivePurgeResponse)
async def purge_archive_items_by_post(
    request: Request,
    body: ArchivePurgeRequest,
) -> ArchivePurgeResponse:
    """避免代理丢弃 DELETE body 的手动归档清理入口。"""
    return await _purge_archive_items_for_request(request, body)


@router.post("/archive/purge-expired", response_model=ArchivePurgeResponse)
async def purge_expired_archive_items(
    request: Request,
    body: Optional[ArchivePurgeExpiredRequest] = Body(default=None),
) -> ArchivePurgeResponse:
    """管理员清理当前渠道下超过 10 天的归档文件。"""
    body = body or ArchivePurgeExpiredRequest()
    _ensure_report_permission(request)
    source_id = _get_report_source_id(request)
    service = _get_optional_continuous_governance_service(request)
    workspace_entries = await _source_archive_workspaces(
        request,
        agent_id=body.target_agent_id,
    )
    pending_groups = _expired_archive_groups_from_indexes(
        workspace_entries,
        body.target_user_id,
    )
    read_model_rows = await _load_archive_read_model_rows(
        service,
        source_id=source_id,
        target_user_ids=_archive_read_model_target_user_ids(
            workspace_entries,
            body.target_user_id,
        ),
        target_agent_id=body.target_agent_id,
    )
    _append_expired_archive_read_model_rows(
        request,
        source_id,
        pending_groups,
        read_model_rows,
    )
    return await _purge_expired_archive_groups(
        request,
        service,
        source_id,
        pending_groups,
        body.reason,
    )


def _collect_admin_audits(
    workspace_root: Path,
    source_id: str,
) -> list[ArchiveAdminAuditRecord]:
    """扫描工作区根目录下当前渠道的管理员清理审计。"""
    records: list[ArchiveAdminAuditRecord] = []
    for audit_path in workspace_root.glob(
        "*/workspaces/*/" + ARCHIVE_ADMIN_AUDIT_FILE,
    ):
        for record in _read_archive_admin_audit(audit_path):
            if record.get("source_id") != source_id:
                continue
            try:
                records.append(ArchiveAdminAuditRecord(**record))
            except Exception:
                continue
    records.sort(key=lambda item: item.timestamp, reverse=True)
    return records


def _build_admin_audit_summary(
    records: list[ArchiveAdminAuditRecord],
) -> ArchiveAdminAuditSummary:
    """根据管理员清理审计明细构造顶部指标。"""
    return ArchiveAdminAuditSummary(
        total_operations=len(records),
        success_operations=sum(
            1 for row in records if row.status == "success"
        ),
        failed_operations=sum(1 for row in records if row.status == "failed"),
        partial_success_operations=sum(
            1 for row in records if row.status == "partial_success"
        ),
        manual_operations=sum(
            1 for row in records if row.operation == "purge_archive"
        ),
        auto_operations=sum(
            1 for row in records if row.operation == "auto_purge_archive"
        ),
        total_files_cleared=sum(row.files_count for row in records),
        total_size_cleared_bytes=sum(row.total_size_bytes for row in records),
        last_operation_at=max(
            (row.timestamp for row in records),
            default=None,
        ),
    )


def _cleanup_audit_to_response(record: Any) -> ArchiveAdminAuditRecord:
    """把数据库清理审计记录转换为既有响应行。"""
    return ArchiveAdminAuditRecord(
        event_id=record.event_id,
        timestamp=record.timestamp,
        operation=record.operation,
        status=record.status,
        actor_user_id=record.actor_user_id,
        actor_role=record.actor_role,
        source_id=record.source_id,
        source_name=record.source_name,
        target_user_id=record.target_user_id,
        target_agent_id=record.target_agent_id,
        scope=record.scope,
        files_count=record.files_count,
        total_size_bytes=record.total_size_bytes,
        reason=record.reason,
        error=record.error,
    )


@router.get("/archive/admin-audits", response_model=ArchiveAdminAuditsResponse)
async def list_archive_admin_audits(
    request: Request,
    target_user_id: Optional[str] = None,
    target_agent_id: Optional[str] = None,
    bbk_id: Optional[str] = None,
    user_search: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> ArchiveAdminAuditsResponse:
    """管理员查询当前渠道下归档清理审计明细和统计。"""
    _ensure_report_permission(request)
    safe_page, safe_page_size = _normalise_page(page, page_size)
    source_id = _get_report_source_id(request)
    service = _get_continuous_governance_service(request)
    safe_agent_id = _optional_target_agent_id(target_agent_id)
    target_user_ids = await _file_report_target_user_ids(
        request,
        source_id=source_id,
        target_user_id=target_user_id,
        bbk_id=bbk_id,
        user_search=user_search,
    )
    records = [
        _cleanup_audit_to_response(record)
        for record in await service.store.list_cleanup_audits(
            source_id,
            target_user_ids=target_user_ids,
            target_agent_id=safe_agent_id,
        )
    ]
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    return ArchiveAdminAuditsResponse(
        summary=_build_admin_audit_summary(records),
        items=records[start:end],
        total=len(records),
        page=safe_page,
        page_size=safe_page_size,
    )


@router.get("/archive/report", response_model=ArchiveReportResponse)
async def get_archive_report(
    request: Request,
    target_user_id: Optional[str] = None,
    target_agent_id: Optional[str] = None,
    bbk_id: Optional[str] = None,
    user_search: Optional[str] = None,
) -> ArchiveReportResponse:
    """为持续治理分析页返回当前渠道归档治理统计。"""
    _ensure_report_permission(request)
    source_id = _get_report_source_id(request)
    safe_agent_id = _optional_target_agent_id(target_agent_id)
    if target_user_id:
        await _ensure_target_user_in_current_source(request, target_user_id)
    tenants = _filter_source_tenants(
        await _load_source_tenants(source_id),
        bbk_id=bbk_id,
        user_search=user_search,
    )
    if target_user_id:
        tenant_ids = {str(row.get("tenant_id") or "") for row in tenants}
        if target_user_id not in tenant_ids:
            tenants = []
            target_user_id = None
    service = _get_continuous_governance_service(request)
    report = await service.build_archive_report(
        source_id=source_id,
        tenants=tenants,
        target_user_id=target_user_id,
        target_agent_id=safe_agent_id,
    )
    return ArchiveReportResponse(
        summary=ArchiveReportSummary(**report.summary.model_dump()),
        health=[
            ReconcileHealthInfo(**item.model_dump()) for item in report.health
        ],
    )
