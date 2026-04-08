# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,protected-access
"""Unit tests for shell tenant path boundary enforcement.

Tests cover:
- Allowed tenant-local shell paths
- Denied cross-tenant cwd
- Denied relative traversal
- Denied absolute-path access
"""
from __future__ import annotations

from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest

from swe.config.context import tenant_context
from swe.agents.tools.shell import (
    _extract_path_tokens,
    _validate_shell_paths,
    _resolve_cwd,
)
from swe.security.tenant_path_boundary import (
    TenantPathBoundaryError,
    TenantContextMissingError,
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
    (tenant_dir / "subdir" / "script.sh").write_text("#!/bin/bash\necho hello")

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
    with patch("swe.constant.WORKING_DIR", parent_dir):
        with patch("swe.security.tenant_path_boundary.WORKING_DIR", parent_dir):
            yield parent_dir


# =============================================================================
# Tests for _extract_path_tokens
# =============================================================================


class TestExtractPathTokens:
    """Tests for _extract_path_tokens function."""

    def test_extracts_absolute_paths(self):
        """Should extract absolute paths from commands."""
        cmd = "cat /etc/passwd && ls /var/log"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/etc/passwd" in file_paths
        assert "/var/log" in file_paths
        assert has_code_exec is False

    def test_extracts_relative_paths(self):
        """Should extract relative paths from commands."""
        cmd = "cat ./file.txt && ls ../parent"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "./file.txt" in file_paths
        assert "../parent" in file_paths
        assert has_code_exec is False

    def test_extracts_paths_after_flags(self):
        """Should extract paths following file-related flags."""
        cmd = "cat -f /path/to/file --input ./input.txt -o /output.txt"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/path/to/file" in file_paths
        assert "./input.txt" in file_paths
        assert "/output.txt" in file_paths
        assert has_code_exec is False

    def test_extracts_tilde_paths(self):
        """Should extract paths starting with tilde."""
        cmd = "cat ~/.bashrc && cp ~/file.txt /dest"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "~/.bashrc" in file_paths
        assert "~/file.txt" in file_paths
        assert has_code_exec is False

    def test_no_false_positives(self):
        """Should not extract non-path tokens."""
        cmd = "echo hello world 123"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "hello" not in file_paths
        assert "world" not in file_paths
        assert "123" not in file_paths
        assert has_code_exec is False

    def test_string_arguments_not_treated_as_paths(self):
        """String arguments to echo flags should not be treated as paths."""
        cmd = 'echo -n "/etc/hosts"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        # /etc/hosts is argument to -n flag in echo command (exempt), should not be extracted
        assert "/etc/hosts" not in file_paths
        assert has_code_exec is False

    def test_code_exec_flags_detected(self):
        """Commands with -c/-e flags should be flagged for rejection."""
        cmd = 'bash -c "cat /etc/passwd"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        # Should detect code execution flag
        assert has_code_exec is True
        # Should NOT extract paths from code strings (we reject the whole command)
        assert "/etc/passwd" not in file_paths

    def test_printf_string_not_treated_as_path(self):
        """printf format strings should not be treated as paths."""
        cmd = 'printf -- "/etc/hosts\\n"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/etc/hosts" not in file_paths
        assert has_code_exec is False

    def test_cat_with_double_dash_extracts_path(self):
        """cat -- /etc/hosts should extract /etc/hosts as path."""
        cmd = "cat -- /etc/hosts"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/etc/hosts" in file_paths
        assert has_code_exec is False

    def test_cp_with_double_dash_extracts_paths(self):
        """cp -- /src /dst should extract both paths."""
        cmd = "cp -- /etc/passwd /tmp/copied.txt"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/etc/passwd" in file_paths
        assert "/tmp/copied.txt" in file_paths
        assert has_code_exec is False

    def test_tar_with_absolute_path_extracts_path(self):
        """tar -xf /etc/hosts should extract /etc/hosts as path."""
        cmd = "tar -xf /etc/hosts"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert "/etc/hosts" in file_paths
        assert has_code_exec is False

    def test_wc_with_dash_c_not_code_exec(self):
        """wc -c file.txt should NOT be treated as code execution."""
        cmd = "wc -c file.txt"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is False

    def test_grep_with_dash_c_not_code_exec(self):
        """grep -c pattern file should NOT be treated as code execution."""
        cmd = "grep -c hello file.txt"
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is False

    def test_echo_with_dash_e_not_code_exec(self):
        """echo -e 'a\\nb' should NOT be treated as code execution."""
        cmd = 'echo -e "a\\nb"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is False

    def test_bash_with_combined_lce_flag_detected(self):
        """bash -lc 'cmd' should be detected as code execution."""
        cmd = 'bash -lc "cat /etc/hosts"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is True

    def test_sh_with_combined_ec_flag_detected(self):
        """sh -ec 'cmd' should be detected as code execution."""
        cmd = 'sh -ec "cat /etc/hosts"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is True

    def test_python_with_combined_flag_detected(self):
        """bash -vc 'code' should be detected as code execution."""
        cmd = 'bash -vc "echo hello"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is True

    def test_python3_with_standalone_c_flag_detected(self):
        """bash -c 'code' should be detected as code execution."""
        cmd = 'bash -c "cat /etc/passwd"'
        file_paths, has_code_exec = _extract_path_tokens(cmd)
        assert has_code_exec is True


# =============================================================================
# Tests for _validate_shell_paths
# =============================================================================


class TestValidateShellPaths:
    """Tests for _validate_shell_paths function."""

    def test_no_paths_returns_none(self, mock_working_dir: Path):
        """Should return None when no paths in command."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths("echo hello world", base_dir=tenant_dir)
            assert result is None

    def test_tenant_local_paths_allowed(self, mock_working_dir: Path):
        """Should allow paths within tenant workspace."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths("cat file.txt", base_dir=tenant_dir)
            assert result is None

    def test_absolute_path_outside_tenant_denied(self, mock_working_dir: Path):
        """Should reject absolute paths outside tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            other_path = mock_working_dir / "other_tenant/secret.txt"
            result = _validate_shell_paths(f"cat {other_path}", base_dir=tenant_dir)
            assert result is not None
            assert "outside the allowed workspace" in result

    def test_relative_traversal_denied(self, mock_working_dir: Path):
        """Should reject relative paths that traverse outside tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                "cat ../other_tenant/secret.txt", base_dir=tenant_dir
            )
            assert result is not None
            assert "outside the allowed workspace" in result

    def test_tilde_path_denied(self, mock_working_dir: Path):
        """Should reject paths starting with tilde (expands to home)."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths("cat ~/.bashrc", base_dir=tenant_dir)
            assert result is not None
            assert "outside the allowed workspace" in result

    def test_relative_path_against_cwd_within_tenant_allowed(
        self, mock_working_dir: Path
    ):
        """Should allow relative paths that resolve within tenant when using cwd."""
        tenant_dir = mock_working_dir / "test_tenant"
        subdir = tenant_dir / "subdir" / "nested"
        subdir.mkdir(parents=True, exist_ok=True)

        with tenant_context(tenant_id="test_tenant"):
            # ../ from subdir/nested should resolve to subdir, which is within tenant
            result = _validate_shell_paths("cat ../file.txt", base_dir=subdir)
            assert result is None

    def test_code_exec_flag_rejected(self, mock_working_dir: Path):
        """Should reject commands with -c/-e code execution flags."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths(
                'bash -c "cat /etc/passwd"',
                base_dir=tenant_dir
            )
            assert result is not None
            assert "code execution flags" in result

    def test_double_dash_paths_validated(self, mock_working_dir: Path):
        """cat -- /etc/hosts should reject /etc/hosts as outside tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths("cat -- /etc/hosts", base_dir=tenant_dir)
            assert result is not None
            assert "outside the allowed workspace" in result

    def test_tar_with_absolute_path_rejected(self, mock_working_dir: Path):
        """tar -xf /etc/hosts should reject /etc/hosts as outside tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths("tar -xf /etc/hosts", base_dir=tenant_dir)
            assert result is not None
            assert "outside the allowed workspace" in result

    def test_wc_with_dash_c_allowed(self, mock_working_dir: Path):
        """wc -c file.txt should be allowed (not code execution)."""
        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "test.txt").write_text("hello")
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths("wc -c test.txt", base_dir=tenant_dir)
            assert result is None

    def test_grep_with_dash_c_allowed(self, mock_working_dir: Path):
        """grep -c pattern file should be allowed (not code execution)."""
        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "test.txt").write_text("hello")
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths("grep -c hello test.txt", base_dir=tenant_dir)
            assert result is None

    def test_bash_with_combined_flag_rejected(self, mock_working_dir: Path):
        """bash -lc 'cmd' should be rejected as code execution."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths('bash -lc "cat /etc/hosts"', base_dir=tenant_dir)
            assert result is not None
            assert "code execution flags" in result

    def test_sh_with_combined_ec_flag_rejected(self, mock_working_dir: Path):
        """sh -ec 'cmd' should be rejected as code execution."""
        tenant_dir = mock_working_dir / "test_tenant"
        with tenant_context(tenant_id="test_tenant"):
            result = _validate_shell_paths('sh -ec "cat /etc/hosts"', base_dir=tenant_dir)
            assert result is not None
            assert "code execution flags" in result


# =============================================================================
# Tests for _resolve_cwd
# =============================================================================


class TestResolveCwd:
    """Tests for _resolve_cwd function."""

    def test_returns_tenant_root_when_cwd_none(self, mock_working_dir: Path):
        """Should return tenant root when cwd is None."""
        with tenant_context(tenant_id="test_tenant"):
            result = _resolve_cwd(None)
            expected = mock_working_dir / "test_tenant"
            assert result == expected

    def test_returns_resolved_cwd_when_within_tenant(self, mock_working_dir: Path):
        """Should return resolved cwd when within tenant."""
        tenant_dir = mock_working_dir / "test_tenant"
        subdir = tenant_dir / "subdir"

        with tenant_context(tenant_id="test_tenant"):
            result = _resolve_cwd(subdir)
            assert result == subdir.resolve()

    def test_raises_when_cwd_outside_tenant(self, mock_working_dir: Path):
        """Should raise TenantPathBoundaryError when cwd outside tenant."""
        other_dir = mock_working_dir / "other_tenant"

        with tenant_context(tenant_id="test_tenant"):
            with pytest.raises(TenantPathBoundaryError) as exc_info:
                _resolve_cwd(other_dir)

            assert "outside the tenant workspace" in str(exc_info.value)

    def test_raises_when_tenant_context_missing(self):
        """Should raise TenantContextMissingError when no tenant context."""
        with pytest.raises(TenantContextMissingError):
            _resolve_cwd(None)

    def test_rejects_traversal_cwd(self, mock_working_dir: Path):
        """Should reject cwd with path traversal."""
        tenant_dir = mock_working_dir / "test_tenant"
        traversal_path = tenant_dir / "../other_tenant"

        with tenant_context(tenant_id="test_tenant"):
            with pytest.raises(TenantPathBoundaryError):
                _resolve_cwd(traversal_path)


# =============================================================================
# Integration tests for execute_shell_command
# =============================================================================


class TestExecuteShellCommand:
    """Integration tests for execute_shell_command with tenant boundary."""

    @pytest.mark.asyncio
    async def test_executes_within_tenant(self, mock_working_dir: Path):
        """Should execute command within tenant workspace."""
        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "test.txt").write_text("hello world")

        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            result = await execute_shell_command("cat test.txt")

            assert result.content[0]["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_rejects_cross_tenant_cwd(self, mock_working_dir: Path):
        """Should reject command with cwd outside tenant."""
        from swe.agents.tools.shell import execute_shell_command

        other_dir = mock_working_dir / "other_tenant"

        with tenant_context(tenant_id="test_tenant"):
            result = await execute_shell_command("ls", cwd=other_dir)

            assert "outside the tenant workspace" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_rejects_cross_tenant_path_in_command(self, mock_working_dir: Path):
        """Should reject command referencing paths outside tenant."""
        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            other_path = mock_working_dir / "other_tenant/secret.txt"
            result = await execute_shell_command(f"cat {other_path}")

            assert "outside the allowed workspace" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_allows_valid_relative_paths(self, mock_working_dir: Path):
        """Should allow commands with valid relative paths."""
        tenant_dir = mock_working_dir / "test_tenant"
        (tenant_dir / "subdir" / "nested_file.txt").write_text("nested content")

        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            result = await execute_shell_command("cat subdir/nested_file.txt")

            assert "nested content" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_rejects_traversal_in_command(self, mock_working_dir: Path):
        """Should reject commands with path traversal."""
        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            result = await execute_shell_command("cat ../other_tenant/secret.txt")

            assert "outside the allowed workspace" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_rejects_code_exec_in_command(self, mock_working_dir: Path):
        """Should reject commands with code execution flags."""
        from swe.agents.tools.shell import execute_shell_command

        with tenant_context(tenant_id="test_tenant"):
            result = await execute_shell_command('bash -c "echo hello"')

            assert "code execution flags" in result.content[0]["text"]
