# -*- coding: utf-8 -*-
"""Tenant path boundary enforcement for built-in tools.

This module provides centralized path resolution and authorization for
builtin local path tools, ensuring all resolved paths stay within the
current tenant's workspace root (WORKING_DIR/<tenant_id>).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from swe.config.context import (
    get_current_tenant_id,
)
from swe.constant import WORKING_DIR


class TenantPathBoundaryError(Exception):
    """Raised when a path violates the tenant workspace boundary."""

    def __init__(self, message: str, *, resolved_path: Optional[Path] = None):
        super().__init__(message)
        self.resolved_path = resolved_path


class TenantContextMissingError(TenantPathBoundaryError):
    """Raised when tenant context is required but not available."""

    pass


class PathTraversalError(TenantPathBoundaryError):
    """Raised when a path attempts to escape the tenant workspace."""

    pass


class AbsolutePathDeniedError(TenantPathBoundaryError):
    """Raised when an absolute path outside the tenant workspace is denied."""

    pass


def get_current_tenant_root() -> Path:
    """Get the current tenant's workspace root directory.

    Returns:
        Path to the tenant's workspace root (WORKING_DIR/<tenant_id>).

    Raises:
        TenantContextMissingError: If tenant ID is not set in context.
    """
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise TenantContextMissingError(
            "Tenant context is not available. "
            "This operation requires a valid tenant context."
        )
    return WORKING_DIR / tenant_id


def resolve_tenant_path(
    path: str,
    *,
    base_dir: Optional[Path] = None,
    allow_nonexistent: bool = False,
) -> Path:
    """Resolve a path against the tenant workspace and validate boundary.

    This function:
    1. Expands user home directory (~)
    2. Resolves the path against the base directory (defaults to tenant root)
    3. Resolves symlinks to detect symlink escape
    4. Validates the resolved path is within the tenant workspace

    Args:
        path: The input path (relative or absolute).
        base_dir: The base directory for relative path resolution.
                 Defaults to the current tenant's workspace root.
        allow_nonexistent: If True, allow paths to non-existent files
                          (needed for write operations). The parent directory
                          must still exist and be within the tenant boundary.

    Returns:
        The resolved absolute Path within the tenant workspace.

    Raises:
        TenantContextMissingError: If tenant ID is not set in context.
        AbsolutePathDeniedError: If an absolute path outside tenant root is provided.
        PathTraversalError: If the resolved path escapes the tenant workspace.
    """
    tenant_root = get_current_tenant_root()

    # Use tenant root as base if not specified
    if base_dir is None:
        base_dir = tenant_root
    else:
        # Ensure the provided base_dir is also within tenant root
        # Avoid recursion by directly validating the base_dir
        base_resolved = base_dir.resolve()
        try:
            base_resolved.relative_to(tenant_root.resolve())
        except ValueError:
            raise PathTraversalError(
                "Base directory escapes the tenant workspace boundary.",
                resolved_path=base_resolved,
            )
        base_dir = base_resolved

    # Expand user home directory
    expanded_path = os.path.expanduser(path)
    path_obj = Path(expanded_path)

    # Handle absolute paths - reject if outside tenant root
    if path_obj.is_absolute():
        try:
            # Check if the absolute path is within tenant root
            path_obj.relative_to(tenant_root)
        except ValueError:
            raise AbsolutePathDeniedError(
                "Absolute paths outside the tenant workspace are not allowed.",
                resolved_path=path_obj,
            )
        resolved = path_obj
    else:
        # Resolve relative path against base directory
        resolved = (base_dir / path_obj).resolve()

    # For non-existent paths (write targets), check parent directory
    if allow_nonexistent and not resolved.exists():
        # Ensure parent directory exists and is within tenant boundary
        parent = resolved.parent
        if not parent.exists():
            raise PathTraversalError(
                f"Parent directory does not exist: {parent.name}",
                resolved_path=resolved,
            )
        # Check parent is within tenant root
        try:
            parent.resolve().relative_to(tenant_root.resolve())
        except ValueError:
            raise PathTraversalError(
                "Path escapes the tenant workspace boundary.",
                resolved_path=resolved,
            )
        return resolved

    # For existing paths, resolve symlinks and check boundary
    try:
        # Use resolve() to follow symlinks
        resolved = resolved.resolve()
    except (OSError, RuntimeError) as e:
        raise PathTraversalError(
            f"Failed to resolve path: {e}",
            resolved_path=resolved,
        )

    # Verify the resolved path is within tenant root
    try:
        resolved.relative_to(tenant_root.resolve())
    except ValueError:
        raise PathTraversalError(
            "Path escapes the tenant workspace boundary.",
            resolved_path=resolved,
        )

    return resolved


def validate_path_within_tenant(path: Path | str) -> None:
    """Validate that a path is within the current tenant's workspace.

    This is a lightweight check for paths that are already resolved.

    Args:
        path: The path to validate (string or Path object).

    Raises:
        TenantContextMissingError: If tenant ID is not set in context.
        PathTraversalError: If the path is outside the tenant workspace.
    """
    tenant_root = get_current_tenant_root()

    path_obj = Path(path) if isinstance(path, str) else path
    resolved = path_obj.resolve()

    try:
        resolved.relative_to(tenant_root.resolve())
    except ValueError:
        raise PathTraversalError(
            "Path escapes the tenant workspace boundary.",
            resolved_path=resolved,
        )


def is_path_within_tenant(path: Path | str) -> bool:
    """Check if a path is within the current tenant's workspace.

    Args:
        path: The path to check (string or Path object).

    Returns:
        True if the path is within the tenant workspace, False otherwise.
        Returns False if tenant context is not available.
    """
    return is_path_within_tenant_with_base(path, base_dir=None)


def is_path_within_tenant_with_base(
    path: Path | str,
    base_dir: Optional[Path] = None,
) -> bool:
    """Check if a path is within the current tenant's workspace.

    Args:
        path: The path to check (string or Path object).
        base_dir: The base directory for resolving relative paths.
                  If None, uses the tenant root.

    Returns:
        True if the path is within the tenant workspace, False otherwise.
        Returns False if tenant context is not available.
    """
    import os

    try:
        tenant_root = get_current_tenant_root()
    except TenantContextMissingError:
        return False

    # Use tenant root as base if not specified
    if base_dir is None:
        base_dir = tenant_root
    else:
        # Validate that base_dir is within tenant root
        try:
            base_dir.resolve().relative_to(tenant_root.resolve())
        except ValueError:
            return False

    path_str = str(path)

    # Expand user home directory (tilde)
    expanded = os.path.expanduser(path_str)
    path_obj = Path(expanded)

    # If still relative after tilde expansion, resolve against base_dir
    if not path_obj.is_absolute():
        path_obj = base_dir / path_obj

    try:
        resolved = path_obj.resolve()
        resolved.relative_to(tenant_root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def make_permission_denied_response(operation: str) -> dict:
    """Create a standardized permission-denied error response.

    This ensures tenant boundary failures do not expose other tenants'
    resolved paths or internal directory structure.

    Args:
        operation: Description of the operation that was denied.

    Returns:
        A dictionary suitable for ToolResponse content.
    """
    return {
        "type": "text",
        "text": (
            f"Error: {operation} failed. "
            "The requested path is outside the allowed workspace."
        ),
    }
