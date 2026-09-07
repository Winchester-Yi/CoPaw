# -*- coding: utf-8 -*-
"""Trusted resolution for typed context references on one agent turn."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal, TypeAlias
from xml.sax.saxutils import escape

from ..context_references import (
    _AgentRunnerMCPClientProvider,
    build_mcp_tool_reference_id,
    discover_mcp_tools,
)
from .skill_selection import SkillUseDirective, build_skill_use_directives

ContextReferenceType = Literal["skill", "mcp_tool", "workspace_file"]
MAX_CONTEXT_REFERENCES = 12
MAX_CONTEXT_REFERENCE_FIELD_LENGTH = 512


@dataclass(frozen=True)
class MCPToolPreferenceDirective:
    server: str
    name: str

    def render(self) -> str:
        return f"""<TOOL-PREFERENCE>
<instruction>
用户显式选择了此 MCP 工具。若它与当前任务相关，请优先考虑调用；但不要仅因被选择而强制调用。
</instruction>
<server>{escape(self.server)}</server>
<tool>{escape(self.name)}</tool>
</TOOL-PREFERENCE>"""


@dataclass(frozen=True)
class WorkspaceFileReferenceDirective:
    root: Literal["media", "static"]
    relative_path: str
    path: Path

    def render(self) -> str:
        return f"""<FILE-REFERENCE>
<instruction>
用户显式引用了这个工作区文件。在任务需要时使用 read_file 工具按需读取该绝对路径。
</instruction>
<path>{escape(str(self.path))}</path>
</FILE-REFERENCE>"""


ContextReferenceDirective: TypeAlias = (
    SkillUseDirective
    | MCPToolPreferenceDirective
    | WorkspaceFileReferenceDirective
)


def _valid_reference_parts(
    raw: object,
) -> tuple[ContextReferenceType, dict[str, object]] | None:
    if not isinstance(raw, dict):
        return None
    reference_type = raw.get("type")
    if reference_type not in {"skill", "mcp_tool", "workspace_file"}:
        return None
    reference_id = raw.get("id")
    if not isinstance(reference_id, str) or not reference_id:
        return None
    if any(
        isinstance(value, str)
        and len(value) > MAX_CONTEXT_REFERENCE_FIELD_LENGTH
        for value in raw.values()
    ):
        return None
    return reference_type, raw


def _validate_workspace_file_reference(
    workspace_dir: Path,
    raw: dict[str, object],
) -> WorkspaceFileReferenceDirective | None:
    root_value = raw.get("root")
    relative_path = raw.get("relative_path")
    reference_id = raw.get("id")
    if root_value == "media":
        root: Literal["media", "static"] = "media"
    elif root_value == "static":
        root = "static"
    else:
        return None
    if not isinstance(relative_path, str):
        return None
    if reference_id != f"workspace_file:{root}/{relative_path}":
        return None
    posix_path = PurePosixPath(relative_path)
    if (
        not relative_path
        or posix_path.is_absolute()
        or posix_path.as_posix() != relative_path
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        return None
    try:
        resolved_workspace = workspace_dir.resolve()
        resolved_root = (workspace_dir / root).resolve()
        resolved_root.relative_to(resolved_workspace)
        resolved_path = (workspace_dir / root / relative_path).resolve()
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    if not resolved_path.is_file():
        return None
    return WorkspaceFileReferenceDirective(
        root=root,
        relative_path=relative_path,
        path=resolved_path,
    )


def _normalize_context_references(
    references: Iterable[object],
) -> list[tuple[ContextReferenceType, dict[str, object]]]:
    """Keep bounded, valid references in their first-seen order."""
    normalized: list[tuple[ContextReferenceType, dict[str, object]]] = []
    seen: set[tuple[str, str]] = set()
    for raw in islice(references, MAX_CONTEXT_REFERENCES):
        parsed = _valid_reference_parts(raw)
        if parsed is None:
            continue
        reference_type, values = parsed
        identity = (reference_type, str(values["id"]))
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append((reference_type, values))
    return normalized


def _skill_directives_by_name(
    *,
    workspace_dir: Path,
    channel: str,
    normalized: Iterable[tuple[ContextReferenceType, dict[str, object]]],
    snapshot: Any | None = None,
) -> dict[str, SkillUseDirective]:
    skill_names = [
        raw["name"]
        for reference_type, raw in normalized
        if reference_type == "skill"
        and isinstance(raw.get("name"), str)
        and raw.get("id") == f"skill:{raw['name']}"
    ]
    skill_directives = build_skill_use_directives(
        workspace_dir=workspace_dir,
        channel=channel,
        selected_skill_names=skill_names,
        snapshot=snapshot,
    )
    return {directive.name: directive for directive in skill_directives}


async def _requested_mcp_tools_by_id(
    *,
    normalized: Iterable[tuple[ContextReferenceType, dict[str, object]]],
    agent_config: Any,
) -> dict[str, tuple[str, str]]:
    requested_mcp_ids = {
        str(raw["id"])
        for reference_type, raw in normalized
        if reference_type == "mcp_tool"
    }
    if not requested_mcp_ids:
        return {}
    available_tools = await discover_mcp_tools(
        manager=_AgentRunnerMCPClientProvider(),
        agent_config=agent_config,
    )
    return {
        tool.id: (tool.server, tool.name)
        for tool in available_tools
        if tool.id in requested_mcp_ids
    }


def _build_directives_from_normalized_references(
    *,
    workspace_dir: Path,
    normalized: Iterable[tuple[ContextReferenceType, dict[str, object]]],
    skills_by_name: dict[str, SkillUseDirective],
    mcp_by_id: dict[str, tuple[str, str]],
) -> list[ContextReferenceDirective]:
    directives: list[ContextReferenceDirective] = []
    for reference_type, raw in normalized:
        if reference_type == "skill":
            name = raw.get("name")
            if isinstance(name, str) and raw.get("id") == f"skill:{name}":
                directive = skills_by_name.get(name)
                if directive is not None:
                    directives.append(directive)
            continue
        if reference_type == "mcp_tool":
            server = raw.get("server")
            name = raw.get("name")
            reference_id = raw.get("id")
            if (
                isinstance(server, str)
                and isinstance(name, str)
                and isinstance(reference_id, str)
                and reference_id == build_mcp_tool_reference_id(server, name)
                and mcp_by_id.get(reference_id) == (server, name)
            ):
                directives.append(MCPToolPreferenceDirective(server, name))
            continue
        directive = _validate_workspace_file_reference(workspace_dir, raw)
        if directive is not None:
            directives.append(directive)
    return directives


async def build_context_reference_directives(
    *,
    workspace_dir: Path,
    channel: str,
    agent_config: Any,
    references: Iterable[object],
    snapshot: Any | None = None,
) -> list[ContextReferenceDirective]:
    """Revalidate structured client references and build trusted directives."""
    normalized = _normalize_context_references(references)
    skills_by_name = _skill_directives_by_name(
        workspace_dir=workspace_dir,
        channel=channel,
        normalized=normalized,
        snapshot=snapshot,
    )
    mcp_by_id = await _requested_mcp_tools_by_id(
        normalized=normalized,
        agent_config=agent_config,
    )
    return _build_directives_from_normalized_references(
        workspace_dir=workspace_dir,
        normalized=normalized,
        skills_by_name=skills_by_name,
        mcp_by_id=mcp_by_id,
    )
