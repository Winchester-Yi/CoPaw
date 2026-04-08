# -*- coding: utf-8 -*-
"""API endpoints for environment variable management."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...envs import load_envs, save_envs, delete_env_var
from ...config.utils import get_tenant_secrets_dir

router = APIRouter(prefix="/envs", tags=["envs"])


def _get_tenant_envs_path(request: Request) -> Path:
    """Get tenant-specific envs.json path."""
    tenant_id = getattr(request.state, "tenant_id", None)
    secrets_dir = get_tenant_secrets_dir(tenant_id)
    return secrets_dir / "envs.json"


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------


class EnvVar(BaseModel):
    """Single environment variable."""

    key: str = Field(..., description="Variable name")
    value: str = Field(..., description="Variable value")


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@router.get(
    "",
    response_model=List[EnvVar],
    summary="List all environment variables",
)
async def list_envs(request: Request) -> List[EnvVar]:
    """Return all configured env vars for the tenant."""
    envs = load_envs(_get_tenant_envs_path(request))
    return [EnvVar(key=k, value=v) for k, v in sorted(envs.items())]


@router.put(
    "",
    response_model=List[EnvVar],
    summary="Batch save environment variables",
    description="Replace all environment variables with "
    "the provided dict. Keys not present are removed.",
)
async def batch_save_envs(
    request: Request,
    body: Dict[str, str],
) -> List[EnvVar]:
    """Batch save – full replacement of all env vars for the tenant."""
    # Validate keys
    for key in body:
        if not key.strip():
            raise HTTPException(
                400,
                detail="Key cannot be empty",
            )
    cleaned = {k.strip(): v for k, v in body.items()}
    save_envs(cleaned, _get_tenant_envs_path(request))
    return [EnvVar(key=k, value=v) for k, v in sorted(cleaned.items())]


@router.delete(
    "/{key}",
    response_model=List[EnvVar],
    summary="Delete an environment variable",
)
async def delete_env(request: Request, key: str) -> List[EnvVar]:
    """Delete a single env var for the tenant."""
    envs_path = _get_tenant_envs_path(request)
    envs = load_envs(envs_path)
    if key not in envs:
        raise HTTPException(
            404,
            detail=f"Env var '{key}' not found",
        )
    envs = delete_env_var(key, envs_path)
    return [EnvVar(key=k, value=v) for k, v in sorted(envs.items())]
