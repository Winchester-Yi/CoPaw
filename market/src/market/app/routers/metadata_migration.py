# -*- coding: utf-8 -*-
"""从 NAS index.json 初始化市场元数据到 TDSQL."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ...marketplace.fs import get_marketplace_dir
from ...marketplace.metadata_migration import migrate_source_metadata

router = APIRouter()


class MetadataMigrationRequest(BaseModel):
    """市场元数据初始化请求."""

    source_ids: list[str] = Field(..., min_length=1)
    dry_run: bool = True


class MetadataMigrationResponse(BaseModel):
    """市场元数据初始化汇总结果."""

    dry_run: bool
    source_ids: list[str]
    total: int
    skills: int
    mcps: int
    migrated: int
    missing_content: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    details: list[dict[str, Any]]


def _require_manager(x_manager: Optional[str]) -> None:
    if x_manager != "true":
        raise HTTPException(status_code=403, detail="Manager access required")


def _normalize_source_ids(source_ids: list[str]) -> list[str]:
    normalized = list(
        dict.fromkeys(source_id.strip() for source_id in source_ids),
    )
    if any(not source_id for source_id in normalized):
        raise HTTPException(
            status_code=400,
            detail="source_ids cannot be empty",
        )
    return normalized


def _validate_source_ids(source_ids: list[str], marketplace_root) -> None:
    try:
        for source_id in source_ids:
            get_marketplace_dir(marketplace_root, source_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _summarize_results(
    source_ids: list[str],
    dry_run: bool,
    details: list[dict[str, Any]],
) -> MetadataMigrationResponse:
    def with_source_id(source_id: str, item: dict[str, Any]) -> dict[str, Any]:
        return {**item, "source_id": source_id}

    return MetadataMigrationResponse(
        dry_run=dry_run,
        source_ids=source_ids,
        total=sum(detail["total"] for detail in details),
        skills=sum(detail["skills"] for detail in details),
        mcps=sum(detail["mcps"] for detail in details),
        migrated=sum(detail["migrated"] for detail in details),
        missing_content=[
            with_source_id(detail["source_id"], missing)
            for detail in details
            for missing in detail["missing_content"]
        ],
        errors=[
            with_source_id(detail["source_id"], error)
            for detail in details
            for error in detail["errors"]
        ],
        details=details,
    )


@router.post(
    "/market/admin/metadata/migrate",
    response_model=MetadataMigrationResponse,
)
async def migrate_marketplace_metadata(
    body: MetadataMigrationRequest,
    request: Request,
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """将 NAS 市场索引元数据初始化或同步到 TDSQL."""
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    if not getattr(svc.db, "is_connected", False):
        raise HTTPException(status_code=503, detail="Database unavailable")

    source_ids = _normalize_source_ids(body.source_ids)
    _validate_source_ids(source_ids, svc.marketplace_root)
    details = [
        await migrate_source_metadata(
            db=svc.db,
            marketplace_root=svc.marketplace_root,
            source_id=source_id,
            dry_run=body.dry_run,
        )
        for source_id in source_ids
    ]
    return _summarize_results(source_ids, body.dry_run, details)
