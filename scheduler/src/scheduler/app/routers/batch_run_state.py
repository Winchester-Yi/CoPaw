"""Parent-scoped batch run-state operations for trusted internal callers."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .batch_operations import require_internal
from ..services.cron import batch_run_state as state

router = APIRouter(prefix="/scheduler/cron/dispatch/parents")


class RunStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: StrictBool
    expected_version: int = Field(ge=1)


def operation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, state.RunStateConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, state.RunStateNotReady):
        return HTTPException(503, str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(404, str(exc))
    return HTTPException(400, str(exc))


@router.get("/{parent_job_id}/run-state")
async def get_state(
    parent_job_id: str,
    tenant_id: str = Query(min_length=1),
    identity=Depends(require_internal),
):
    try:
        return await state.get_run_state(identity[0], tenant_id, parent_job_id)
    except (ValueError, LookupError, state.RunStateNotReady) as exc:
        raise operation_error(exc) from exc


@router.put("/{parent_job_id}/run-state")
async def update_state(
    parent_job_id: str,
    body: RunStateUpdate,
    tenant_id: str = Query(min_length=1),
    identity=Depends(require_internal),
):
    try:
        return await state.set_run_state(
            identity[0],
            tenant_id,
            parent_job_id,
            identity[1],
            body.paused,
            body.expected_version,
        )
    except (ValueError, LookupError, state.RunStateNotReady) as exc:
        raise operation_error(exc) from exc


@router.post("/{parent_job_id}/run-state/initialize")
async def initialize_state(
    parent_job_id: str,
    tenant_id: str = Query(min_length=1),
    identity=Depends(require_internal),
):
    try:
        db = state.get_db_connection()
        await state.assert_run_state_schema_ready(db)
        parent = await state.validated_parent(
            db, (identity[0], tenant_id, parent_job_id)
        )
        await state.initialize_parent(db, parent)
        return await state.get_run_state(identity[0], tenant_id, parent_job_id)
    except (ValueError, LookupError, state.RunStateNotReady) as exc:
        raise operation_error(exc) from exc
