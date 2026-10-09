# -*- coding: utf-8 -*-
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from ...app.deps import DbDep
from ...marketplace.models import CategoryItem
from ...utils.logging_utils import log_params

router = APIRouter()
logger = logging.getLogger(__name__)


class CreateCategoryRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class UpdateCategoryRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    branch_visible: bool | None = None


class ReorderCategoriesRequest(BaseModel):
    category_ids: list[int] = Field(..., min_length=1)


class ReorderCategoriesResponse(BaseModel):
    success: bool


def _require_manager(
    x_manager: Optional[str],
    x_user_role: Optional[str],
) -> None:
    if x_manager == "true" or x_user_role in {"admin", "manager"}:
        return
    raise HTTPException(status_code=403, detail="Manager access required")


def _category_select_sql(where_suffix: str = "") -> str:
    return (
        "SELECT id, source_id, name, sort_order, "
        "COALESCE(branch_visible, 1) AS branch_visible, created_at "
        "FROM swe_marketplace_categories c "
        f"{where_suffix} "
    )


def _skill_counts(request: Request, source_id: str) -> dict[int, int]:
    service = getattr(request.app.state, "marketplace", None)
    if service is None:
        return {}
    from ...marketplace.fs import load_index

    counts: dict[int, int] = {}
    for item in load_index(service.marketplace_root, source_id):
        if (
            item.item_type == "skill"
            and item.status == "active"
            and item.category_id is not None
        ):
            counts[item.category_id] = counts.get(item.category_id, 0) + 1
    return counts


async def _category_skill_count(
    request: Request,
    db: DbDep,
    source_id: str,
    category_id: int,
) -> int:
    counts = _skill_counts(request, source_id)
    if counts or getattr(request.app.state, "marketplace", None) is not None:
        return counts.get(category_id, 0)
    row = await db.fetch_one(
        "SELECT skill_count FROM swe_marketplace_categories "
        "WHERE id = %s AND source_id = %s",
        (category_id, source_id),
    )
    return int((row or {}).get("skill_count", 0))


def _with_skill_counts(
    rows: list[dict],
    counts: dict[int, int],
) -> list[dict]:
    return [
        {
            **row,
            "skill_count": counts.get(
                int(row["id"]),
                int(row.get("skill_count", 0) or 0),
            ),
        }
        for row in rows
    ]


@router.get("/market/categories", response_model=list[CategoryItem])
async def get_categories(
    request: Request,
    db: DbDep,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
):
    """获取当前 source-id 下的分类列表，按总行/非总行权限过滤."""
    if not x_source_id:
        raise HTTPException(
            status_code=400,
            detail="X-Source-Id header is required",
        )

    log_params(logger, request.method, request.url.path, source_id=x_source_id)
    if not db.is_connected:
        raise HTTPException(status_code=503, detail="Database unavailable")

    where = "WHERE c.source_id = %s"
    if x_bbk_id != "100":
        where += " AND COALESCE(c.branch_visible, 1) = 1"
    rows = await db.fetch_all(
        _category_select_sql(where) + "ORDER BY sort_order ASC",
        (x_source_id,),
    )
    return [
        CategoryItem(**row)
        for row in _with_skill_counts(
            rows,
            _skill_counts(request, x_source_id),
        )
    ]


@router.post(
    "/market/categories",
    response_model=CategoryItem,
    status_code=201,
)
async def create_category(
    request: Request,
    req: CreateCategoryRequest,
    db: DbDep,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_role: Optional[str] = Header(default=None, alias="X-User-Role"),
):
    """创建新分类."""
    if not x_source_id:
        raise HTTPException(
            status_code=400,
            detail="X-Source-Id header is required",
        )
    _require_manager(x_manager, x_user_role)

    log_params(
        logger,
        request.method,
        request.url.path,
        source_id=x_source_id,
        name=req.name,
    )
    if not db.is_connected:
        raise HTTPException(status_code=503, detail="Database unavailable")

    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="分类名称不能为空")

    # 检查同名分类是否已存在
    existing = await db.fetch_one(
        "SELECT id FROM swe_marketplace_categories WHERE source_id = %s AND name = %s",
        (x_source_id, name),
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"分类 '{name}' 已存在",
        )

    # 获取当前最大 sort_order
    row = await db.fetch_one(
        "SELECT COALESCE(MAX(sort_order), -1) AS max_order "
        "FROM swe_marketplace_categories WHERE source_id = %s",
        (x_source_id,),
    )
    max_order = row["max_order"] if row else -1

    # 插入新分类
    await db.execute(
        "INSERT INTO swe_marketplace_categories "
        "(source_id, name, sort_order, branch_visible) "
        "VALUES (%s, %s, %s, %s)",
        (x_source_id, name, max_order + 1, True),
    )

    # 查询刚插入的记录获取完整信息
    new_row = await db.fetch_one(
        _category_select_sql("WHERE source_id = %s AND name = %s"),
        (x_source_id, name),
    )

    if new_row is None:
        raise RuntimeError(f"Failed to fetch newly inserted category '{name}'")

    return CategoryItem(
        **{
            **new_row,
            "skill_count": _skill_counts(request, x_source_id).get(
                int(new_row["id"]),
                int(new_row.get("skill_count", 0) or 0),
            ),
        },
    )


@router.patch("/market/categories/{category_id}", response_model=CategoryItem)
async def update_category(
    category_id: int,
    request: Request,
    req: UpdateCategoryRequest,
    db: DbDep,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_role: Optional[str] = Header(default=None, alias="X-User-Role"),
):
    source_id = x_source_id
    if not source_id:
        raise HTTPException(
            status_code=400,
            detail="X-Source-Id header is required",
        )
    _require_manager(x_manager, x_user_role)
    log_params(
        logger,
        request.method,
        request.url.path,
        source_id=source_id,
        category_id=category_id,
    )
    if not db.is_connected:
        raise HTTPException(status_code=503, detail="Database unavailable")

    fields: list[str] = []
    params: list[object] = []
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="分类名称不能为空")
        existing = await db.fetch_one(
            "SELECT id FROM swe_marketplace_categories "
            "WHERE source_id = %s AND name = %s AND id <> %s",
            (source_id, name, category_id),
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"分类 '{name}' 已存在",
            )
        fields.append("name = %s")
        params.append(name)
    if req.branch_visible is not None:
        fields.append("branch_visible = %s")
        params.append(req.branch_visible)

    if fields:
        params.extend([category_id, source_id])
        await db.execute(
            "UPDATE swe_marketplace_categories SET "
            + ", ".join(fields)
            + " WHERE id = %s AND source_id = %s",
            tuple(params),
        )

    row = await db.fetch_one(
        _category_select_sql("WHERE id = %s AND source_id = %s"),
        (category_id, source_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Category not found")
    return CategoryItem(
        **{
            **row,
            "skill_count": _skill_counts(request, source_id).get(
                category_id,
                int(row.get("skill_count", 0) or 0),
            ),
        },
    )


@router.put(
    "/market/categories/reorder",
    response_model=ReorderCategoriesResponse,
)
async def reorder_categories(
    request: Request,
    req: ReorderCategoriesRequest,
    db: DbDep,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_role: Optional[str] = Header(default=None, alias="X-User-Role"),
):
    source_id = x_source_id
    if not source_id:
        raise HTTPException(
            status_code=400,
            detail="X-Source-Id header is required",
        )
    _require_manager(x_manager, x_user_role)
    log_params(logger, request.method, request.url.path, source_id=source_id)
    if not db.is_connected:
        raise HTTPException(status_code=503, detail="Database unavailable")
    await db.execute_many(
        "UPDATE swe_marketplace_categories SET sort_order = %s "
        "WHERE id = %s AND source_id = %s",
        [
            (sort_order, category_id, source_id)
            for sort_order, category_id in enumerate(req.category_ids)
        ],
    )
    return ReorderCategoriesResponse(success=True)


@router.delete(
    "/market/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_category(
    category_id: int,
    request: Request,
    db: DbDep,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_role: Optional[str] = Header(default=None, alias="X-User-Role"),
):
    source_id = x_source_id
    if not source_id:
        raise HTTPException(
            status_code=400,
            detail="X-Source-Id header is required",
        )
    _require_manager(x_manager, x_user_role)
    log_params(
        logger,
        request.method,
        request.url.path,
        source_id=source_id,
        category_id=category_id,
    )
    if not db.is_connected:
        raise HTTPException(status_code=503, detail="Database unavailable")
    skill_count = await _category_skill_count(
        request,
        db,
        source_id,
        category_id,
    )
    if skill_count > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "分类下仍有技能，无法删除",
                "skill_count": skill_count,
            },
        )
    deleted = await db.execute(
        "DELETE FROM swe_marketplace_categories WHERE id = %s AND source_id = %s",
        (category_id, source_id),
    )
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Category not found")
