# -*- coding: utf-8 -*-
"""Select search evidence without changing downstream compaction."""

import asyncio
import logging
import re
from uuid import uuid4

from .client import JevClient
from .context import current_jev_context
from ..security.tenant_path_boundary import (
    get_current_tool_base_dir,
    resolve_tenant_path,
    TenantPathBoundaryError,
)

logger = logging.getLogger(__name__)
_GREP_LINE = re.compile(r"^(.*):(\d+):([> ]) (.*)$")


def select_ids(
    scores: dict[str, float], top_k: int, threshold: float
) -> list[str]:
    ranked = sorted(scores, key=scores.get, reverse=True)
    return [
        key
        for index, key in enumerate(ranked)
        if index < top_k or scores[key] >= threshold
    ]


def _parse_group(lines: list[str]):
    entries = {}
    path = None
    for line in lines:
        match = _GREP_LINE.match(line)
        if match is None or (path is not None and path != match[1]):
            return None
        path = match[1]
        entries[int(match[2])] = line
    return (path, entries) if entries else None


def grep_snippets(lines: list[str]) -> list[str]:
    if "---" not in lines:
        # A scan limit can cut off the separator after a context window.
        if any(
            (match := _GREP_LINE.match(line)) and match[3] == " "
            for line in lines
        ):
            parsed = _parse_group(lines)
            if parsed and max(parsed[1]) - min(parsed[1]) + 1 == len(
                parsed[1]
            ):
                return ["\n".join(lines)]
        return list(dict.fromkeys(lines))
    groups = []
    current = []
    for line in lines:
        if line == "---":
            if current:
                groups.append(current)
                current = []
        else:
            current.append(line)
    if current:
        groups.append(current)
    merged = []
    previous = None
    for group in groups:
        parsed = _parse_group(group)
        if (
            parsed
            and previous
            and parsed[0] == previous[0]
            and min(parsed[1]) <= max(previous[1])
            and max(parsed[1]) >= min(previous[1])
        ):
            for number, line in parsed[1].items():
                if (
                    number not in previous[1]
                    or _GREP_LINE.match(line)[3] == ">"
                ):
                    previous[1][number] = line
            merged[-1] = "\n".join(
                previous[1][number] for number in sorted(previous[1])
            )
        else:
            merged.append("\n".join(group))
            previous = parsed
    return merged


def _save_original(original: str) -> str:
    base = get_current_tool_base_dir()
    path = resolve_tenant_path(
        f"tool_result/jev-{uuid4().hex}.txt",
        base_dir=base,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(original, encoding="utf-8")
    return str(path)


async def filter_search_results(
    lines: list[str],
    original: str,
    *,
    query: str,
    kind: str,
    intent: str = "",
) -> str:
    context = current_jev_context()
    if context is None or not lines:
        return original
    raw_prefix = "\n".join(lines)
    if not original.startswith(raw_prefix):
        return original
    snippets = (
        grep_snippets(lines) if kind == "grep" else list(dict.fromkeys(lines))
    )
    candidates = {f"snippet_{i}": text for i, text in enumerate(snippets)}
    state = (
        f"Task: {context.task}\nSearch: {query}\n"
        f"Current query intent: {intent}"
    )
    scores = await JevClient(context.config).relevance(state, candidates)
    if scores is None:
        return original
    settings = context.config.search_filtering
    selected = select_ids(scores, settings.top_k, settings.threshold)
    try:
        reference = await asyncio.to_thread(_save_original, original)
    except (OSError, TenantPathBoundaryError):
        logger.warning("jev_search_fallback reason=artifact_write_failed")
        return original
    logger.info(
        "jev_search candidates=%d retained=%d",
        len(candidates),
        len(selected),
    )
    result = "\n".join(candidates[key] for key in selected)
    result += original[len(raw_prefix) :]
    return (
        f"{result}\n\n[Jev: retained={len(selected)} candidates={len(candidates)}; "
        f"original: read_file {reference}]"
    )
