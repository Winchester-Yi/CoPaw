# -*- coding: utf-8 -*-
"""Tenant-scoped UI settings (language, theme, etc.).

Persisted in tenant workspace ``settings.json``, isolated per tenant.
All endpoints are public (no auth required) but tenant-scoped.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request

from ...config.utils import get_tenant_working_dir

router = APIRouter(prefix="/settings", tags=["settings"])

_VALID_LANGUAGES = {"en", "zh", "ja", "ru"}


def _get_settings_file(request: Request) -> Path:
    """Get tenant-specific settings.json path.

    Args:
        request: FastAPI request object.

    Returns:
        Path to tenant settings.json.
    """
    # Get tenant_id from request state (set by TenantIdentityMiddleware)
    tenant_id = getattr(request.state, "tenant_id", None)

    # Use tenant-specific directory
    tenant_dir = get_tenant_working_dir(tenant_id)
    return tenant_dir / "settings.json"


def _load(settings_file: Path) -> dict:
    """Load settings from file.

    Args:
        settings_file: Path to settings file.

    Returns:
        Settings dictionary.
    """
    if settings_file.is_file():
        try:
            return json.loads(settings_file.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(settings_file: Path, data: dict) -> None:
    """Save settings to file.

    Args:
        settings_file: Path to settings file.
        data: Settings dictionary.
    """
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        "utf-8",
    )


@router.get("/language", summary="Get UI language")
async def get_language(request: Request) -> dict:
    """Get UI language for current tenant.

    Args:
        request: FastAPI request object.

    Returns:
        Dictionary with language code.
    """
    settings_file = _get_settings_file(request)
    return {"language": _load(settings_file).get("language", "en")}


@router.put("/language", summary="Update UI language")
async def put_language(
    request: Request,
    body: dict = Body(..., description='e.g. {"language": "zh"}'),
) -> dict:
    """Update UI language for current tenant.

    Args:
        request: FastAPI request object.
        body: Request body with language code.

    Returns:
        Dictionary with updated language code.

    Raises:
        HTTPException: If language is invalid.
    """
    language = body.get("language", "").strip()
    if language not in _VALID_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid language, must be one of "
            f"{sorted(_VALID_LANGUAGES)}",
        )

    settings_file = _get_settings_file(request)
    data = _load(settings_file)
    data["language"] = language
    _save(settings_file, data)
    return {"language": language}
