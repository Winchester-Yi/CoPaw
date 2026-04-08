# -*- coding: utf-8 -*-
"""Instance management API router."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from .models import (
    AllocateUserRequest,
    AllocateUserResponse,
    AllocationListResponse,
    CreateInstanceRequest,
    DeleteAllocationRequest,
    InstanceListResponse,
    LogListResponse,
    MigrateUserRequest,
    OverviewStats,
    SourceListResponse,
    UpdateInstanceRequest,
    UserInstanceUrlResponse,
)
from .service import InstanceService
from .store import InstanceStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/instance", tags=["instance"])

# Global store and service instances
_store: Optional[InstanceStore] = None
_service: Optional[InstanceService] = None


def init_instance_module(db=None):
    """Initialize instance module with database connection.

    Args:
        db: TDSQLConnection instance. If None, will try to get from trace manager.
    """
    global _store, _service

    # If no db provided, try to get from trace manager
    if db is None:
        try:
            from ...tracing import get_trace_manager

            trace_manager = get_trace_manager()
            # pylint: disable=protected-access
            if (
                trace_manager._store
                and trace_manager._store._db
                and trace_manager._store._db.is_connected
            ):
                db = trace_manager._store._db
                logger.info(
                    "Instance module using database from trace manager",
                )
        except RuntimeError:
            logger.info(
                "Trace manager not available, instance module running without database",
            )

    _store = InstanceStore(db)
    _service = InstanceService(_store)


def get_service() -> InstanceService:
    """Get instance service."""
    global _service
    if _service is None:
        init_instance_module()
        # After init_instance_module, _service should not be None
        if _service is None:
            raise RuntimeError("Failed to initialize instance service")
    return _service


# ==================== Overview ====================


@router.get("/overview", response_model=OverviewStats)
async def get_overview():
    """Get overview statistics."""
    service = get_service()
    stats = await service.store.get_overview_stats()
    return OverviewStats(**stats)


# ==================== Sources (只读，用于下拉选择) ====================


@router.get("/sources", response_model=SourceListResponse)
async def list_sources():
    """Get all sources with statistics."""
    service = get_service()
    sources = await service.store.get_sources_with_stats()
    return SourceListResponse(sources=sources, total=len(sources))


# ==================== Instance management ====================


@router.get("/instances", response_model=InstanceListResponse)
async def list_instances(
    source_id: Optional[str] = Query(None, description="Filter by source ID"),
    status: Optional[str] = Query(None, description="Filter by status"),
):
    """Get instances with optional filters."""
    service = get_service()
    instances = await service.store.get_instances(
        source_id=source_id,
        status=status,
    )
    return InstanceListResponse(instances=instances, total=len(instances))


@router.get("/instances/{instance_id}")
async def get_instance(instance_id: str):
    """Get instance details with usage statistics."""
    service = get_service()
    instance = await service.store.get_instance_with_usage(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")
    return instance


@router.post("/instances")
async def create_instance(request: CreateInstanceRequest):
    """Create a new instance."""
    service = get_service()
    try:
        instance = await service.create_instance(request)
        return {"success": True, "data": instance}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.put("/instances/{instance_id}")
async def update_instance(instance_id: str, request: UpdateInstanceRequest):
    """Update instance."""
    service = get_service()
    try:
        instance = await service.update_instance(instance_id, request)
        return {"success": True, "data": instance}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/instances/{instance_id}")
async def delete_instance(instance_id: str):
    """Delete instance."""
    service = get_service()
    try:
        await service.delete_instance(instance_id)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ==================== User allocation ====================


@router.get("/user-ids", response_model=list[str])
async def get_user_ids():
    """Get list of all user IDs from allocations.

    Returns:
        List of all unique user IDs with active status
    """
    service = get_service()
    return await service.store.get_user_ids()


@router.get("/allocations", response_model=AllocationListResponse)
async def list_allocations(
    user_id: Optional[str] = Query(
        None,
        description="Filter by user ID (partial match)",
    ),
    source_id: Optional[str] = Query(None, description="Filter by source ID"),
    instance_id: Optional[str] = Query(
        None,
        description="Filter by instance ID",
    ),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """Get user allocations with filters and pagination."""
    service = get_service()
    allocations, total = await service.store.get_allocations(
        user_id=user_id,
        source_id=source_id,
        instance_id=instance_id,
        page=page,
        page_size=page_size,
    )
    return AllocationListResponse(allocations=allocations, total=total)


@router.get("/allocations/url", response_model=UserInstanceUrlResponse)
async def get_user_instance_url(
    user_id: str = Query(..., description="User ID"),
    source_id: str = Query(..., description="Source ID"),
):
    """Get user's instance URL by user_id and source_id."""
    service = get_service()
    return await service.get_user_instance_url(user_id, source_id)


@router.post("/allocations", response_model=AllocateUserResponse)
async def allocate_user(request: AllocateUserRequest):
    """Allocate user to an instance (auto or manual)."""
    service = get_service()
    try:
        return await service.allocate_user(request.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/allocations/migrate", response_model=AllocateUserResponse)
async def migrate_user(request: MigrateUserRequest):
    """Migrate user to another instance."""
    service = get_service()
    try:
        return await service.migrate_user(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/allocations")
async def delete_allocation(
    user_id: str = Query(..., description="User ID"),
    source_id: str = Query(..., description="Source ID"),
):
    """Delete user allocation."""
    service = get_service()
    try:
        del_request = DeleteAllocationRequest(
            user_id=user_id,
            source_id=source_id,
        )
        await service.delete_allocation(del_request)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ==================== Operation logs ====================


@router.get("/logs", response_model=LogListResponse)
async def list_logs(
    action: Optional[str] = Query(None, description="Filter by action type"),
    target_type: Optional[str] = Query(
        None,
        description="Filter by target type",
    ),
    target_id: Optional[str] = Query(
        None,
        description="Filter by target ID (partial match)",
    ),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """Get operation logs with filters and pagination."""
    service = get_service()
    logs, total = await service.store.get_logs(
        action=action,
        target_type=target_type,
        target_id=target_id,
        page=page,
        page_size=page_size,
    )
    return LogListResponse(logs=logs, total=total)
