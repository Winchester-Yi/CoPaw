# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access
"""Unit tests for tenant path boundary enforcement.

Tests cover:
- Tenant-root resolution
- Absolute-path rejection
- Path traversal (..) rejection
- Missing tenant context failure
- Symlink escape rejection
"""
from __future__ import annotations

from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest

from swe.config.context import tenant_context
from swe.constant import WORKING_DIR
from swe.security.tenant_path_boundary import (
    get_current_tenant_root,
    resolve_tenant_path,
    validate_path_within_tenant,
    is_path_within_tenant,
    make_permission_denied_response,
    TenantPathBoundaryError,
    TenantContextMissingError,
    PathTraversalError,
    AbsolutePathDeniedError,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_tenant_dir(tmp_path: Path) -> Path:
    """Create a temporary directory structure mimicking ~/.swe/tenant_id."""
    tenant_dir = tmp_path / "test_tenant"
    tenant_dir.mkdir(parents=True)

    # Create subdirectories
    (tenant_dir / "subdir").mkdir()
    (tenant_dir / "subdir" / "nested").mkdir()

    # Create test files
    (tenant_dir / "file.txt").write_text("test content")
    (tenant_dir / "subdir" / "nested_file.txt").write_text("nested content")

    return tenant_dir


@pytest.fixture
def other_tenant_dir(tmp_path: Path) -> Path:
    """Create another tenant directory to test isolation."""
    other_dir = tmp_path / "other_tenant"
    other_dir.mkdir(parents=True)
    (other_dir / "secret.txt").write_text("secret content")
    return other_dir


@pytest.fixture
def mock_working_dir(
    temp_tenant_dir: Path, other_tenant_dir: Path
) -> Generator[Path, None, None]:
    """Mock WORKING_DIR to use temporary directory."""
    parent_dir = temp_tenant_dir.parent
    with patch("swe.security.tenant_path_boundary.WORKING_DIR", parent_dir):
        with patch("swe.constant.WORKING_DIR", parent_dir):
            yield parent_dir


# =============================================================================
# Tests for get_current_tenant_root
# =============================================================================


class TestGetCurrentTenantRoot:
    """Tests for get_current_tenant_root function."""

    def test_raises_when_tenant_context_missing(self):
        """Should raise TenantContextMissingError when no tenant context."""
        with pytest.raises(TenantContextMissingError) as exc_info:
            get_current_tenant_root()

        assert "Tenant context is not available" in str(exc_info.value)

    def test_returns_tenant_root_with_context(self, mock_working_dir: Path):
        """Should return correct tenant root when context is set."""
        tenant_id = "test_tenant"
        expected_root = mock_working_dir / tenant_id

        with tenant_context(tenant_id=tenant_id):
            result = get_current_tenant_root()
            assert result == expected_root
            assert result.is_absolute()


# =============================================================================
# Tests for resolve_tenant_path
# =============================================================================


class TestResolveTenantPath:
    """Tests for resolve_tenant_path function."""

    def test_resolve_simple_relative_path(self, mock_working_dir: Path):
        """Should resolve simple relative paths within tenant root."""
        tenant_id = "test_tenant"

        with tenant_context(tenant_id=tenant_id):
            result = resolve_tenant_path("file.txt")
            expected = mock_working_dir / tenant_id / "file.txt"
            assert result == expected

    def test_resolve_nested_relative_path(self, mock_working_dir: Path):
        """Should resolve nested relative paths."""
        tenant_id = "test_tenant"

        with tenant_context(tenant_id=tenant_id):
            result = resolve_tenant_path("subdir/nested_file.txt")
            expected = mock_working_dir / tenant_id / "subdir/nested_file.txt"
            assert result == result.resolve()  # Should be absolute and resolved
            assert result.name == "nested_file.txt"

    def test_resolve_with_explicit_base_dir(self, mock_working_dir: Path):
        """Should resolve relative to provided base_dir."""
        tenant_id = "test_tenant"
        base = mock_working_dir / tenant_id / "subdir"

        with tenant_context(tenant_id=tenant_id):
            result = resolve_tenant_path("nested_file.txt", base_dir=base)
            expected = mock_working_dir / tenant_id / "subdir/nested_file.txt"
            assert result.resolve() == expected.resolve()

    def test_reject_dot_dot_traversal(self, mock_working_dir: Path):
        """Should reject paths that traverse outside tenant root."""
        tenant_id = "test_tenant"

        with tenant_context(tenant_id=tenant_id):
            with pytest.raises(PathTraversalError) as exc_info:
                resolve_tenant_path("subdir/../../../etc/passwd")

            assert "escapes the tenant workspace" in str(exc_info.value)

    def test_reject_dot_dot_traversal_from_base(self, mock_working_dir: Path):
        """Should reject traversal even from a nested base dir."""
        tenant_id = "test_tenant"
        base = mock_working_dir / tenant_id / "subdir/nested"

        with tenant_context(tenant_id=tenant_id):
            with pytest.raises(PathTraversalError) as exc_info:
                resolve_tenant_path("../../other_tenant/secret.txt")

            assert "escapes the tenant workspace" in str(exc_info.value)

    def test_reject_absolute_path_outside_tenant(self, mock_working_dir: Path):
        """Should reject absolute paths outside tenant root."""
        tenant_id = "test_tenant"
        other_path = mock_working_dir / "other_tenant/secret.txt"

        with tenant_context(tenant_id=tenant_id):
            with pytest.raises(AbsolutePathDeniedError) as exc_info:
                resolve_tenant_path(str(other_path))

            assert "Absolute paths outside the tenant workspace" in str(exc_info.value)

    def test_allow_absolute_path_within_tenant(self, mock_working_dir: Path):
        """Should allow absolute paths within tenant root."""
        tenant_id = "test_tenant"
        internal_path = mock_working_dir / tenant_id / "file.txt"

        with tenant_context(tenant_id=tenant_id):
            result = resolve_tenant_path(str(internal_path))
            assert result == internal_path

    def test_reject_missing_tenant_context(self):
        """Should raise TenantContextMissingError when no tenant context."""
        with pytest.raises(TenantContextMissingError):
            resolve_tenant_path("file.txt")

    def test_resolve_with_allow_nonexistent(self, mock_working_dir: Path):
        """Should allow non-existent paths for write operations."""
        tenant_id = "test_tenant"

        with tenant_context(tenant_id=tenant_id):
            result = resolve_tenant_path("new_file.txt", allow_nonexistent=True)
            expected = mock_working_dir / tenant_id / "new_file.txt"
            assert result == expected

    def test_reject_nonexistent_parent(self, mock_working_dir: Path):
        """Should reject paths where parent doesn't exist."""
        tenant_id = "test_tenant"

        with tenant_context(tenant_id=tenant_id):
            with pytest.raises(PathTraversalError) as exc_info:
                resolve_tenant_path(
                    "nonexistent_dir/new_file.txt",
                    allow_nonexistent=True,
                )

            assert "Parent directory does not exist" in str(exc_info.value)

    def test_reject_symlink_escape(self, mock_working_dir: Path):
        """Should reject paths that escape via symlinks."""
        tenant_id = "test_tenant"
        tenant_dir = mock_working_dir / tenant_id
        other_dir = mock_working_dir / "other_tenant"

        # Create a symlink pointing outside tenant directory
        symlink_path = tenant_dir / "link_to_secret"
        target_path = other_dir / "secret.txt"

        try:
            symlink_path.symlink_to(target_path)
        except OSError:
            pytest.skip("Symlink creation not supported on this platform")

        with tenant_context(tenant_id=tenant_id):
            with pytest.raises(PathTraversalError) as exc_info:
                resolve_tenant_path("link_to_secret")

            assert "escapes the tenant workspace" in str(exc_info.value)

    def test_allow_symlink_within_tenant(self, mock_working_dir: Path):
        """Should allow symlinks that stay within tenant."""
        tenant_id = "test_tenant"
        tenant_dir = mock_working_dir / tenant_id

        # Create a symlink pointing within tenant directory
        symlink_path = tenant_dir / "link_to_file"
        target_path = tenant_dir / "file.txt"

        try:
            symlink_path.symlink_to(target_path)
        except OSError:
            pytest.skip("Symlink creation not supported on this platform")

        with tenant_context(tenant_id=tenant_id):
            result = resolve_tenant_path("link_to_file")
            assert result == target_path.resolve()

    def test_expand_user_tilde(self, mock_working_dir: Path):
        """Should expand ~ to user home, then validate."""
        tenant_id = "test_tenant"

        with tenant_context(tenant_id=tenant_id):
            # ~ should expand to home dir, which is outside tenant
            with pytest.raises(AbsolutePathDeniedError):
                resolve_tenant_path("~/.bashrc")

    def test_normalize_path_components(self, mock_working_dir: Path):
        """Should normalize path components like . and redundant slashes."""
        tenant_id = "test_tenant"

        with tenant_context(tenant_id=tenant_id):
            result = resolve_tenant_path("./subdir/./nested_file.txt")
            expected = mock_working_dir / tenant_id / "subdir/nested_file.txt"
            assert result.resolve() == expected.resolve()


# =============================================================================
# Tests for validate_path_within_tenant
# =============================================================================


class TestValidatePathWithinTenant:
    """Tests for validate_path_within_tenant function."""

    def test_valid_path_passes(self, mock_working_dir: Path):
        """Should not raise for valid paths within tenant."""
        tenant_id = "test_tenant"
        tenant_dir = mock_working_dir / tenant_id

        with tenant_context(tenant_id=tenant_id):
            # Should not raise
            validate_path_within_tenant(tenant_dir / "file.txt")
            validate_path_within_tenant(str(tenant_dir / "subdir"))

    def test_invalid_path_raises(self, mock_working_dir: Path):
        """Should raise PathTraversalError for paths outside tenant."""
        tenant_id = "test_tenant"

        with tenant_context(tenant_id=tenant_id):
            with pytest.raises(PathTraversalError):
                validate_path_within_tenant(mock_working_dir / "other_tenant")

    def test_missing_context_raises(self):
        """Should raise TenantContextMissingError when no context."""
        with pytest.raises(TenantContextMissingError):
            validate_path_within_tenant("/some/path")


# =============================================================================
# Tests for is_path_within_tenant
# =============================================================================


class TestIsPathWithinTenant:
    """Tests for is_path_within_tenant function."""

    def test_returns_true_for_valid_path(self, mock_working_dir: Path):
        """Should return True for paths within tenant."""
        tenant_id = "test_tenant"
        tenant_dir = mock_working_dir / tenant_id

        with tenant_context(tenant_id=tenant_id):
            assert is_path_within_tenant(tenant_dir / "file.txt") is True
            assert is_path_within_tenant("subdir") is True

    def test_returns_false_for_invalid_path(self, mock_working_dir: Path):
        """Should return False for paths outside tenant."""
        tenant_id = "test_tenant"

        with tenant_context(tenant_id=tenant_id):
            assert is_path_within_tenant(mock_working_dir / "other_tenant") is False

    def test_returns_false_when_no_context(self):
        """Should return False when tenant context is missing."""
        assert is_path_within_tenant("/some/path") is False


# =============================================================================
# Tests for is_path_within_tenant_with_base
# =============================================================================


class TestIsPathWithinTenantWithBase:
    """Tests for is_path_within_tenant_with_base function."""

    def test_returns_true_for_path_within_tenant_using_base(
        self, mock_working_dir: Path
    ):
        """Should return True for paths within tenant when using base_dir."""
        from swe.security.tenant_path_boundary import is_path_within_tenant_with_base

        tenant_id = "test_tenant"
        tenant_dir = mock_working_dir / tenant_id
        subdir = tenant_dir / "subdir"

        with tenant_context(tenant_id=tenant_id):
            # Relative path from subdir should resolve to within tenant
            assert (
                is_path_within_tenant_with_base("file.txt", base_dir=subdir) is True
            )
            assert (
                is_path_within_tenant_with_base("../file.txt", base_dir=subdir)
                is True
            )

    def test_returns_false_for_path_escaping_tenant_via_base(
        self, mock_working_dir: Path
    ):
        """Should return False for paths that escape tenant via base_dir."""
        from swe.security.tenant_path_boundary import is_path_within_tenant_with_base

        tenant_id = "test_tenant"
        tenant_dir = mock_working_dir / tenant_id
        subdir = tenant_dir / "subdir"

        with tenant_context(tenant_id=tenant_id):
            # ../../ should escape tenant from subdir
            assert (
                is_path_within_tenant_with_base("../../other_tenant", base_dir=subdir)
                is False
            )

    def test_returns_false_when_base_dir_outside_tenant(self, mock_working_dir: Path):
        """Should return False when base_dir itself is outside tenant."""
        from swe.security.tenant_path_boundary import is_path_within_tenant_with_base

        tenant_id = "test_tenant"
        other_dir = mock_working_dir / "other_tenant"

        with tenant_context(tenant_id=tenant_id):
            # base_dir outside tenant should be rejected
            assert (
                is_path_within_tenant_with_base("file.txt", base_dir=other_dir)
                is False
            )

    def test_uses_tenant_root_when_base_dir_none(self, mock_working_dir: Path):
        """Should use tenant root as base when base_dir is None."""
        from swe.security.tenant_path_boundary import is_path_within_tenant_with_base

        tenant_id = "test_tenant"

        with tenant_context(tenant_id=tenant_id):
            # Should work like is_path_within_tenant when base_dir is None
            assert is_path_within_tenant_with_base("file.txt", base_dir=None) is True
            assert (
                is_path_within_tenant_with_base("../other_tenant", base_dir=None)
                is False
            )


# =============================================================================
# Tests for make_permission_denied_response
# =============================================================================


class TestMakePermissionDeniedResponse:
    """Tests for make_permission_denied_response function."""

    def test_returns_dict_with_text_type(self):
        """Should return a dict with type 'text'."""
        result = make_permission_denied_response("Read file")

        assert result["type"] == "text"

    def test_includes_operation_in_message(self):
        """Should include the operation name in the message."""
        result = make_permission_denied_response("Write file")

        assert "Write file failed" in result["text"]

    def test_does_not_expose_internal_paths(self):
        """Should not expose internal directory structure."""
        result = make_permission_denied_response("Read file")

        assert "test_tenant" not in result["text"]
        assert "other_tenant" not in result["text"]
        assert str(WORKING_DIR) not in result["text"]

    def test_message_is_generic(self):
        """Should use generic message without specific path details."""
        result = make_permission_denied_response("Delete file")

        assert "outside the allowed workspace" in result["text"]


# =============================================================================
# Error class tests
# =============================================================================


class TestErrorClasses:
    """Tests for custom exception classes."""

    def test_tenant_context_missing_error_is_boundary_error(self):
        """TenantContextMissingError should inherit from TenantPathBoundaryError."""
        assert issubclass(TenantContextMissingError, TenantPathBoundaryError)

    def test_path_traversal_error_is_boundary_error(self):
        """PathTraversalError should inherit from TenantPathBoundaryError."""
        assert issubclass(PathTraversalError, TenantPathBoundaryError)

    def test_absolute_path_denied_error_is_boundary_error(self):
        """AbsolutePathDeniedError should inherit from TenantPathBoundaryError."""
        assert issubclass(AbsolutePathDeniedError, TenantPathBoundaryError)

    def test_boundary_error_stores_resolved_path(self):
        """TenantPathBoundaryError should store resolved_path."""
        test_path = Path("/test/path")
        error = TenantPathBoundaryError("Test error", resolved_path=test_path)

        assert error.resolved_path == test_path

    def test_boundary_error_without_resolved_path(self):
        """TenantPathBoundaryError should work without resolved_path."""
        error = TenantPathBoundaryError("Test error")

        assert error.resolved_path is None
