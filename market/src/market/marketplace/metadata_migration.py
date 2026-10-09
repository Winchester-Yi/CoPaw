# -*- coding: utf-8 -*-
"""将现有 index.json 市场元数据迁移到 TDSQL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .fs import load_index
from .market_skill_registry import MarketSkillRegistry
from .mcp_market_registry import MCPMarketRegistry


async def migrate_source_metadata(
    *,
    db,
    marketplace_root: Path,
    source_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Migrate one source index idempotently and report missing content."""
    result: dict[str, Any] = {
        "source_id": source_id,
        "dry_run": dry_run,
        "total": 0,
        "skills": 0,
        "mcps": 0,
        "migrated": 0,
        "missing_content": [],
        "errors": [],
    }
    items = load_index(marketplace_root, source_id)
    skill_registry = MarketSkillRegistry(db)
    mcp_registry = MCPMarketRegistry(db)

    for item in items:
        if item.item_type not in {"skill", "mcp"}:
            continue
        result["total"] += 1
        content_path = (
            marketplace_root
            / source_id
            / ("skills" if item.item_type == "skill" else "mcp")
            / item.item_id
        )
        if item.item_type == "mcp":
            content_path = content_path / "mcp.json"
        if not content_path.exists():
            result["missing_content"].append(
                {
                    "item_type": item.item_type,
                    "item_id": item.item_id,
                    "content_path": str(content_path),
                },
            )

        if item.item_type == "skill":
            result["skills"] += 1
        else:
            result["mcps"] += 1
        if dry_run:
            continue

        try:
            if item.item_type == "skill":
                success = await skill_registry.upsert_market_skill(
                    source_id=source_id,
                    item_id=item.item_id,
                    skill_id=item.skill_id,
                    skill_name=item.name,
                    cn_name=item.chinese_name,
                    description=item.description,
                    version=item.version,
                    status=item.status,
                    category_id=item.category_id,
                    bbk_ids=item.bbk_ids,
                    content_path=str(content_path),
                    include_in_statistics=item.include_in_statistics,
                    creator_id=item.creator_id,
                    creator_name=item.creator_name,
                )
            else:
                success = await mcp_registry.upsert_market_mcp(
                    source_id=source_id,
                    item=item,
                    content_path=str(content_path),
                )
            if not success:
                result["errors"].append(
                    {
                        "item_type": item.item_type,
                        "item_id": item.item_id,
                        "error": "metadata upsert returned false",
                    },
                )
                continue
            result["migrated"] += 1
        except Exception as exc:  # pragma: no cover - DB-specific failures
            result["errors"].append(
                {
                    "item_type": item.item_type,
                    "item_id": item.item_id,
                    "error": str(exc),
                },
            )
    return result


async def compare_source_metadata(
    *,
    db,
    marketplace_root: Path,
    source_id: str,
) -> list[dict[str, Any]]:
    """Return metadata fields that differ between index.json and TDSQL."""
    items = load_index(marketplace_root, source_id)
    skill_registry = MarketSkillRegistry(db)
    mcp_registry = MCPMarketRegistry(db)
    differences: list[dict[str, Any]] = []
    for item in items:
        if item.item_type == "skill":
            stored = await skill_registry.get_market_skill(
                source_id,
                item.item_id,
            )
        elif item.item_type == "mcp":
            stored = await mcp_registry.get_market_mcp(
                source_id,
                item.item_id,
            )
        else:
            continue
        if stored is None:
            differences.append(
                {
                    "item_type": item.item_type,
                    "item_id": item.item_id,
                    "reason": "missing_in_tdsql",
                },
            )
            continue
        fields = (
            "name",
            "description",
            "version",
            "creator_id",
            "creator_name",
            "category_id",
            "bbk_ids",
            "status",
        )
        changed = {
            field: {
                "index": getattr(item, field),
                "tdsql": getattr(stored, field),
            }
            for field in fields
            if getattr(item, field) != getattr(stored, field)
        }
        if changed:
            differences.append(
                {
                    "item_type": item.item_type,
                    "item_id": item.item_id,
                    "reason": "metadata_mismatch",
                    "fields": changed,
                },
            )
    return differences
