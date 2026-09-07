# -*- coding: utf-8 -*-
"""Skill invocation detector for detecting skill boundaries.

This module provides the SkillInvocationDetector which detects when tool
calls belong to skill execution flows, manages skill execution boundaries,
and resolves multi-skill attribution conflicts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from inspect import isawaitable
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TYPE_CHECKING

from .skill_context_manager import (
    SkillExecutionContext,
    SkillContextManager,
    get_skill_context_manager,
)
from .skill_feature_inferencer import (
    SkillFeatureInferencer,
    get_skill_feature_inferencer,
)
from .skill_runtime_profile import SkillRuntimeProfile
from .skill_tool_registry import SkillToolRegistry, get_skill_tool_registry
from .skills_manager import get_workspace_skill_manifest_path

if TYPE_CHECKING:
    from ..tracing.manager import TraceManager

logger = logging.getLogger(__name__)


# 技能描述缓存
_SKILL_DESCRIPTION_CACHE: dict[str, str] = {}
_GENERIC_CONTINUATION_TOOLS = {
    "execute_shell_command",
    "read_file",
    "write_file",
    "grep_search",
    "glob_search",
    "unknown_tool",
}


def _get_skill_description(skill_name: str) -> str:
    """从技能目录读取技能描述（fallback 方式）.

    从内置技能目录的 SKILL.md 文件中读取 description 字段。
    主要用于内置技能的描述获取。

    Args:
        skill_name: 技能名称

    Returns:
        技能描述字符串，如果未找到则返回空字符串
    """
    # 检查缓存
    if skill_name in _SKILL_DESCRIPTION_CACHE:
        return _SKILL_DESCRIPTION_CACHE[skill_name]

    description = ""

    # 尝试从内置技能目录读取
    try:
        from .skills_manager import get_builtin_skills_dir

        builtin_dir = get_builtin_skills_dir()
        skill_md_path = builtin_dir / skill_name / "SKILL.md"
        if skill_md_path.exists():
            description = _parse_skill_description(skill_md_path)
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"Failed to read builtin skill description: {e}")

    # 缓存结果
    _SKILL_DESCRIPTION_CACHE[skill_name] = description
    return description


def _parse_skill_description(skill_md_path: Path) -> str:
    """解析 SKILL.md 文件获取描述字段.

    Args:
        skill_md_path: SKILL.md 文件路径

    Returns:
        技能描述字符串
    """
    try:
        content = skill_md_path.read_text(encoding="utf-8")
        return _extract_description_from_frontmatter(content)
    except Exception as e:
        logger.debug(
            f"Failed to parse skill description from {skill_md_path}: {e}",
        )
    return ""


def _extract_description_from_frontmatter(content: str) -> str:
    """从 YAML frontmatter 提取 description 字段.

    Args:
        content: 文件内容

    Returns:
        技能描述字符串，未找到则返回空字符串
    """
    if not content.startswith("---"):
        return ""

    end_idx = content.find("---", 3)
    if end_idx == -1:
        return ""

    frontmatter = content[3:end_idx].strip()
    for line in frontmatter.split("\n"):
        if line.startswith("description:"):
            desc = line.split(":", 1)[1].strip()
            return _strip_quotes(desc)
    return ""


def _strip_quotes(text: str) -> str:
    """移除字符串两端可能的引号.

    Args:
        text: 原始字符串

    Returns:
        移除引号后的字符串
    """
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    return text


class SkillInvocationDetector:
    """Skill invocation detector.

    Responsible for:
    1. Detecting tool calls that belong to skill execution flows
    2. Managing skill execution start/end boundaries
    3. Resolving multi-skill attribution conflicts
    4. Tracking skill state and activity

    The detector uses multiple layers for attribution:
    - Layer 1: Explicit declaration (uses_tools in SKILL.md)
    - Layer 2: Feature matching (file extensions, keywords)
    - Layer 3: Tool sequence patterns
    - Layer 4: Skill-tool association hints

    Example:
        detector = SkillInvocationDetector(
            registry=get_skill_tool_registry(),
            context_manager=get_skill_context_manager(),
        )
        detector.set_enabled_skills(["xlsx", "pdf"])

        # On tool call
        primary_skill, weights = await detector.on_tool_call(
            "execute_shell_command",
            {"command": "python process.py data.xlsx"},
        )
        # Returns: ("xlsx", {"xlsx": 0.8, "pdf": 0.2})
    """

    def __init__(
        self,
        registry: Optional[SkillToolRegistry] = None,
        context_manager: Optional[SkillContextManager] = None,
        inferencer: Optional[SkillFeatureInferencer] = None,
        trace_manager: Optional["TraceManager"] = None,
        trace_id: Optional[str] = None,
        user_id: str = "",
        session_id: str = "",
        channel: str = "",
        source_id: str = "",
        idle_threshold: int = 3,
        user_name: Optional[str] = None,
        bbk_id: Optional[str] = None,
        workspace_dir: Optional[Path] = None,
        skill_dirs: Optional[dict[str, Path]] = None,
        skill_signatures: Optional[dict[str, str]] = None,
        skill_hook_loader: (
            Callable[[str], Awaitable[None] | None] | None
        ) = None,
        confirmed_skill_callback: (
            Callable[[str], Awaitable[None] | None] | None
        ) = None,
    ) -> None:
        """Initialize the detector.

        Args:
            registry: Skill-tool registry for explicit declarations
            context_manager: Skill context manager for execution tracking
            inferencer: Feature inferencer for legacy skill support
            trace_manager: Optional trace manager for emitting events
            trace_id: Current trace ID
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
            source_id: Source identifier for data isolation
            idle_threshold: Number of non-skill tool calls before ending skill
            user_name: Optional user name
            bbk_id: Optional BBK identifier
            workspace_dir: Workspace directory for reading skill manifest
            skill_hook_loader: Optional session-scoped hook loader callback
            confirmed_skill_callback: Optional callback invoked when a skill
                reaches confirmed association and is activated
        """
        self._registry = registry or get_skill_tool_registry()
        self._context_manager = context_manager or get_skill_context_manager()
        self._inferencer = inferencer or get_skill_feature_inferencer()
        self._trace_manager = trace_manager
        self._trace_id = trace_id
        self._user_id = user_id
        self._session_id = session_id
        self._channel = channel
        self._source_id = source_id
        self._user_name = user_name
        self._bbk_id = bbk_id
        self._workspace_dir = workspace_dir
        self._skill_dirs = dict(skill_dirs or {})
        self._skill_signatures = dict(skill_signatures or {})
        self._skill_hook_loader = skill_hook_loader
        self._confirmed_skill_callback = confirmed_skill_callback

        # Configuration
        self._idle_threshold = idle_threshold

        # State tracking
        self._enabled_skills: set[str] = set()
        self._skill_descriptions: dict[str, str] = (
            {}
        )  # skill_name -> description
        self._skill_ids: dict[str, str] = {}  # skill_name -> skill_id
        self._skill_cn_names: dict[str, str] = {}  # skill_name -> cn_name
        self._skill_activation_time: dict[str, datetime] = {}
        self._skill_call_history: dict[str, int] = {}
        self._idle_counters: dict[str, int] = {}
        self._recent_tools: list[str] = []
        self._skill_runtime_profiles: dict[str, SkillRuntimeProfile] = {}

        # Layer 0: User message detection cache
        self._message_detected_skill: Optional[str] = None
        self._message_detected_confidence: float = 0.0
        self._locked_skill_from_md: Optional[str] = None
        self._pending_pruned_contexts: list[SkillExecutionContext] = []
        self._pruned_context_tasks: set[asyncio.Task[None]] = set()
        self._pending_skill_md_continuation: Optional[str] = None

    def set_enabled_skills(
        self,
        skills: list[str],
        metadata_by_name: dict[str, Any] | None = None,
    ) -> None:
        """Set the list of enabled skills and cache their descriptions.

        Reads skill descriptions from the workspace management manifest at
        ``skill.json`` during setup time, so they're ready
        when start_skill is called.

        Args:
            skills: List of skill names that are currently enabled
        """
        self._enabled_skills = set(skills)

        if metadata_by_name is not None:
            self._cache_skill_metadata(skills, metadata_by_name)

        # 技能启用集变化后，立即清理已经失效的 SKILL.md 锁定状态，
        # 避免后续工具调用继续被已禁用技能短路归因。
        if (
            self._locked_skill_from_md
            and self._locked_skill_from_md not in self._enabled_skills
        ):
            self._locked_skill_from_md = None
        if (
            self._pending_skill_md_continuation
            and self._pending_skill_md_continuation not in self._enabled_skills
        ):
            self._pending_skill_md_continuation = None

        # 用户消息识别缓存同样依赖当前启用技能集合；
        # 如果缓存技能已被禁用，必须同步失效，避免兜底层返回陈旧结果。
        if (
            self._message_detected_skill
            and self._message_detected_skill not in self._enabled_skills
        ):
            self._message_detected_skill = None
            self._message_detected_confidence = 0.0

        removed_contexts = self._context_manager.prune_disabled_skills(
            self._enabled_skills,
        )
        if removed_contexts:
            logger.info(
                "Pruned disabled skill contexts after enabled skills update: %s",
                [context.skill_name for context in removed_contexts],
            )
            self._schedule_pruned_context_finalization(removed_contexts)

        # Pre-cache descriptions from workspace manifest
        if metadata_by_name is None and self._workspace_dir:
            manifest_path = get_workspace_skill_manifest_path(
                self._workspace_dir,
            )

            if manifest_path.exists():
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        manifest = json.load(f)

                    for skill_name in skills:
                        skill_entry = manifest.get("skills", {}).get(
                            skill_name,
                            {},
                        )
                        metadata = skill_entry.get("metadata", {})
                        description = metadata.get("description", "") or ""
                        if description:
                            self._skill_descriptions[skill_name] = str(
                                description,
                            )
                            logger.debug(
                                "Cached description for skill '%s'",
                                skill_name,
                            )
                        # Cache skill_id and cn_name
                        skill_id = metadata.get("skill_id", "")
                        cn_name = metadata.get("cn_name", "")
                        if skill_id:
                            self._skill_ids[skill_name] = str(skill_id)
                            logger.debug(
                                "Cached skill_id for '%s': %s",
                                skill_name,
                                skill_id,
                            )
                        else:
                            logger.debug(
                                "No skill_id in metadata for '%s'",
                                skill_name,
                            )
                        if cn_name:
                            self._skill_cn_names[skill_name] = str(cn_name)
                            logger.debug(
                                "Cached cn_name for '%s': %s",
                                skill_name,
                                cn_name,
                            )
                        else:
                            logger.debug(
                                "No cn_name in metadata for '%s'",
                                skill_name,
                            )
                except Exception as e:
                    logger.warning("Failed to read skill manifest: %s", e)

    def _cache_skill_metadata(
        self,
        skills: list[str],
        metadata_by_name: Mapping[str, Any],
    ) -> None:
        """Cache descriptions and stable IDs supplied by a query snapshot."""
        for skill_name in skills:
            metadata = metadata_by_name.get(skill_name) or {}
            if not isinstance(metadata, Mapping):
                continue
            description = metadata.get("description") or ""
            if description:
                self._skill_descriptions[skill_name] = str(description)
            skill_id = metadata.get("skill_id") or ""
            if skill_id:
                self._skill_ids[skill_name] = str(skill_id)
            cn_name = metadata.get("cn_name") or ""
            if cn_name:
                self._skill_cn_names[skill_name] = str(cn_name)

    def set_skill_runtime_profiles(
        self,
        profiles: dict[str, SkillRuntimeProfile],
    ) -> None:
        """设置平台内部的 skill 运行时画像。"""
        self._skill_runtime_profiles = dict(profiles)

    def get_skill_runtime_profile(
        self,
        skill_name: str,
    ) -> Optional[SkillRuntimeProfile]:
        """返回 skill 的运行时画像，只供外部只读消费。"""
        return self._skill_runtime_profiles.get(skill_name)

    def _is_declared_tool_bootstrap_allowed(self, skill_name: str) -> bool:
        """判断 skill 是否允许仅凭 declared tool 自动启动。"""
        profile = self.get_skill_runtime_profile(skill_name)
        if profile is None:
            return True
        return bool(profile.declared_tool_bootstrap_allowed)

    def _can_bootstrap_from_input_evidence(
        self,
        confidence: float,
    ) -> bool:
        """仅允许强输入证据自动启动新技能。"""
        return confidence >= 0.8

    def _can_continue_with_runtime_evidence(
        self,
        skill_name: Optional[str],
    ) -> bool:
        """运行时弱信号只能续接当前已激活技能。"""
        current = self._context_manager.current_skill
        return bool(skill_name and current and current == skill_name)

    def _can_bootstrap_from_message_match(
        self,
        skill_name: Optional[str],
    ) -> bool:
        """消息级命中只能与结构化证据组合后激活技能。"""
        return bool(
            skill_name
            and self._message_detected_skill == skill_name
            and self._message_detected_confidence >= 0.7,
        )

    def _should_apply_pending_continuation(
        self,
        pending_skill: Optional[str],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        """判断 one-shot continuation 是否仍与当前 skill 相关。"""
        current = self._context_manager.current_skill
        if not pending_skill or pending_skill != current:
            return False
        return self._tool_input_targets_skill_assets(
            pending_skill,
            tool_input,
        )

    @staticmethod
    def _is_generic_continuation_tool(tool_name: str) -> bool:
        """判断工具是否属于通用平台工具。"""
        return tool_name in _GENERIC_CONTINUATION_TOOLS

    @classmethod
    def _is_ambiguous_continuation_tool(cls, tool_name: str) -> bool:
        """判断工具是否缺乏 skill 专属性，容易导致误归因。"""
        return cls._is_generic_continuation_tool(
            tool_name,
        ) or tool_name.startswith("mcp_")

    def _is_unconfirmed_restored_current_skill(
        self,
        current_skill: str,
    ) -> bool:
        """判断当前 skill 是否仅来自 session restore 且尚未确认。"""
        context = self._context_manager.current_context
        if context is None or context.skill_name != current_skill:
            return False
        if context.trigger_reason != "session_restore":
            return False
        return not (context.tools_called or context.mcp_tools_called)

    def _should_continue_declared_current_skill(
        self,
        current_skill: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        """当前 skill 命中 declared tools 时，判断是否允许直接续接。"""
        if not self._is_unconfirmed_restored_current_skill(current_skill):
            return True
        if not self._is_ambiguous_continuation_tool(tool_name):
            return True
        return self._tool_input_targets_skill_assets(current_skill, tool_input)

    def _tool_input_targets_skill_assets(
        self,
        skill_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        """检查工具输入是否引用了当前 skill 目录下的资产。"""
        skill_dirs = self._get_skill_asset_dirs(skill_name)
        if not skill_dirs:
            return False

        for value in self._iter_tool_input_strings(tool_input):
            if self._string_targets_skill_assets(value, skill_dirs):
                return True
        return False

    def _detect_skill_from_tool_input_assets(
        self,
        tool_input: dict[str, Any],
    ) -> Optional[str]:
        """Resolve a tool input to one enabled skill's real asset directory."""
        matches = [
            skill_name
            for skill_name in self._enabled_skills
            if self._tool_input_targets_skill_assets(skill_name, tool_input)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _iter_tool_input_strings(self, value: Any) -> list[str]:
        """递归提取工具参数中的所有字符串值。"""
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            dict_strings: list[str] = []
            for nested in value.values():
                dict_strings.extend(self._iter_tool_input_strings(nested))
            return dict_strings
        if isinstance(value, (list, tuple, set)):
            list_strings: list[str] = []
            for nested in value:
                list_strings.extend(self._iter_tool_input_strings(nested))
            return list_strings
        return []

    def _get_skill_asset_dirs(self, skill_name: str) -> list[Path]:
        """返回可能的 skill 目录列表。"""
        candidate_dirs: list[Path] = []

        snapshot_dir = self._skill_dirs.get(skill_name)
        if snapshot_dir is not None:
            candidate_dirs.append(snapshot_dir)

        if self._workspace_dir and snapshot_dir is None:
            try:
                from .skills_manager import get_workspace_skills_dir

                candidate_dirs.append(
                    get_workspace_skills_dir(self._workspace_dir) / skill_name,
                )
            except Exception:
                candidate_dirs.append(
                    self._workspace_dir / "skills" / skill_name,
                )

        try:
            from .skills_manager import get_builtin_skills_dir

            candidate_dirs.append(get_builtin_skills_dir() / skill_name)
        except Exception:
            pass

        deduped: list[Path] = []
        seen: set[str] = set()
        for skill_dir in candidate_dirs:
            key = str(skill_dir)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(skill_dir)
        return deduped

    def _string_targets_skill_assets(
        self,
        value: str,
        skill_dirs: list[Path],
    ) -> bool:
        """检查字符串参数是否指向 skill 目录中的文件。"""
        stripped = value.strip()
        if not stripped:
            return False

        direct_path = self._normalize_candidate_path(stripped)
        if direct_path and self._matches_skill_asset_candidate(
            direct_path,
            skill_dirs,
            workspace_dir=self._workspace_dir,
        ):
            return True

        for token in re.findall(r'(?:"[^"]+"|\'[^\']+\'|\S+)', stripped):
            normalized = self._normalize_candidate_path(token)
            if not normalized:
                continue
            if self._matches_skill_asset_candidate(
                normalized,
                skill_dirs,
                workspace_dir=self._workspace_dir,
            ):
                return True
        return False

    @staticmethod
    def _normalize_candidate_path(token: str) -> Optional[str]:
        """清洗字符串中的潜在路径片段。"""
        candidate = token.strip().strip("\"'")
        if not candidate:
            return None
        candidate = candidate.rstrip(",;")
        if candidate.startswith("-"):
            return None
        if not any(marker in candidate for marker in ("/", "\\", ".", ":")):
            return None
        return candidate

    @staticmethod
    def _matches_skill_asset_candidate(
        candidate: str,
        skill_dirs: list[Path],
        *,
        workspace_dir: Optional[Path] = None,
    ) -> bool:
        """判断候选路径是否解析到 skill 目录内的真实资产。"""
        candidate_path = Path(candidate)
        for skill_dir in skill_dirs:
            if not skill_dir.exists():
                continue
            try:
                resolved_skill_dir = skill_dir.resolve(strict=True)
                if candidate_path.is_absolute():
                    resolved_candidate = candidate_path.resolve(strict=True)
                elif workspace_dir is not None:
                    resolved_candidate = (
                        workspace_dir / candidate_path
                    ).resolve(
                        strict=True,
                    )
                else:
                    continue
                resolved_candidate.relative_to(resolved_skill_dir)
            except (OSError, ValueError):
                continue
            return True
        return False

    def detect_from_user_message(
        self,
        user_message: str,
    ) -> tuple[Optional[str], float]:
        """Layer 0: Detect skill from user message content.

        This method should be called at the start of a trace, before any
        tool calls are made. The result is cached for use during tool
        call detection.

        Args:
            user_message: User's message text

        Returns:
            Tuple of (skill_name, confidence) or (None, 0.0)
        """
        enabled_skills = list(self._enabled_skills)

        skill, confidence = self._inferencer.infer_skill_from_user_message(
            user_message,
            enabled_skills,
        )

        if skill:
            self._message_detected_skill = skill
            self._message_detected_confidence = confidence
        else:
            # 清理上一轮缓存，避免同一 detector 被复用时沿用陈旧命中。
            self._message_detected_skill = None
            self._message_detected_confidence = 0.0

        return skill, confidence

    def set_tracing_context(
        self,
        trace_manager: "TraceManager",
        trace_id: str,
        user_id: str,
        session_id: str,
        channel: str,
        source_id: str = "",
    ) -> None:
        """Set tracing context for emitting events.

        Args:
            trace_manager: Trace manager instance
            trace_id: Current trace ID
            user_id: User identifier
            session_id: Session identifier
            channel: Channel identifier
        """
        self._trace_manager = trace_manager
        self._trace_id = trace_id
        self._user_id = user_id
        self._session_id = session_id
        self._channel = channel
        self._source_id = source_id

    async def on_tool_call(
        self,
        tool_name: str,
        tool_input: Optional[dict[str, Any]] = None,
        mcp_server: Optional[str] = None,
    ) -> tuple[Optional[str], dict[str, float]]:
        """Process exact evidence of actual skill use.

        Runtime attribution intentionally accepts only two evidence classes:
        reading an enabled skill's ``SKILL.md`` and a real asset path under one
        enabled skill directory. Feature inference, declared tools, MCP
        servers, tool hints, and recent tool sequences are discovery metadata,
        not runtime activation evidence.

        Args:
            tool_name: Name of the tool being called
            tool_input: Tool input parameters (for inference)
            mcp_server: MCP server name if this is an MCP tool

        Returns:
            Tuple of (primary_skill, weights_dict)
            - primary_skill: The main skill to attribute this call to
            - weights: Dict mapping skill_name -> weight (sum = 1.0)
        """
        tool_input = tool_input or {}

        await self._drain_pending_pruned_contexts()

        # Reading SKILL.md is the only detector-originated hook-load path.
        skill_from_md_read = self._detect_skill_from_skill_md_read(
            tool_name,
            tool_input,
        )
        if skill_from_md_read:
            return await self._activate_and_record_skill(
                skill_from_md_read,
                1.0,
                tool_name,
                mcp_server,
                load_hooks=True,
            )

        skill_from_asset_path = self._detect_skill_from_tool_input_assets(
            tool_input,
        )
        if skill_from_asset_path:
            return await self._activate_and_record_skill(
                skill_from_asset_path,
                1.0,
                tool_name,
                mcp_server,
                load_hooks=False,
            )

        return None, {}

    async def _handle_declared_skills(
        self,
        skills: list[str],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> tuple[Optional[str], dict[str, float]]:
        """Handle tool call with explicit skill declarations.

        Args:
            skills: Skills that declare using this tool
            tool_name: Tool name
            tool_input: Tool input

        Returns:
            Tuple of (primary_skill, weights)
        """
        current = self._context_manager.current_skill
        # Check if current active skill is in the list
        if current and current in skills:
            if not self._should_continue_declared_current_skill(
                current,
                tool_name,
                tool_input,
            ):
                return None, {}
            # Continue current skill
            self._update_skill_state(current)
            self._context_manager.record_tool_call(tool_name)
            return current, {current: 1.0}

        if not current:
            for skill in skills:
                if not self._can_bootstrap_from_message_match(skill):
                    continue
                confidence = self._message_detected_confidence
                await self._ensure_skill_active(
                    skill,
                    confidence,
                    tool_name,
                )
                self._context_manager.record_tool_call(tool_name)
                return skill, {skill: confidence}

        bootstrap_skills = [
            skill
            for skill in skills
            if self._is_declared_tool_bootstrap_allowed(skill)
        ]
        # declared/tool ownership 属于运行时续接证据，不再负责 bootstrap。
        if not bootstrap_skills or not current:
            return None, {}

        # Check if current skill should end (idle threshold)
        if current:
            self._idle_counters[current] = (
                self._idle_counters.get(current, 0) + 1
            )
            if self._idle_counters[current] >= self._idle_threshold:
                await self._end_skill(current)
                current = None

        # Calculate weights for multi-skill attribution
        weights = self._calculate_weights(
            bootstrap_skills,
            tool_name,
            tool_input,
        )

        # Select primary skill (highest weight)
        primary_skill = (
            max(weights, key=lambda k: weights[k]) if weights else None
        )

        return primary_skill, weights

    async def _infer_skill_attribution(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        mcp_server: Optional[str] = None,
        pending_skill_md_continuation: Optional[str] = None,
    ) -> tuple[Optional[str], dict[str, float]]:
        """Infer skill attribution for tools without explicit declarations.

        Uses multiple layers:
        0. Cached user message detection (if available)
        1. MCP server matching
        2. Feature matching (file extensions, keywords)
        3. Tool sequence patterns
        4. Tool-skill hints

        Args:
            tool_name: Tool name
            tool_input: Tool input
            mcp_server: MCP server if applicable

        Returns:
            Tuple of (primary_skill, weights)
        """
        enabled_skills = list(self._enabled_skills)
        for resolver in (
            self._try_infer_from_mcp_server,
            self._try_infer_from_tool_input,
            self._try_infer_from_tool_sequence,
            self._try_infer_from_message_asset_bootstrap,
            self._try_infer_from_tool_hints,
        ):
            result = await resolver(
                enabled_skills,
                tool_name,
                tool_input,
                mcp_server,
            )
            if result is not None:
                return result

        fallback = await self._try_infer_from_pending_or_message_fallback(
            tool_name,
            tool_input,
            mcp_server,
            pending_skill_md_continuation,
        )
        if fallback is not None:
            return fallback

        # No attribution possible
        return None, {}

    async def _activate_and_record_skill(
        self,
        skill_name: str,
        confidence: float,
        tool_name: str,
        mcp_server: Optional[str],
        *,
        weights: Optional[dict[str, float]] = None,
        load_hooks: bool = False,
    ) -> tuple[str, dict[str, float]]:
        """激活技能并记录本次工具调用。"""
        await self._ensure_skill_active(
            skill_name,
            confidence,
            tool_name,
            load_hooks=load_hooks,
        )
        self._context_manager.record_tool_call(tool_name, mcp_server)
        return skill_name, weights or {skill_name: confidence}

    async def _try_infer_from_mcp_server(
        self,
        enabled_skills: list[str],
        tool_name: str,
        _tool_input: dict[str, Any],
        mcp_server: Optional[str],
    ) -> Optional[tuple[str, dict[str, float]]]:
        """尝试使用 MCP server 证据归因。"""
        if not mcp_server:
            return None

        skill, confidence = self._inferencer.infer_skill_from_mcp_server(
            mcp_server,
            enabled_skills,
        )
        if not skill:
            return None
        if not (
            self._can_continue_with_runtime_evidence(skill)
            or self._can_bootstrap_from_message_match(skill)
        ):
            return None

        return await self._activate_and_record_skill(
            skill,
            confidence,
            tool_name,
            mcp_server,
        )

    async def _try_infer_from_tool_input(
        self,
        enabled_skills: list[str],
        tool_name: str,
        tool_input: dict[str, Any],
        mcp_server: Optional[str],
    ) -> Optional[tuple[str, dict[str, float]]]:
        """尝试使用工具输入特征归因。"""
        skill, confidence = self._inferencer.infer_skill_from_tool_input(
            tool_name,
            tool_input,
            enabled_skills,
        )
        if not skill:
            return None
        if not (
            self._can_bootstrap_from_input_evidence(confidence)
            or self._can_continue_with_runtime_evidence(skill)
            or self._can_bootstrap_from_message_match(skill)
        ):
            return None

        return await self._activate_and_record_skill(
            skill,
            confidence,
            tool_name,
            mcp_server,
        )

    async def _try_infer_from_tool_sequence(
        self,
        enabled_skills: list[str],
        tool_name: str,
        _tool_input: dict[str, Any],
        mcp_server: Optional[str],
    ) -> Optional[tuple[str, dict[str, float]]]:
        """尝试使用工具序列特征归因。"""
        skill, confidence = self._inferencer.infer_skill_from_tool_sequence(
            self._recent_tools,
            enabled_skills,
        )
        if not skill or not self._can_continue_with_runtime_evidence(skill):
            return None

        return await self._activate_and_record_skill(
            skill,
            confidence,
            tool_name,
            mcp_server,
        )

    async def _try_infer_from_message_asset_bootstrap(
        self,
        _enabled_skills: list[str],
        tool_name: str,
        tool_input: dict[str, Any],
        mcp_server: Optional[str],
    ) -> Optional[tuple[str, dict[str, float]]]:
        """尝试使用消息命中加 skill 资产证据进行启动。"""
        message_skill = self._message_detected_skill
        current = self._context_manager.current_skill
        if current is not None or message_skill is None:
            return None
        if not self._can_bootstrap_from_message_match(message_skill):
            return None
        if not self._tool_input_targets_skill_assets(
            message_skill,
            tool_input,
        ):
            return None

        return await self._activate_and_record_skill(
            message_skill,
            1.0,
            tool_name,
            mcp_server,
        )

    async def _try_infer_from_tool_hints(
        self,
        enabled_skills: list[str],
        tool_name: str,
        _tool_input: dict[str, Any],
        mcp_server: Optional[str],
    ) -> Optional[tuple[str, dict[str, float]]]:
        """尝试使用 tool hints 归因。"""
        inferred = self._inferencer.get_skills_for_tool(
            tool_name,
            enabled_skills,
        )
        raw_inferred = list(inferred)
        continued = [
            (skill_name, confidence)
            for skill_name, confidence in inferred
            if self._can_continue_with_runtime_evidence(skill_name)
        ]
        if continued:
            primary_skill = continued[0][0]
            weights = dict(continued)
            return await self._activate_and_record_skill(
                primary_skill,
                weights.get(primary_skill, 0.4),
                tool_name,
                mcp_server,
                weights=weights,
            )

        bootstrap = [
            (skill_name, confidence)
            for skill_name, confidence in raw_inferred
            if self._can_bootstrap_from_message_match(skill_name)
        ]
        if not bootstrap:
            return None

        primary_skill = bootstrap[0][0]
        weights = dict(bootstrap)
        return await self._activate_and_record_skill(
            primary_skill,
            weights.get(primary_skill, 0.4),
            tool_name,
            mcp_server,
            weights=weights,
        )

    async def _try_infer_from_pending_or_message_fallback(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        mcp_server: Optional[str],
        pending_skill_md_continuation: Optional[str],
    ) -> Optional[tuple[str, dict[str, float]]]:
        """尝试使用 pending continuation 或消息兜底归因。"""
        current = self._context_manager.current_skill
        if self._should_apply_pending_continuation(
            pending_skill_md_continuation,
            tool_name,
            tool_input,
        ):
            if current is None:
                return None
            # 仅对紧随 SKILL.md 读取后的下一次无证据工具调用延续归因，
            # 避免恢复的 session skill 在无证据场景下直接丢失。
            self._update_skill_state(current)
            self._context_manager.record_tool_call(tool_name, mcp_server)
            self._pending_skill_md_continuation = None
            return current, {current: 1.0}

        if not self._should_apply_message_fallback() or current is None:
            return None

        return await self._activate_and_record_skill(
            current,
            self._message_detected_confidence,
            tool_name,
            mcp_server,
        )

    def _should_apply_message_fallback(self) -> bool:
        """仅在当前 skill 已有确认调用时允许消息级兜底续接。"""
        current = self._context_manager.current_skill
        context = self._context_manager.current_context
        if (
            not current
            or self._message_detected_skill != current
            or self._message_detected_confidence < 0.7
            or context is None
            or context.skill_name != current
        ):
            return False
        return bool(context.tools_called or context.mcp_tools_called)

    async def _ensure_skill_active(
        self,
        skill_name: str,
        confidence: float,
        trigger_tool: str,
        *,
        load_hooks: bool = False,
    ) -> None:
        """Ensure a skill is active, starting it if needed.

        Args:
            skill_name: Skill to ensure active
            confidence: Attribution confidence
            trigger_tool: Tool that triggered this skill
        """
        current = self._context_manager.current_skill

        if current == skill_name:
            # Already active, just update state
            self._update_skill_state(skill_name)
            return

        if current:
            # Different skill active, end it first
            await self._end_skill(current)

        # Start the new skill
        await self.start_skill(
            skill_name,
            trigger_tool=trigger_tool,
            trigger_reason="inferred",
            confidence=confidence,
            load_hooks=load_hooks,
        )

    def _schedule_pruned_context_finalization(
        self,
        contexts: list[SkillExecutionContext],
    ) -> None:
        """为被裁剪的技能上下文安排 tracing 收尾."""
        if not contexts:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._pending_pruned_contexts.extend(contexts)
            return

        task = loop.create_task(self._finalize_pruned_contexts(contexts))
        self._pruned_context_tasks.add(task)
        task.add_done_callback(self._pruned_context_tasks.discard)

    async def _drain_pending_pruned_contexts(self) -> None:
        """在显式 await 边界补齐延迟的上下文收尾."""
        if self._pending_pruned_contexts:
            pending_contexts = self._pending_pruned_contexts
            self._pending_pruned_contexts = []
            await self._finalize_pruned_contexts(pending_contexts)

        if self._pruned_context_tasks:
            await asyncio.gather(
                *list(self._pruned_context_tasks),
                return_exceptions=True,
            )

    async def _finalize_pruned_contexts(
        self,
        contexts: list[SkillExecutionContext],
    ) -> None:
        """结束因启用技能变化而被移除的上下文."""
        for context in contexts:
            await self._emit_skill_end_for_context(context)

    def _flush_pruned_contexts_for_reset(self) -> None:
        """在 reset 前尽量完成被裁剪上下文的 tracing 收尾."""
        pending_contexts = self._pending_pruned_contexts
        self._pending_pruned_contexts = []

        if not pending_contexts:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._finalize_pruned_contexts(pending_contexts))
            return

        task = loop.create_task(
            self._finalize_pruned_contexts(pending_contexts),
        )
        self._pruned_context_tasks.add(task)
        task.add_done_callback(self._pruned_context_tasks.discard)

    def _calculate_weights(
        self,
        skills: list[str],
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, float]:
        """Calculate attribution weights for multi-skill scenarios.

        Uses multiple factors:
        - Recency of activation (0-0.4)
        - Input feature matching (0-0.3)
        - Call frequency (0-0.2)
        - Enabled status (0-0.1)

        Args:
            skills: Skills claiming this tool
            tool_name: Tool name
            tool_input: Tool input

        Returns:
            Dict mapping skill_name -> weight (sum = 1.0)
        """
        if len(skills) == 1:
            return {skills[0]: 1.0}

        scores: dict[str, float] = {}

        for skill in skills:
            score = 0.0

            # Factor 1: Recent activation (decays over 5 minutes)
            if skill in self._skill_activation_time:
                elapsed = (
                    datetime.now() - self._skill_activation_time[skill]
                ).seconds
                recency = max(0, 0.4 * (1 - elapsed / 300))
                score += recency

            # Factor 2: Input feature matching
            input_score = self._match_tool_input(skill, tool_name, tool_input)
            score += input_score * 0.3

            # Factor 3: Call frequency
            calls = self._skill_call_history.get(skill, 0)
            frequency = min(0.2, calls * 0.02)
            score += frequency

            # Factor 4: Enabled status
            if skill in self._enabled_skills:
                score += 0.1

            scores[skill] = score

        # Normalize to sum = 1.0
        total = sum(scores.values())
        if total > 0:
            return {k: v / total for k, v in scores.items()}
        else:
            # Equal distribution if no factors apply
            n = len(skills)
            return {s: 1.0 / n for s in skills}

    def _match_tool_input(
        self,
        skill: str,
        _tool_name: str,
        tool_input: dict[str, Any],
    ) -> float:
        """Match tool input against skill features.

        Args:
            skill: Skill name
            tool_name: Tool name
            tool_input: Tool input parameters

        Returns:
            Match score (0.0-1.0)
        """
        feature = self._inferencer.get_feature(skill)
        if not feature:
            return 0.5  # No feature info, neutral score

        input_str = str(tool_input).lower()
        matches = sum(
            1 for f in feature.file_extensions if f.lower() in input_str
        )
        matches += sum(1 for kw in feature.keywords if kw.lower() in input_str)

        if not feature.file_extensions and not feature.keywords:
            return 0.5

        total_features = len(feature.file_extensions) + len(feature.keywords)
        if total_features == 0:
            return 0.5

        return matches / total_features

    def _detect_skill_from_skill_md_read(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> Optional[str]:
        """检测Agent是否在读取某个技能的SKILL.md文件.

        当Agent调用read_file工具读取某个启用技能的SKILL.md时，
        这表明Agent正在主动了解该技能的使用方法，应将该技能激活。

        Args:
            tool_name: 工具名称
            tool_input: 工具输入参数

        Returns:
            技能名称，如果未检测到则返回None
        """
        if tool_name != "read_file":
            return None

        file_path = tool_input.get("file_path", "")
        if not file_path:
            return None

        path = Path(file_path)
        if path.name != "SKILL.md":
            return None

        if self._workspace_dir and not path.is_absolute():
            path = self._workspace_dir / path

        skill_name = path.parent.name
        if skill_name not in self._enabled_skills:
            return None

        if self._workspace_dir:
            resolved_path = path.resolve(strict=False)
            expected_paths = [
                skill_dir.resolve(strict=False) / "SKILL.md"
                for skill_dir in self._get_skill_asset_dirs(skill_name)
            ]
            if resolved_path not in expected_paths or not path.is_file():
                return None

        logger.info(
            "Detected skill '%s' from SKILL.md read: %s",
            skill_name,
            file_path,
        )
        return skill_name

    async def validate_tool_call_snapshot(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        """Check snapshot content before attributing a workspace skill read."""
        if tool_name != "read_file" or not self._skill_signatures:
            return True
        skill_name = self._detect_skill_from_skill_md_read(
            tool_name,
            tool_input,
        )
        if skill_name is None:
            return True
        expected = self._skill_signatures.get(skill_name)
        skill_dir = self._skill_dirs.get(skill_name)
        if expected is None or skill_dir is None:
            return False
        from .skills_manager import _build_signature

        actual = await asyncio.to_thread(_build_signature, skill_dir)
        if actual != expected:
            logger.warning(
                "Skipping skill attribution for changed skill '%s'",
                skill_name,
            )
            return False
        return True

    def _update_skill_state(self, skill: str) -> None:
        """Update skill state after a tool call.

        Args:
            skill: Skill name to update
        """
        self._skill_activation_time[skill] = datetime.now()
        self._skill_call_history[skill] = (
            self._skill_call_history.get(skill, 0) + 1
        )
        if skill in self._idle_counters:
            self._idle_counters[skill] = 0

    def get_skill_description(self, skill_name: str) -> str:
        """获取技能描述.

        从缓存的 _skill_descriptions 中获取，如果缓存中没有则尝试从
        内置技能目录的 SKILL.md 文件中获取。

        Args:
            skill_name: 技能名称

        Returns:
            技能描述字符串，如果未找到则返回空字符串
        """
        # 优先从缓存获取
        description = self._skill_descriptions.get(skill_name, "")
        if description:
            return description

        # Fallback 到内置技能目录
        return _get_skill_description(skill_name)

    async def start_skill(
        self,
        skill_name: str,
        trigger_tool: str,
        trigger_reason: str = "inferred",
        confidence: float = 1.0,
        *,
        load_hooks: bool = False,
    ) -> None:
        """Start a new skill invocation.

        Args:
            skill_name: Skill to start
            trigger_tool: Tool that triggered this skill
            trigger_reason: How the skill was detected
            confidence: Attribution confidence
            load_hooks: Whether this evidence permits loading skill hooks
        """
        # Get skill_id from cache; description is read by tracing only if needed
        skill_id = self._skill_ids.get(skill_name)

        logger.debug(
            "start_skill: skill_name=%s, skill_id=%s",
            skill_name,
            skill_id,
        )
        # Emit tracing event first to get span_id
        span_id = None
        if self._trace_manager and self._trace_id:
            try:
                span_id = await self._trace_manager.emit_skill_invocation(
                    trace_id=self._trace_id,
                    skill_name=skill_name,
                    user_id=self._user_id,
                    session_id=self._session_id,
                    channel=self._channel,
                    source_id=self._source_id,
                    skill_input={
                        "trigger_tool": trigger_tool,
                        "trigger_reason": trigger_reason,
                        "confidence": confidence,
                    },
                    user_name=self._user_name,
                    bbk_id=self._bbk_id,
                    skill_id=skill_id,
                )
            except Exception as e:
                logger.warning("Failed to emit skill start event: %s", e)

        # Push to context manager with span_id
        self._context_manager.push_skill(
            skill_name,
            trigger_reason=trigger_reason,
            confidence=confidence,
            span_id=span_id,
        )

        # Update state
        self._update_skill_state(skill_name)

        if self._confirmed_skill_callback is not None:
            try:
                result = self._confirmed_skill_callback(skill_name)
                if isawaitable(result):
                    await result
            except Exception as e:
                logger.warning(
                    "Failed to persist confirmed skill '%s': %s",
                    skill_name,
                    e,
                )

        if load_hooks and self._skill_hook_loader is not None:
            try:
                result = self._skill_hook_loader(skill_name)
                if isawaitable(result):
                    await result
            except Exception as e:
                logger.warning(
                    "Failed to load hooks for skill '%s': %s",
                    skill_name,
                    e,
                )

        logger.info(
            "Started skill '%s' (reason: %s, confidence: %.2f)",
            skill_name,
            trigger_reason,
            confidence,
        )

    async def _end_skill(self, _skill_name: str) -> None:
        """End a skill invocation.

        Args:
            _skill_name: Skill to end (unused, kept for API consistency)
        """
        # Pop from context manager
        context = self._context_manager.pop_skill()

        if context is None:
            return

        await self._emit_skill_end_for_context(context)

    async def _emit_skill_end_for_context(
        self,
        context: SkillExecutionContext,
    ) -> None:
        """根据上下文发出技能结束事件."""
        if context is None:
            return

        # Emit tracing event with span_id from context
        if self._trace_manager and self._trace_id and context.span_id:
            try:
                await self._trace_manager.end_skill_invocation(
                    trace_id=self._trace_id,
                    span_id=context.span_id,
                    skill_output=json.dumps(
                        {
                            "tools_called": context.tools_called,
                            "mcp_tools_called": context.mcp_tools_called,
                            "total_tools": len(context.tools_called)
                            + len(context.mcp_tools_called),
                        },
                    ),
                )
            except Exception as e:
                logger.warning("Failed to emit skill end event: %s", e)

    async def on_reasoning_end(self) -> None:
        """Handle end of LLM reasoning.

        Ends all active skills when reasoning completes.
        """
        await self._drain_pending_pruned_contexts()

        # End all skills in the stack (from top to bottom)
        while self._context_manager.skill_depth > 0:
            current = self._context_manager.current_skill
            if current:
                await self._end_skill(current)
            else:
                break

        # Clear any remaining state
        self._context_manager.clear()
        self._locked_skill_from_md = None
        self._pending_skill_md_continuation = None

    def reset(self) -> None:
        """Reset detector state for a new request."""
        self._flush_pruned_contexts_for_reset()
        self._skill_activation_time.clear()
        self._skill_call_history.clear()
        self._idle_counters.clear()
        self._recent_tools.clear()
        self._context_manager.clear()
        self._locked_skill_from_md = None
        self._pending_skill_md_continuation = None
        # Clear Layer 0 cache
        self._message_detected_skill = None
        self._message_detected_confidence = 0.0

    def restore_confirmed_skill(
        self,
        skill_name: str,
        *,
        allow_one_shot_continuation: bool = True,
    ) -> bool:
        """Ignore persisted attribution; only current-turn exact evidence counts."""
        del skill_name, allow_one_shot_continuation
        return False


# Global detector instance (per-request, should be reset)
_skill_invocation_detector: Optional[SkillInvocationDetector] = None


def get_skill_invocation_detector() -> SkillInvocationDetector:
    """Get the global skill invocation detector.

    Returns:
        SkillInvocationDetector instance
    """
    global _skill_invocation_detector
    if _skill_invocation_detector is None:
        _skill_invocation_detector = SkillInvocationDetector()
    return _skill_invocation_detector


def reset_skill_invocation_detector() -> None:
    """Reset the global detector (for testing or new request)."""
    global _skill_invocation_detector
    if _skill_invocation_detector is not None:
        _skill_invocation_detector.reset()
    _skill_invocation_detector = None
