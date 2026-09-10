"""Internal-only operations; SWE validates the manager and source identity."""

import hmac
import os

from fastapi import APIRouter, Depends, HTTPException, Request

from ..services.cron.batch_operations import (
    RetryRequest,
    failure_preview,
    retry_failed,
)
from ..services.cron.batch_run_state import RunStateNotReady


def require_internal(request: Request):
    secret = os.environ.get("SWE_INTERNAL_TOKEN") or os.environ.get(
        "SCHEDULER_SWE_INTERNAL_TOKEN"
    )
    if not secret or not hmac.compare_digest(
        request.headers.get("X-Internal-Token", ""),
        f"Bearer {secret}",
    ):
        raise HTTPException(403, "Internal authentication required")
    source = request.headers.get("X-Source-Id", "").strip()
    actor = request.headers.get("X-User-Id", "").strip()
    if not source or not actor:
        raise HTTPException(400, "Source and actor required")
    return source, actor


router = APIRouter(prefix="/scheduler/cron/dispatch/batches")


@router.get("/{batch_id}/failures")
async def preview(
    batch_id: str, failure_types: str = "", identity=Depends(require_internal)
):
    try:
        return await failure_preview(
            identity[0], batch_id, [t for t in failure_types.split(",") if t]
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{batch_id}/retry")
async def retry(
    batch_id: str, body: RetryRequest, identity=Depends(require_internal)
):
    try:
        return await retry_failed(identity[0], batch_id, identity[1], body)
    except RunStateNotReady as exc:
        raise HTTPException(503, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
