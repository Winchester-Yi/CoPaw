# -*- coding: utf-8 -*-
"""Filter the rendered skill catalog without changing runtime registration."""

import copy
import logging
from pathlib import Path

from agentscope.tool import Toolkit, ToolResponse

from .client import JevClient

logger = logging.getLogger(__name__)
_PAGE_SIZE = 20
_DISCOVERY_HINT = (
    "If the listed skills do not fit the task, use search_skills to discover "
    "other available skills, then read the returned SKILL.md path."
)


class JevToolkit(Toolkit):
    visible_skill_names: set[str] | None = None

    def get_agent_skill_prompt(self) -> str | None:
        if self.visible_skill_names is None:
            return super().get_agent_skill_prompt()
        view = copy.copy(self)
        view.skills = {
            name: skill
            for name, skill in self.skills.items()
            if name in self.visible_skill_names
        }
        prompt = Toolkit.get_agent_skill_prompt(view) or ""
        if "search_skills" in self.tools:
            prompt = f"{prompt}\n\n{_DISCOVERY_HINT}".strip()
        return prompt or None


def create_search_skills(skills: dict):
    # Freeze the same validated, channel-scoped catalog used by this request.
    catalog = copy.deepcopy(skills)

    async def search_skills(query: str, offset: int = 0) -> ToolResponse:
        """Find available skills when the visible catalog does not fit.

        Args:
            query: Keywords; use an empty string to browse all skills.
            offset: Zero-based pagination offset.
        """
        terms = query.casefold().split()
        matches = [
            skill
            for skill in catalog.values()
            if all(
                term in (skill["name"] + " " + skill["description"]).casefold()
                for term in terms
            )
        ]
        # No lexical hit still offers a usable full-catalog discovery path.
        if not matches:
            matches = list(catalog.values())
        offset = max(0, offset)
        page = matches[offset : offset + _PAGE_SIZE]
        lines = [
            f'{skill["name"]}: {skill["description"]}\n'
            f'Read: {Path(skill["dir"]) / "SKILL.md"}'
            for skill in page
        ]
        next_offset = offset + len(page)
        more = next_offset < len(matches)
        lines.append(
            f"total={len(matches)} has_more={more} next_offset={next_offset}",
        )
        return ToolResponse(
            content=[{"type": "text", "text": "\n\n".join(lines)}]
        )

    return search_skills


async def prepare_skill_selection(agent, *, pinned=()) -> None:
    config = getattr(getattr(agent, "_agent_config", None), "jev", None)
    if config is None or not config.skill_selection.enabled:
        return
    toolkit = agent.toolkit
    if not isinstance(toolkit, JevToolkit):
        return
    settings = getattr(
        getattr(agent._agent_config, "tools", None), "builtin_tools", {}
    )
    discovery = (settings or {}).get("search_skills")
    if (
        "read_file" in toolkit.tools
        and (discovery is None or discovery.enabled)
        and "search_skills" not in toolkit.tools
        and agent._request_context.get("agent_role")
        not in {
            "subagent",
            "completion_judge",
        }
    ):
        toolkit.register_tool_function(create_search_skills(toolkit.skills))
    scores = await JevClient(config).relevance(
        agent._request_context.get("current_user_text", ""),
        {
            name: f'{skill["name"]}\n{skill["description"]}'
            for name, skill in toolkit.skills.items()
        },
    )
    if scores is None:
        toolkit.visible_skill_names = None
        return
    required = set(pinned) | set(getattr(agent, "_jev_pinned_skills", ()))
    toolkit.visible_skill_names = {
        name
        for name, score in scores.items()
        if score >= config.skill_selection.threshold or name in required
    }
    logger.info(
        "jev_skills candidates=%d retained=%d",
        len(scores),
        len(toolkit.visible_skill_names),
    )
