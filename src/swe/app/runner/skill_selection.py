# -*- coding: utf-8 -*-
"""Trusted resolution of Console-selected skills for one agent turn."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

import frontmatter
from yaml import YAMLError

from ...agents.skills_manager import (
    resolve_effective_skill_dir,
    resolve_effective_skills,
)
from ...agents.skill_runtime_snapshot import WorkspaceSkillSnapshot


@dataclass(frozen=True)
class SkillUseDirective:
    name: str
    description: str
    path: Path
    content_signature: str | None = None

    def render(self) -> str:
        return f"""<SKILL-USE>
<instruction>
用户显式选择了下面 <name> 指定的技能。请先使用 read_file 工具读取 <path> 指定的 SKILL.md 文件。读取后必须严格按照该技能说明执行本轮任务：
- 不要跳过任何步骤，也不要把步骤改写成泛化或概括的回答；
- 不要重复询问技能文档中已经明确给出的内容；
- 不要凭猜测代替技能中明确的指令；
- 技能文档中提到的相对脚本、资源、模板路径，都必须按 <path> 指定的 SKILL.md 所在目录解析；执行脚本时请使用绝对路径，或把 cwd 设置为该技能目录；
- 始终使用中文回答。
</instruction>
<name>{escape(self.name)}</name>
<description>{escape(self.description)}</description>
<path>{escape(str(self.path))}</path>
</SKILL-USE>"""


def build_skill_use_directives(
    *,
    workspace_dir: Path,
    channel: str,
    selected_skill_names: Iterable[object],
    snapshot: WorkspaceSkillSnapshot | None = None,
) -> list[SkillUseDirective]:
    """Resolve selected names to readable effective skills."""
    if snapshot is None:
        effective_names = set(resolve_effective_skills(workspace_dir, channel))
    else:
        effective_names = set(
            resolve_effective_skills(
                workspace_dir,
                channel,
                _snapshot=snapshot,
            ),
        )
    directives: list[SkillUseDirective] = []
    seen: set[str] = set()

    for raw_name in selected_skill_names:
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if name not in effective_names:
            continue
        if snapshot is not None:
            runtime_skill = snapshot.skills.get(name)
            if runtime_skill is None:
                continue
            directives.append(
                SkillUseDirective(
                    name=name,
                    description=str(
                        runtime_skill.metadata.get("description") or "",
                    ),
                    path=runtime_skill.directory / "SKILL.md",
                    content_signature=runtime_skill.content_signature,
                ),
            )
            continue
        skill_dir = resolve_effective_skill_dir(workspace_dir, name)
        skill_path = skill_dir / "SKILL.md" if skill_dir is not None else None
        if skill_path is None or not skill_path.is_file():
            continue
        try:
            content = skill_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        try:
            description = str(
                frontmatter.loads(content).get("description") or "",
            )
        except (ValueError, TypeError, YAMLError):
            description = ""
        directives.append(
            SkillUseDirective(
                name=name,
                description=description,
                path=skill_path.resolve(),
            ),
        )

    return directives


def resolve_scenario_skill_names(
    *,
    workspace_dir: Path,
    channel: str,
    resource_ids: Iterable[object],
) -> list[str]:
    """Map market skill IDs to already-enabled local runtime skill names.

    Scenario selection never installs a package here.  It can only reuse a
    Skill already admitted to this tenant/Agent workspace, which preserves the
    existing scanner, channel enablement, and filesystem trust boundary.
    """
    wanted = {
        value.strip()
        for value in resource_ids
        if isinstance(value, str) and value.strip()
    }
    if not wanted:
        return []

    resolved: list[str] = []
    for name in resolve_effective_skills(workspace_dir, channel):
        skill_dir = resolve_effective_skill_dir(workspace_dir, name)
        skill_path = skill_dir / "SKILL.md" if skill_dir is not None else None
        if skill_path is None or not skill_path.is_file():
            continue
        try:
            metadata = frontmatter.loads(
                skill_path.read_text(encoding="utf-8"),
            )
        except (OSError, UnicodeError, ValueError, TypeError, YAMLError):
            continue
        skill_id = str(metadata.get("skill_id") or "").strip()
        if name in wanted or skill_id in wanted:
            resolved.append(name)
    return resolved
