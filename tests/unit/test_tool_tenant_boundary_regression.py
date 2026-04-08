# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access
"""Regression tests for tenant path boundary in search and media tools.

These tests confirm that search and media tools cannot read or enumerate
sibling tenant directories.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from pathlib import Path
from typing import Generator
from unittest.mock import patch

from swe.config.context import tenant_context
from swe.agents.tools.file_search import grep_search, glob_search
from swe.agents.tools.view_media import view_image, view_video
from swe.agents.tools.send_file import send_file_to_user


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_tenant_dir(tmp_path: Path) -> Path:
    """Create a temporary directory structure with two tenants."""
    # Create tenant A directory
    tenant_a = tmp_path / "tenant_a"
    tenant_a.mkdir(parents=True)
    (tenant_a / "public.txt").write_text("tenant_a public content")
    (tenant_a / "private.txt").write_text("tenant_a private content")
    subdir_a = tenant_a / "subdir"
    subdir_a.mkdir()
    (subdir_a / "nested.txt").write_text("tenant_a nested content")

    # Create tenant B directory with secrets
    tenant_b = tmp_path / "tenant_b"
    tenant_b.mkdir(parents=True)
    (tenant_b / "secret.txt").write_text("tenant_b secret - should not be accessible")
    (tenant_b / "confidential.txt").write_text("tenant_b confidential - should not be accessible")
    subdir_b = tenant_b / "secrets"
    subdir_b.mkdir()
    (subdir_b / "top_secret.txt").write_text("tenant_b top secret")

    # Create an image file in tenant_a
    (tenant_a / "image.png").write_bytes(b"PNG fake image data")

    # Create a video file in tenant_b
    (tenant_b / "video.mp4").write_bytes(b"MP4 fake video data")

    return tmp_path


@pytest.fixture
def mock_working_dir(temp_tenant_dir: Path) -> Generator[Path, None, None]:
    """Mock WORKING_DIR to use temporary directory."""
    with patch("swe.constant.WORKING_DIR", temp_tenant_dir):
        with patch("swe.security.tenant_path_boundary.WORKING_DIR", temp_tenant_dir):
            yield temp_tenant_dir


# =============================================================================
# Regression tests for grep_search
# =============================================================================


class TestGrepSearchRegression:
    """Regression tests for grep_search tenant boundary enforcement."""

    @pytest.mark.asyncio
    async def test_cannot_search_sibling_tenant(self, mock_working_dir: Path):
        """grep_search should not be able to search sibling tenant directories."""
        with tenant_context(tenant_id="tenant_a"):
            # Try to search for content that exists in tenant_b
            result = await grep_search("secret", path="../tenant_b")

            # Should get a permission error, not search results
            result_text = result.content[0].get("text", "")
            assert "outside" in result_text.lower() or "Error" in result_text

    @pytest.mark.asyncio
    async def test_cannot_search_via_absolute_path(self, mock_working_dir: Path):
        """grep_search should not be able to search via absolute path to sibling tenant."""
        tenant_b_path = mock_working_dir / "tenant_b"

        with tenant_context(tenant_id="tenant_a"):
            result = await grep_search("secret", path=str(tenant_b_path))

            result_text = result.content[0].get("text", "")
            assert "outside" in result_text.lower() or "Error" in result_text

    @pytest.mark.asyncio
    async def test_can_search_own_tenant(self, mock_working_dir: Path):
        """grep_search should work normally within own tenant."""
        with tenant_context(tenant_id="tenant_a"):
            result = await grep_search("content", path=".")

            result_text = result.content[0].get("text", "")
            assert "tenant_a public content" in result_text
            assert "tenant_a private content" in result_text

    @pytest.mark.asyncio
    async def test_default_search_root_is_tenant_root(self, mock_working_dir: Path):
        """When no path specified, grep_search should default to tenant root."""
        with tenant_context(tenant_id="tenant_a"):
            # Search without specifying path - should only find tenant_a content
            result = await grep_search("content")

            result_text = result.content[0].get("text", "")
            assert "tenant_a" in result_text
            assert "tenant_b" not in result_text


# =============================================================================
# Regression tests for glob_search
# =============================================================================


class TestGlobSearchRegression:
    """Regression tests for glob_search tenant boundary enforcement."""

    @pytest.mark.asyncio
    async def test_cannot_glob_sibling_tenant(self, mock_working_dir: Path):
        """glob_search should not be able to enumerate sibling tenant directories."""
        with tenant_context(tenant_id="tenant_a"):
            # Try to glob files in tenant_b via traversal
            result = await glob_search("*.txt", path="../tenant_b")

            result_text = result.content[0].get("text", "")
            assert "outside" in result_text.lower() or "Error" in result_text

    @pytest.mark.asyncio
    async def test_cannot_glob_via_absolute_path(self, mock_working_dir: Path):
        """glob_search should not be able to enumerate via absolute path to sibling tenant."""
        tenant_b_path = mock_working_dir / "tenant_b"

        with tenant_context(tenant_id="tenant_a"):
            result = await glob_search("*.txt", path=str(tenant_b_path))

            result_text = result.content[0].get("text", "")
            assert "outside" in result_text.lower() or "Error" in result_text

    @pytest.mark.asyncio
    async def test_can_glob_own_tenant(self, mock_working_dir: Path):
        """glob_search should work normally within own tenant."""
        with tenant_context(tenant_id="tenant_a"):
            result = await glob_search("**/*.txt", path=".")

            result_text = result.content[0].get("text", "")
            assert "public.txt" in result_text
            assert "private.txt" in result_text
            assert "nested.txt" in result_text
            # Should not see tenant_b files
            assert "secret.txt" not in result_text
            assert "confidential.txt" not in result_text

    @pytest.mark.asyncio
    async def test_recursive_glob_stops_at_tenant_boundary(self, mock_working_dir: Path):
        """Recursive glob should not cross tenant boundary even with **."""
        with tenant_context(tenant_id="tenant_a"):
            # Even with **/* which could theoretically traverse up, should stay in tenant_a
            result = await glob_search("**/*")

            result_text = result.content[0].get("text", "")
            # Should find tenant_a files
            assert "public.txt" in result_text or "No files matched" not in result_text
            # Should not find tenant_b files
            assert "secret.txt" not in result_text
            assert "confidential.txt" not in result_text


# =============================================================================
# Regression tests for view_image
# =============================================================================


class TestViewImageRegression:
    """Regression tests for view_image tenant boundary enforcement."""

    @pytest.mark.asyncio
    async def test_cannot_view_image_in_sibling_tenant(self, mock_working_dir: Path):
        """view_image should not be able to access images in sibling tenant."""
        tenant_b_image = mock_working_dir / "tenant_b/image.png"
        # Create a real image file for tenant_b
        tenant_b_image.write_bytes(b"PNG fake image data")

        with tenant_context(tenant_id="tenant_a"):
            result = await view_image(str(tenant_b_image))

            result_text = result.content[0].get("text", "")
            assert "outside" in result_text.lower() or "Error" in result_text

    @pytest.mark.asyncio
    async def test_cannot_view_image_via_traversal(self, mock_working_dir: Path):
        """view_image should not be able to access images via path traversal."""
        with tenant_context(tenant_id="tenant_a"):
            result = await view_image("../tenant_b/image.png")

            result_text = result.content[0].get("text", "")
            assert "outside" in result_text.lower() or "Error" in result_text

    @pytest.mark.asyncio
    async def test_can_view_image_in_own_tenant(self, mock_working_dir: Path):
        """view_image should work normally within own tenant."""
        # Create a real-looking image file
        tenant_a_image = mock_working_dir / "tenant_a/image.png"
        tenant_a_image.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG magic bytes

        with tenant_context(tenant_id="tenant_a"):
            result = await view_image("image.png")

            result_text = result.content[0].get("text", "")
            # Should succeed - check that it's not an error
            assert "outside" not in result_text.lower()
            assert "Error" not in result_text


# =============================================================================
# Regression tests for send_file_to_user
# =============================================================================


class TestSendFileRegression:
    """Regression tests for send_file_to_user tenant boundary enforcement."""

    @pytest.mark.asyncio
    async def test_cannot_send_file_from_sibling_tenant(self, mock_working_dir: Path):
        """send_file_to_user should not be able to send files from sibling tenant."""
        tenant_b_file = mock_working_dir / "tenant_b/secret.txt"

        with tenant_context(tenant_id="tenant_a"):
            result = await send_file_to_user(str(tenant_b_file))

            result_text = result.content[0].get("text", "")
            assert "outside" in result_text.lower() or "Error" in result_text

    @pytest.mark.asyncio
    async def test_cannot_send_file_via_traversal(self, mock_working_dir: Path):
        """send_file_to_user should not be able to send files via path traversal."""
        with tenant_context(tenant_id="tenant_a"):
            result = await send_file_to_user("../tenant_b/secret.txt")

            result_text = result.content[0].get("text", "")
            assert "outside" in result_text.lower() or "Error" in result_text

    @pytest.mark.asyncio
    async def test_can_send_file_from_own_tenant(self, mock_working_dir: Path):
        """send_file_to_user should work normally within own tenant."""
        with tenant_context(tenant_id="tenant_a"):
            result = await send_file_to_user("public.txt")

            result_text = result.content[0].get("text", "")
            # Should succeed - check that it's not an error
            assert "outside" not in result_text.lower()
            assert "does not exist" not in result_text


# =============================================================================
# Cross-tenant isolation verification
# =============================================================================


class TestCrossTenantIsolation:
    """Tests to verify complete isolation between tenants."""

    @pytest.mark.asyncio
    async def test_tenant_a_cannot_list_tenant_b_files(self, mock_working_dir: Path):
        """Tenant A should not be able to enumerate files in Tenant B."""
        with tenant_context(tenant_id="tenant_a"):
            # Try various glob patterns that might reveal tenant_b structure
            result = await glob_search("../tenant_b/*")
            result_text = result.content[0].get("text", "")
            # Should get traversal error (rejects .. in pattern)
            assert ".." in result_text or "Error" in result_text

    @pytest.mark.asyncio
    async def test_tenant_b_cannot_list_tenant_a_files(self, mock_working_dir: Path):
        """Tenant B should not be able to enumerate files in Tenant A."""
        with tenant_context(tenant_id="tenant_b"):
            result = await glob_search("../tenant_a/*")
            result_text = result.content[0].get("text", "")
            # Should get traversal error (rejects .. in pattern)
            assert ".." in result_text or "Error" in result_text

    @pytest.mark.asyncio
    async def test_no_cross_tenant_content_leakage_via_grep(self, mock_working_dir: Path):
        """Grep should not find content from sibling tenant."""
        with tenant_context(tenant_id="tenant_a"):
            # Search for text that only exists in tenant_b
            result = await grep_search("tenant_b secret")

            result_text = result.content[0].get("text", "")
            # Should not find the actual tenant_b content (just the pattern in error msg)
            assert "tenant_b secret -" not in result_text
            assert "should not be accessible" not in result_text
            # Should show no matches or be limited to tenant_a
            assert "No matches" in result_text or "tenant_a" in result_text or "Error" in result_text
