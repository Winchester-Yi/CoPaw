# -*- coding: utf-8 -*-
"""市场文件系统工具.

市场目录结构：
  <marketplace_root>/<source_id>/index.json
  <marketplace_root>/<source_id>/skills/<item_id>/skill.json
  <marketplace_root>/<source_id>/skills/<item_id>/SKILL.md

用户技能目录：
  <swe_root>/<scope_id>/workspaces/<agent_id>/skills/<skill_name>/
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX fallback
    msvcrt = None

from .models import MarketItem
from ..runtime.context import (
    encode_scope_id,
    migrate_legacy_scope_dir_if_needed,
)

logger = logging.getLogger(__name__)

DEFAULT_AGENT_ID = "default"


class WorkspaceSkillManifestError(ValueError):
    """A shared Workspace skill manifest cannot safely be mutated."""


@dataclass(frozen=True)
class ResolvedRegisteredSkill:
    """A registered package location and whether a collision enables it."""

    path: Path | None
    promoted: bool = False


# === MCP 配置归一化函数 ===


def _normalize_transport_value(raw_transport: str) -> str | None:
    """将 transport 字符串标准化为标准值."""
    lowered = raw_transport.strip().lower()
    if lowered == "streamable-http":
        return "streamable_http"
    if lowered in {"stdio", "sse", "streamable_http"}:
        return lowered
    return None


def _extract_first_mcp_server(config_data: dict) -> dict:
    """从嵌套的 mcpServers 结构中提取第一个 MCP server 配置.

    使用深拷贝确保嵌套字典（headers、env 等）不会共享引用，
    避免重复分发时配置污染问题。
    """
    mcp_servers = config_data.get("mcpServers")
    if not isinstance(mcp_servers, dict) or not mcp_servers:
        # 深拷贝确保嵌套字典独立
        return copy.deepcopy(config_data)

    _, first_value = next(iter(mcp_servers.items()))
    if isinstance(first_value, dict):
        # 深拷贝第一个 server 配置
        return copy.deepcopy(first_value)
    return copy.deepcopy(config_data)


def _apply_advanced_fields(normalized: dict) -> None:
    """将嵌套的 'advanced' 字段提升到顶层配置.

    处理逻辑：
    - headers: 仅当顶层不存在时，从 advanced.headers 提升（避免覆盖用户配置）
    - transport: 仅当顶层不存在时，从 advanced.transport 提升
    - 其他 advanced 内的字段忽略（已通过深拷贝保留在 normalized 中）
    """
    advanced = normalized.get("advanced")
    if not isinstance(advanced, dict):
        return

    # headers 提升：顶层 headers 优先，避免覆盖用户显式配置
    if "headers" not in normalized:
        advanced_headers = advanced.get("headers")
        if isinstance(advanced_headers, dict):
            # 深拷贝避免引用共享
            normalized["headers"] = copy.deepcopy(advanced_headers)

    # transport 提升：顶层 transport 优先
    if "transport" not in normalized:
        advanced_transport = advanced.get("transport")
        if isinstance(advanced_transport, str):
            transport = _normalize_transport_value(advanced_transport)
            if transport:
                normalized["transport"] = transport


def _infer_transport_from_config(normalized: dict) -> None:
    """从 command 或 url 字段推断 transport 类型."""
    if "transport" in normalized:
        return

    command = normalized.get("command")
    if isinstance(command, str) and command.strip():
        normalized["transport"] = "stdio"
        return

    url = normalized.get("url")
    if isinstance(url, str) and url.strip():
        normalized["transport"] = "streamable_http"


def normalize_mcp_config_data(config_data: dict) -> dict:
    """兼容旧市场条目中的原始 MCP 上传结构.

    历史上部分市场条目直接把上传 JSON 原样保存到了 config 中，
    例如 {"mcpServers": {...}}。分发时需要先把这类旧结构归一化
    成 MCPClientConfig 可识别的扁平字段。

    主要处理：
    - 将 mcpServers 嵌套结构提取到顶层（使用深拷贝避免引用共享）
    - 将 advanced.headers 提升到顶层 headers（深拷贝）
    - 将 advanced.transport 提升到顶层 transport
    - 统一 transport 字段的命名（type -> transport, streamable-http -> streamable_http）

    重要：返回的字典是深拷贝结果，嵌套字典（headers、env 等）与原配置无引用关系，
    确保重复分发时各租户配置独立，不会相互污染。
    """
    if not isinstance(config_data, dict):
        return {}

    # 深拷贝提取配置，避免嵌套字典引用共享
    normalized = _extract_first_mcp_server(config_data)
    _apply_advanced_fields(normalized)

    # Normalize top-level transport/type field
    raw_transport = normalized.get("transport") or normalized.get("type")
    if isinstance(raw_transport, str):
        transport = _normalize_transport_value(raw_transport)
        if transport:
            normalized["transport"] = transport

    _infer_transport_from_config(normalized)
    return normalized


# 系统标识符 (source_id, item_id, user_id, agent_id)：仅允许 ASCII 安全字符
_SAFE_SYSTEM_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

# 技能目录名危险字符：控制字符、空格、Windows 保留字符、路径分隔符
# 空格也替换为下划线，避免脚本/工具兼容问题
_UNSAFE_SKILL_NAME_CHARS_RE = re.compile(r'[\x00-\x1f <>:"|?*\\/]')


def normalize_skill_name(name: str) -> str:
    """将技能名称规范化为安全的目录名，保留中文等 Unicode 字符.

    与 SWE 服务的 _normalize_skill_dir_name() 行为对齐，仅过滤真正危险的
    文件系统字符，保留中文、日文、韩文等 Unicode 字符。

    处理流程：
    1. 去除前后空格
    2. 检查空值、NUL 字节、路径遍历
    3. 替换危险字符（控制字符、空格、Windows 保留字符、路径分隔符）为下划线
    4. 合并连续下划线
    5. 去除首尾下划线
    6. 截断到 64 个字符

    Args:
        name: 原始技能名称，如 "数据分析" 或 "Word / DOCX"

    Returns:
        规范的目录名，如 "数据分析" 或 "Word_DOCX"

    Raises:
        ValueError: 名称为空或仅包含非法字符
    """
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("Skill name cannot be empty")
    if "\x00" in normalized:
        raise ValueError("Skill name cannot contain NUL bytes")
    if normalized in {".", ".."}:
        raise ValueError(f"Invalid skill name: {normalized!r}")
    # 替换危险字符为下划线（保留对含 / 的 frontmatter 名称的兼容）
    normalized = _UNSAFE_SKILL_NAME_CHARS_RE.sub("_", normalized)
    # 合并连续下划线
    normalized = re.sub(r"_+", "_", normalized)
    # 去除首尾下划线
    normalized = normalized.strip("_")
    # 截断到 64 个字符
    if len(normalized) > 64:
        normalized = normalized[:64].strip("_")
    if not normalized:
        raise ValueError("Skill name contains only invalid characters")
    return normalized


def _validate_path_segment(value: str, name: str = "segment") -> None:
    """校验系统标识符（source_id, item_id, user_id, agent_id）仅包含 ASCII 安全字符."""
    if value in {".", ".."} or not _SAFE_SYSTEM_SEGMENT_RE.match(value):
        raise ValueError(
            f"Invalid {name} {value!r}: only alphanumerics, underscores, hyphens, and dots are allowed",
        )


def _validate_skill_name_segment(value: str) -> None:
    """校验技能目录名，允许 Unicode 字符但拦截危险文件系统字符."""
    if not value:
        raise ValueError("Skill name cannot be empty")
    if "\x00" in value:
        raise ValueError("Skill name cannot contain NUL bytes")
    if value in {".", ".."}:
        raise ValueError(f"Invalid skill name: {value!r}")
    if _UNSAFE_SKILL_NAME_CHARS_RE.search(value):
        raise ValueError(
            f"Invalid skill name {value!r}: contains unsafe filesystem characters",
        )


def _has_existing_creator_id(entry: dict) -> bool:
    """判断 manifest 条目是否带有有效的 creator_id。"""
    metadata = entry.get("metadata")
    creator_id = None
    if isinstance(metadata, dict):
        creator_id = metadata.get("creator_id")
    if not creator_id:
        creator_id = entry.get("creator_id")
    if isinstance(creator_id, str):
        return bool(creator_id.strip())
    return creator_id is not None


def get_marketplace_dir(marketplace_root: Path, source_id: str) -> Path:
    _validate_path_segment(source_id, "source_id")
    return marketplace_root / source_id


def get_index_path(marketplace_root: Path, source_id: str) -> Path:
    _validate_path_segment(source_id, "source_id")
    return get_marketplace_dir(marketplace_root, source_id) / "index.json"


def get_skill_dir(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
) -> Path:
    _validate_path_segment(source_id, "source_id")
    _validate_path_segment(item_id, "item_id")
    return (
        get_marketplace_dir(marketplace_root, source_id) / "skills" / item_id
    )


def get_expert_dir(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
) -> Path:
    _validate_path_segment(source_id, "source_id")
    _validate_path_segment(item_id, "item_id")
    return (
        get_marketplace_dir(marketplace_root, source_id) / "experts" / item_id
    )


def get_expert_versions_root(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
) -> Path:
    return get_expert_dir(marketplace_root, source_id, item_id) / "versions"


def get_expert_version_dir(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
    version_id: str,
) -> Path:
    _validate_path_segment(version_id, "version_id")
    return (
        get_expert_versions_root(marketplace_root, source_id, item_id)
        / version_id
    )


def get_expert_versions_manifest_path(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
) -> Path:
    return (
        get_expert_dir(marketplace_root, source_id, item_id) / "versions.json"
    )


def get_expert_definition_path(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
) -> Path:
    return (
        get_expert_dir(marketplace_root, source_id, item_id)
        / "definition.toml"
    )


def get_expert_scan_result_path(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
) -> Path:
    return (
        get_expert_dir(marketplace_root, source_id, item_id)
        / "scan_result.json"
    )


def get_user_expert_dir(
    swe_root: Path,
    user_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    source_id: str | None = None,
) -> Path:
    """返回一个 Agent Profile 的本地专家目录。"""
    effective_user_id = resolve_effective_user_id(user_id, source_id)
    _validate_path_segment(effective_user_id, "user_id")
    _validate_path_segment(agent_id, "agent_id")
    user_root = migrate_legacy_scope_dir_if_needed(swe_root, effective_user_id)
    return user_root / "workspaces" / agent_id / "agents"


def resolve_effective_user_id(
    user_id: str,
    source_id: str | None = None,
) -> str:
    """解析用户本地状态使用的有效目录标识。"""
    if not source_id:
        return user_id
    if user_id == "default":
        return f"default_{source_id}"
    return encode_scope_id(user_id, source_id)


def get_user_skills_dir(
    swe_root: Path,
    user_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    source_id: str | None = None,
) -> Path:
    effective_user_id = resolve_effective_user_id(user_id, source_id)
    _validate_path_segment(effective_user_id, "user_id")
    _validate_path_segment(agent_id, "agent_id")
    user_root = migrate_legacy_scope_dir_if_needed(swe_root, effective_user_id)
    return user_root / "workspaces" / agent_id / "skills"


def load_index(marketplace_root: Path, source_id: str) -> list[MarketItem]:
    """读取市场索引，不存在时返回空列表."""
    path = get_index_path(marketplace_root, source_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [MarketItem(**item) for item in data.get("items", [])]
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        logger.error("Failed to load index %s: %s", path, e)
        return []


def save_index(
    marketplace_root: Path,
    source_id: str,
    items: list[MarketItem],
) -> None:
    """原子写入市场索引."""
    path = get_index_path(marketplace_root, source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # MCP 条目不写入 skill_id 和 include_in_statistics 字段（仅对 skill 类型生效）
    data = {
        "items": [
            item.model_dump(
                exclude=(
                    {"skill_id", "include_in_statistics"}
                    if item.item_type != "skill"
                    else set()
                ),
            )
            for item in items
        ],
    }
    _atomic_write_json(path, data)


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写入 JSON 文件，防止并发损坏."""
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def copy_skill_to_user(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
    swe_root: Path,
    user_id: str,
    skill_name: str,
    original_name: str,
    description: str,
    distributed_by: str,
    version: str,
    agent_id: str = DEFAULT_AGENT_ID,
    skill_id: str = "",
    cn_name: str = "",
    category_id: int | None = None,
) -> dict:
    """将市场技能复制到用户工作目录，返回分发元数据供 manifest 使用.

    分发前检查目标目录是否已有同名技能：
    - 自建技能（source=customized）：跳过，保护用户自建内容
    - 接收的技能（source=marketplace:...）：覆盖更新
    - 不存在：正常分发

    注意：不再写入技能目录内的 skill.json，分发元数据由调用方写入 workspace manifest。

    Args:
        skill_name: 规范的目录名（normalize 后，保留中文等 Unicode 字符）
        original_name: 原始技能名称（用于前端展示）
        description: 技能描述（用于前端展示）
        distributed_by: 分发者标识
        version: 技能版本
        skill_id: 技能唯一标识符（跨租户共享）
        cn_name: 中文展示名
        category_id: 市场分类 ID

    Returns:
        {"status": "distributed", "metadata": {...}} 或 {"status": "conflict", "reason": "customized"}
    """
    import shutil

    _validate_skill_name_segment(skill_name)
    src_dir = get_skill_dir(marketplace_root, source_id, item_id)

    # Validate the shared manifest before choosing or replacing a package.
    manifest_path = get_user_skill_manifest_path(
        swe_root,
        user_id,
        agent_id,
        source_id,
    )
    manifest_data = _read_user_skill_manifest_for_mutation(manifest_path)
    existing_entry = manifest_data["skills"].get(skill_name)
    promoted = False
    active_dir = (
        get_user_skills_dir(
            swe_root,
            user_id,
            agent_id,
            source_id,
        )
        / skill_name
    )
    hidden_dir = (
        get_user_disabled_skills_dir(
            swe_root,
            user_id,
            agent_id,
            source_id,
        )
        / skill_name
    )
    if existing_entry is not None:
        resolved = resolve_registered_skill_path(
            manifest_path.parent,
            skill_name,
            existing_entry,
        )
        promoted = resolved.promoted
        final_enabled = existing_entry["enabled"] or promoted
    else:
        final_enabled = True
    dst_dir = active_dir if final_enabled else hidden_dir
    stale_dir = hidden_dir if final_enabled else active_dir

    # 检查目标目录是否已有同名技能（通过 workspace manifest 判断）
    existing_created_at = None
    if existing_entry is not None:
        existing_source = existing_entry.get("source", "customized")
        existing_created_at = existing_entry.get("created_at")

        # 仅当自建技能明确绑定 creator_id 时才保护，兼容历史脏数据覆盖
        if existing_source == "customized" and _has_existing_creator_id(
            existing_entry,
        ):
            return {"status": "conflict", "reason": "customized"}

    # 先删除旧目录，再整体复制（确保与市场源一致）
    if stale_dir.exists():
        shutil.rmtree(stale_dir)
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir)

    # 删除复制过来的 skill.json（不再需要）
    dst_skill_json = dst_dir / "skill.json"
    if dst_skill_json.exists():
        try:
            dst_skill_json.unlink()
        except OSError:
            pass

    # 构建分发元数据，供调用方写入 workspace manifest
    metadata = {
        "name": original_name,
        "description": description,
        "distributed_by": distributed_by,
        "received_version": version,
    }

    # 添加 skill_id 和 cn_name
    if skill_id:
        metadata["skill_id"] = skill_id
    if cn_name:
        metadata["cn_name"] = cn_name
    if category_id is not None:
        metadata["category_id"] = category_id

    # 保留原有 created_at（重复分发时不覆盖首次创建时间）
    if existing_created_at:
        metadata["created_at"] = existing_created_at

    return {
        "status": "distributed",
        "metadata": metadata,
        "package_path": dst_dir,
        "final_enabled": final_enabled,
        "promoted": promoted,
    }


def get_workspace_skill_manifest_path(workspace_dir: Path) -> Path:
    """Return the Workspace v2 skill management manifest path."""
    return Path(workspace_dir) / "skill.json"


def default_workspace_skill_manifest() -> dict:
    """Return a fresh Workspace v2 skill manifest payload."""
    return {
        "schema_version": "workspace-skill-manifest.v1",
        "layout_version": 2,
        "version": 0,
        "skills": {},
    }


def validate_workspace_skill_manifest_v2(payload: object) -> dict:
    """Validate the shared manifest required for Marketplace mutations."""
    if not isinstance(payload, dict) or payload.get("layout_version") != 2:
        raise WorkspaceSkillManifestError(
            "Run skills migrate-layout --apply before mutating skills",
        )
    skills = payload.get("skills")
    if not isinstance(skills, dict):
        raise WorkspaceSkillManifestError(
            "Workspace manifest skills must be an object",
        )
    for skill_name, entry in skills.items():
        if not isinstance(skill_name, str) or not isinstance(entry, dict):
            raise WorkspaceSkillManifestError(
                "Workspace manifest has an invalid skill entry",
            )
        if "enabled" not in entry or not isinstance(entry["enabled"], bool):
            raise WorkspaceSkillManifestError(
                "Workspace manifest skill enabled must be a boolean",
            )
    return payload


def get_user_skill_manifest_path(
    swe_root: Path,
    user_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    source_id: str | None = None,
) -> Path:
    """获取用户 workspace 的运行时 manifest 路径（skill.json）.

    该文件存储技能的运行时状态（enabled、channels、config 等），
    与技能目录内的 skills/<技能名>/skill.json 职责不同。
    路径与 src/swe 的 get_workspace_skill_manifest_path 保持一致。
    """
    effective_user_id = resolve_effective_user_id(user_id, source_id)
    _validate_path_segment(effective_user_id, "user_id")
    _validate_path_segment(agent_id, "agent_id")
    user_root = migrate_legacy_scope_dir_if_needed(swe_root, effective_user_id)
    workspace_dir = user_root / "workspaces" / agent_id
    return get_workspace_skill_manifest_path(workspace_dir)


def get_user_disabled_skills_dir(
    swe_root: Path,
    user_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    source_id: str | None = None,
) -> Path:
    """Return the registered disabled-skill root beside active skills."""
    return (
        get_user_skill_manifest_path(
            swe_root,
            user_id,
            agent_id,
            source_id,
        ).parent
        / ".disabled_skills"
    )


def resolve_registered_skill_path(
    workspace_dir: Path,
    skill_name: str,
    entry: dict,
) -> ResolvedRegisteredSkill:
    """Resolve a registered package without changing files or manifest."""
    _validate_skill_name_segment(skill_name)
    enabled = entry["enabled"]
    active = workspace_dir / "skills" / skill_name
    disabled = workspace_dir / ".disabled_skills" / skill_name
    if active.exists() and disabled.exists():
        return ResolvedRegisteredSkill(active, promoted=not enabled)
    target, fallback = (active, disabled) if enabled else (disabled, active)
    if target.exists():
        return ResolvedRegisteredSkill(target)
    if fallback.exists():
        return ResolvedRegisteredSkill(fallback)
    return ResolvedRegisteredSkill(None)


def read_user_skill_manifest(
    swe_root: Path,
    user_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
    source_id: str | None = None,
) -> dict:
    """读取用户技能 manifest，不存在时返回默认结构."""
    manifest_path = get_user_skill_manifest_path(
        swe_root,
        user_id,
        agent_id,
        source_id,
    )
    if not manifest_path.exists():
        return default_workspace_skill_manifest()
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read manifest %s: %s", manifest_path, e)
        return default_workspace_skill_manifest()


def _read_user_skill_manifest_for_mutation(manifest_path: Path) -> dict:
    """Strictly read a shared manifest before mutating it.

    Only a missing manifest starts from the Workspace v2 default. Existing
    manifests must be readable UTF-8 containing valid JSON; failures propagate
    so callers cannot overwrite data they failed to read.
    """
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_workspace_skill_manifest()
    except UnicodeDecodeError as exc:
        raise WorkspaceSkillManifestError(
            "Run skills migrate-layout --apply before mutating skills",
        ) from exc
    try:
        payload = json.loads(manifest_text)
    except json.JSONDecodeError as exc:
        raise WorkspaceSkillManifestError(
            "Run skills migrate-layout --apply before mutating skills",
        ) from exc
    return validate_workspace_skill_manifest_v2(payload)


def check_skill_status_in_manifest(
    swe_root: Path,
    user_id: str,
    skill_name: str,
    source_id: str,
    agent_id: str = DEFAULT_AGENT_ID,
) -> tuple[str, str | None]:
    """检查用户工作区 manifest 文件判断技能状态.

    Args:
        swe_root: SWE 根目录
        user_id: 用户 ID
        skill_name: 技能名（规范化后的目录名）
        source_id: 来源 ID
        agent_id: Agent ID

    Returns:
        元组 (状态, 版本号)：状态为 first_time / update / conflict，版本号仅在 update 时返回
    """
    manifest = read_user_skill_manifest(swe_root, user_id, agent_id, source_id)
    skills_dict = manifest.get("skills", {})
    existing_entry = skills_dict.get(skill_name)

    if existing_entry is None:
        # manifest 中没有该技能，首次分发
        return ("first_time", None)

    existing_source = existing_entry.get("source", "")

    # 冲突判断逻辑与 copy_skill_to_user 保持一致：
    # 仅当自建技能明确绑定 creator_id 时才保护，兼容历史脏数据覆盖
    if existing_source == "customized" and _has_existing_creator_id(
        existing_entry,
    ):
        return ("conflict", None)
    elif existing_source.startswith("marketplace:"):
        # 已分发技能，返回状态和版本号
        metadata = existing_entry.get("metadata", {})
        version = metadata.get("version_text")
        return ("update", version)
    else:
        # 其他来源（包括无 creator_id 的 customized），视为可覆盖
        return ("first_time", None)


def mutate_user_skill_manifest(
    swe_root: Path,
    user_id: str,
    agent_id: str,
    mutation_fn,
    source_id: str | None = None,
    rollback_fn: Callable[[], None] | None = None,
) -> bool:
    """原子修改用户技能 manifest.

    Args:
        mutation_fn: 接受 dict 参数，返回 bool 表示是否修改成功
    """
    manifest_path = get_user_skill_manifest_path(
        swe_root,
        user_id,
        agent_id,
        source_id,
    )
    with _workspace_manifest_write_lock(manifest_path):
        try:
            current = _read_user_skill_manifest_for_mutation(manifest_path)
            if not mutation_fn(current):
                return False

            _atomic_write_json(manifest_path, current)
            return True
        except Exception:
            if rollback_fn is not None:
                try:
                    rollback_fn()
                except OSError:
                    logger.exception(
                        "Failed to roll back workspace skill manifest mutation",
                    )
            raise


@contextmanager
def _workspace_manifest_write_lock(manifest_path: Path) -> Iterator[None]:
    """Serialize Marketplace mutations with src workspace reconciliation."""
    lock_path = manifest_path.with_name(f".{manifest_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _mask_env_value(value: Optional[str]) -> Optional[str]:
    """脱敏环境变量值。

    Args:
        value: 原始值。

    Returns:
        脱敏后的值，短值全部遮盖，长值显示前2-3字符和后4字符。
    """
    if value is None or value == "":
        return value
    length = len(value)
    if length <= 8:
        return "*" * length
    # 如果第3位是 "-"，前缀取3字符（如 "sk-"），否则取2字符
    prefix_len = 3 if length > 2 and value[2] == "-" else 2
    prefix = value[:prefix_len]
    suffix = value[-4:]
    masked_len = max(length - prefix_len - 4, 4)
    return f"{prefix}{'*' * masked_len}{suffix}"


def get_mcp_dir(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
) -> Path:
    """获取 MCP 条目目录路径。

    Args:
        marketplace_root: 市场根目录。
        source_id: 来源 ID。
        item_id: 条目 ID。

    Returns:
        MCP 条目目录路径。
    """
    _validate_path_segment(source_id, "source_id")
    _validate_path_segment(item_id, "item_id")
    return marketplace_root / source_id / "mcp" / item_id


def get_mcp_config_path(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
) -> Path:
    """获取 MCP 配置文件路径。

    Args:
        marketplace_root: 市场根目录。
        source_id: 来源 ID。
        item_id: 条目 ID。

    Returns:
        MCP 配置文件路径 (mcp.json)。
    """
    return get_mcp_dir(marketplace_root, source_id, item_id) / "mcp.json"


def load_mcp_config(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
) -> Optional[dict]:
    """读取 MCP 配置文件。

    Args:
        marketplace_root: 市场根目录。
        source_id: 来源 ID。
        item_id: 条目 ID。

    Returns:
        MCP 配置字典，不存在或解析失败返回 None。
    """
    path = get_mcp_config_path(marketplace_root, source_id, item_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load MCP config %s: %s", path, e)
        return None


def save_mcp_config(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
    config: dict,
) -> None:
    """保存 MCP 配置文件。

    Args:
        marketplace_root: 市场根目录。
        source_id: 来源 ID。
        item_id: 条目 ID。
        config: MCP 配置字典。
    """
    mcp_dir = get_mcp_dir(marketplace_root, source_id, item_id)
    mcp_dir.mkdir(parents=True, exist_ok=True)
    path = mcp_dir / "mcp.json"
    _atomic_write_json(path, config)


def _normalize_mcp_client_key(name: str) -> str:
    """将 MCP 名称归一化为安全的 client_key，与前端 buildClientKey 对齐。

    市场名称唯一，因此 name 派生的 key 天然不会在市场 MCP 之间冲突。
    """
    import re as _re

    if not name or not name.strip():
        return "mcp"
    # 小写 + 空格/特殊字符替换为连字符
    normalized = _re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
    normalized = _re.sub(r"-+", "-", normalized).strip("-_")
    return normalized or "mcp"


def _load_user_agent_config(user_config_path: Path) -> dict:
    """加载用户 agent 配置，确保 mcp.clients 结构存在。"""
    user_config: dict = {}
    if user_config_path.exists():
        try:
            user_config = json.loads(
                user_config_path.read_text(encoding="utf-8"),
            )
        except (json.JSONDecodeError, OSError):
            pass
    if "mcp" not in user_config:
        user_config["mcp"] = {"clients": {}}
    if "clients" not in user_config["mcp"]:
        user_config["mcp"]["clients"] = {}
    return user_config


def _remove_existing_same_name_mcp(
    clients: dict,
    mcp_name: str,
    current_source: str,
) -> str | None:
    """移除已有的同名市场 MCP 条目，返回被保留的 created_at（如有）。"""
    if not mcp_name:
        return None
    replaced_created_at: str | None = None
    for existing_key, existing_cfg in list(clients.items()):
        if not isinstance(existing_cfg, dict):
            continue
        if existing_cfg.get("name") != mcp_name:
            continue
        existing_source = existing_cfg.get("source", "")
        if (
            isinstance(existing_source, str)
            and existing_source.startswith("marketplace:")
            and existing_source != current_source
        ):
            if existing_cfg.get("created_at"):
                replaced_created_at = existing_cfg["created_at"]
            del clients[existing_key]
        elif existing_source == current_source:
            if existing_cfg.get("created_at"):
                replaced_created_at = existing_cfg["created_at"]
            del clients[existing_key]
    return replaced_created_at


def _resolve_effective_client_key(
    clients: dict,
    client_key: str,
    mcp_name: str,
) -> str:
    """确定最终写入的 dict key，处理 client_key 碰撞。"""
    effective_client_key = client_key
    existing_at_key = clients.get(effective_client_key)
    if existing_at_key and isinstance(existing_at_key, dict):
        name_key = _normalize_mcp_client_key(mcp_name)
        effective_client_key = name_key
        _suffix = 1
        _base = effective_client_key
        while effective_client_key in clients:
            effective_client_key = f"{_base}-{_suffix}"
            _suffix += 1
    return effective_client_key


def _enrich_config_data(
    config_data: dict,
    *,
    item_id: str,
    client_key: str,
    distributed_by: str,
    replaced_created_at: str | None,
    version: str,
    creator_id: str,
    creator_name: str,
    mcp_name: str,
) -> None:
    """向 config_data 写入市场来源、版本号、创建者等元信息。"""
    config_data["source"] = f"marketplace:{item_id}"
    config_data["market_client_key"] = client_key
    config_data["distributed_by"] = distributed_by
    config_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    config_data["created_at"] = (
        replaced_created_at
        if replaced_created_at
        else datetime.now(timezone.utc).isoformat()
    )
    if version:
        config_data["received_version"] = version
        config_data["version"] = version
    if creator_id is not None:
        config_data["creator_id"] = creator_id
    if creator_name is not None:
        config_data["creator_name"] = creator_name
    if mcp_name and not config_data.get("name"):
        config_data["name"] = mcp_name


def copy_mcp_to_user(
    marketplace_root: Path,
    source_id: str,
    item_id: str,
    swe_root: Path,
    user_id: str,
    client_key: str,
    distributed_by: str,
    version: str = "",
    agent_id: str = DEFAULT_AGENT_ID,
    creator_id: str = "",
    creator_name: str = "",
    mcp_name: str = "",
) -> str:
    """将市场 MCP 复制到用户本地配置。

    写入逻辑基于 MCP 名称（市场内唯一）而非 client_key（不可靠）：
    1) 同名替换：已有同名市场 MCP 时先移除旧条目再写入，保证不重复累积。
    2) dict key：优先使用市场原始 client_key；若被不同名称的条目占用，
       则用名称派生的 key，因市场 name 唯一，天然无碰撞。
    3) 用户自建 MCP：同名由上游 _find_user_mcp_name_conflict 拦截，
       不同名但 client_key 碰撞时用名称派生 key 保护。

    Args:
        marketplace_root: 市场根目录。
        source_id: 来源 ID。
        item_id: 条目 ID。
        swe_root: SWE 用户根目录。
        user_id: 用户 ID。
        client_key: MCP 客户端标识（市场来源，非全局唯一）。
        distributed_by: 分发者标识。
        version: 市场条目版本号，写入 received_version。
        agent_id: Agent ID，默认为 "default"。
        creator_id: 市场条目的创建者 ID（写入用户配置以便后续同名检测）。
        creator_name: 市场条目的创建者名称。
        mcp_name: 市场条目的 name，写入用户配置作为稳定标识。

    Returns:
        实际写入用户配置的 client_key。
    """
    mcp_config = load_mcp_config(marketplace_root, source_id, item_id)
    if mcp_config is None:
        raise ValueError(f"MCP config not found for item {item_id}")

    # 解析运行时 scope 目录，并处理 legacy 目录迁移
    effective_user_id = resolve_effective_user_id(user_id, source_id)
    user_root = migrate_legacy_scope_dir_if_needed(swe_root, effective_user_id)
    user_config_path = user_root / "workspaces" / agent_id / "agent.json"
    user_config_path.parent.mkdir(parents=True, exist_ok=True)

    user_config = _load_user_agent_config(user_config_path)

    # 合并 MCP 配置
    config_data = mcp_config.get("config", {})
    # 归一化配置数据，将 advanced.headers 等字段提升到顶层
    config_data = normalize_mcp_config_data(config_data)

    # ---- 基于名称的替换与 key 决策 ----
    current_source = f"marketplace:{item_id}"
    clients = user_config["mcp"]["clients"]
    replaced_created_at = _remove_existing_same_name_mcp(
        clients,
        mcp_name,
        current_source,
    )

    effective_client_key = _resolve_effective_client_key(
        clients,
        client_key,
        mcp_name,
    )

    _enrich_config_data(
        config_data,
        item_id=item_id,
        client_key=client_key,
        distributed_by=distributed_by,
        replaced_created_at=replaced_created_at,
        version=version,
        creator_id=creator_id,
        creator_name=creator_name,
        mcp_name=mcp_name,
    )

    clients[effective_client_key] = config_data

    _atomic_write_json(user_config_path, user_config)
    return effective_client_key
