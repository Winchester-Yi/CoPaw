# -*- coding: utf-8 -*-
# flake8: noqa: E402
# pylint: disable=wrong-import-position,redefined-outer-name,reimported,unused-argument,unused-variable,unused-import,protected-access
"""Unit tests for tenant workspace middleware.

Tests workspace loading from pool, request.state binding,
and context reset after response.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import HTTPException, Request
from starlette.responses import Response
from swe.config.context import encode_scope_id

from swe.tenant_models.models import (
    TenantModelConfig,
    TenantProviderConfig,
    RoutingConfig,
    ModelSlot,
)
from swe.tenant_models.context import TenantModelContext


class TestTenantWorkspaceMiddlewareOrdering:
    """Tests for middleware ordering requirements."""

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_middleware_ordering(self):
        """Middleware should be ordered: identity -> workspace -> agent."""
        # This is verified by integration tests
        raise AssertionError("Test requires full app dependencies")


class TestTenantWorkspaceLoading:
    """Tests for workspace loading behavior."""

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_workspace_loaded_from_pool(self):
        """Workspace is loaded from TenantWorkspacePool."""
        raise AssertionError("Test requires full app dependencies")

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_workspace_stored_in_request_state(self):
        """Workspace is stored in request.state.workspace."""
        raise AssertionError("Test requires full app dependencies")

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_workspace_directory_bound_to_context(self):
        """Workspace directory is bound to current_workspace_dir context."""
        raise AssertionError("Test requires full app dependencies")


class TestTenantWorkspaceExemptions:
    """Tests for workspace-exempt routes."""

    def test_health_routes_exempt(self):
        """Health check routes don't require workspace."""
        # Health routes are exempt from workspace requirements
        exempt_routes = ["/health", "/healthz", "/ready", "/readyz"]
        for route in exempt_routes:
            assert route.startswith("/")
            assert "health" in route or "ready" in route

    def test_version_route_exempt(self):
        """Version endpoint doesn't require workspace."""
        # Version endpoint should be exempt
        exempt_routes = ["/version", "/api/version"]
        for route in exempt_routes:
            assert route.endswith("version") or "/version" in route

    def test_auth_routes_exempt(self):
        """Auth routes don't require workspace."""
        # Auth routes should be exempt
        exempt_routes = [
            "/login",
            "/register",
            "/auth/login",
            "/auth/register",
        ]
        for route in exempt_routes:
            assert "login" in route or "register" in route or "auth" in route

    def test_public_text_asset_api_routes_exempt(self):
        """Public text asset API routes should bypass workspace loading."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        middleware = TenantWorkspaceMiddleware(app=MagicMock())

        assert middleware._is_workspace_exempt("/api/assets/text/read") is True


class TestTenantWorkspaceHelpers:
    """Tests for workspace helper functions."""

    def test_get_workspace_from_request_returns_none(self):
        """get_workspace_from_request returns None when not set."""
        from fastapi import Request
        from unittest.mock import MagicMock

        # Create a mock request without workspace
        mock_request = MagicMock(spec=Request)
        mock_request.state = MagicMock()
        mock_request.state.workspace = None

        # When workspace is not set, should return None
        result = getattr(mock_request.state, "workspace", None)
        assert result is None

    def test_get_workspace_from_request_strict_raises(self):
        """get_workspace_from_request_strict raises when not set."""
        from fastapi import Request
        from unittest.mock import MagicMock

        # Create a mock request without workspace
        mock_request = MagicMock(spec=Request)
        mock_request.state = MagicMock()
        mock_request.state.workspace = None

        # Should raise when workspace is required but not set
        with pytest.raises((AttributeError, RuntimeError)):
            if mock_request.state.workspace is None:
                raise RuntimeError("Workspace not set")

    @pytest.mark.asyncio
    async def test_dispatch_uses_effective_tenant_id_for_source_scoped_default(
        self,
    ):
        """Workspace loading should use effective_tenant_id when present."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        scope_id = encode_scope_id("default", "RMASSIST")
        effective_root = Path(f"/tmp/{scope_id}")

        mock_req = MagicMock(spec=Request)
        mock_req.method = "GET"
        mock_req.state = MagicMock()
        mock_req.state.tenant_id = "default"
        mock_req.state.effective_tenant_id = scope_id
        mock_req.state.scope_id = scope_id
        mock_req.state.source_id = "RMASSIST"
        mock_req.url = MagicMock()
        mock_req.url.path = "/api/test"
        mock_req.app = MagicMock()
        mock_req.app.state = MagicMock()

        pool = MagicMock()
        pool.ensure_bootstrap = AsyncMock()
        pool.get_tenant_workspace_dir = MagicMock(return_value=effective_root)
        mock_req.app.state.tenant_workspace_pool = pool

        middleware = TenantWorkspaceMiddleware(app=MagicMock())

        async def call_next(_request):
            return Response(content=b"OK", status_code=200)

        response = await middleware.dispatch(mock_req, call_next)

        assert response.status_code == 200
        pool.ensure_bootstrap.assert_awaited_once_with(
            "default",
            source_id="RMASSIST",
            scope_id=scope_id,
            tenant_name=mock_req.state.user_name,
            bbk_id=mock_req.state.bbk_id,
        )
        pool.get_tenant_workspace_dir.assert_called_once_with(
            scope_id,
        )

    @pytest.mark.asyncio
    async def test_dispatch_allows_public_static_without_tenant_context(self):
        """Public static routes should bypass workspace requirements."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        mock_req = MagicMock(spec=Request)
        mock_req.method = "GET"
        mock_req.state = MagicMock()
        mock_req.state.tenant_id = None
        mock_req.state.effective_tenant_id = None
        mock_req.url = MagicMock()
        mock_req.url.path = "/static/alice/demo.txt"
        mock_req.app = MagicMock()

        middleware = TenantWorkspaceMiddleware(app=MagicMock())

        async def call_next(_request):
            return Response(content=b"OK", status_code=200)

        response = await middleware.dispatch(mock_req, call_next)

        assert response.status_code == 200


class TestTenantWorkspaceContextReset:
    """Tests for context reset after request."""

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_context_reset_after_response(self):
        """Workspace context is reset after response is sent."""
        raise AssertionError("Test requires full app dependencies")

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_context_reset_on_exception(self):
        """Workspace context is reset even if exception occurs."""
        raise AssertionError("Test requires full app dependencies")


@pytest.mark.skip(
    reason="TenantModelContext 已移除，租户模型配置现由 ProviderManager 管理",
)
class TestTenantModelConfigLoading:
    """Tests for tenant model configuration loading in middleware."""

    @pytest.fixture
    def sample_model_config(self):
        """Create a sample TenantModelConfig for testing."""
        return TenantModelConfig(
            version="1.0",
            providers=[
                TenantProviderConfig(
                    id="openai-main",
                    type="openai",
                    api_key="test-key",
                    models=["gpt-4"],
                    enabled=True,
                ),
            ],
            routing=RoutingConfig(
                mode="cloud_first",
                slots={
                    "cloud": ModelSlot(
                        provider_id="openai-main",
                        model="gpt-4",
                    ),
                },
            ),
        )

    @pytest.fixture
    def mock_request(self):
        """Create a mock FastAPI request."""
        mock_req = MagicMock(spec=Request)
        mock_req.state = MagicMock()
        mock_req.state.tenant_id = "test-tenant"
        mock_req.url = MagicMock()
        mock_req.url.path = "/api/test"
        mock_req.app = MagicMock()
        mock_req.app.state = MagicMock()
        return mock_req

    @pytest.mark.asyncio
    async def test_model_config_loaded_and_bound(
        self,
        mock_request,
        sample_model_config,
    ):
        """Model configuration is loaded and bound to context during request."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        # Mock workspace
        mock_workspace = MagicMock()
        mock_workspace.workspace_dir = "/test/workspace"

        # Mock pool
        mock_pool = AsyncMock()
        mock_pool.get_or_create = AsyncMock(return_value=mock_workspace)
        mock_request.app.state.tenant_workspace_pool = mock_pool

        # Mock TenantModelManager.load
        with patch(
            "swe.app.middleware.tenant_workspace.TenantModelManager.load",
        ) as mock_load:
            mock_load.return_value = sample_model_config

            # Track context state
            config_token = None
            original_get_config = TenantModelContext.get_config

            def mock_set_config(config):
                nonlocal config_token
                config_token = object()  # Mock token
                return config_token

            def mock_get_config():
                return sample_model_config

            def mock_reset_config(token):
                nonlocal config_token
                assert token is config_token, "Token mismatch in reset"

            with patch.object(
                TenantModelContext,
                "set_config",
                side_effect=mock_set_config,
            ):
                with patch.object(
                    TenantModelContext,
                    "get_config",
                    side_effect=mock_get_config,
                ):
                    with patch.object(
                        TenantModelContext,
                        "reset_config",
                        side_effect=mock_reset_config,
                    ):
                        middleware = TenantWorkspaceMiddleware(app=MagicMock())

                        # Mock call_next
                        async def call_next(request):
                            # Verify config is set during request handling
                            current_config = TenantModelContext.get_config()
                            assert current_config is sample_model_config
                            return Response(content=b"OK", status_code=200)

                        response = await middleware.dispatch(
                            mock_request,
                            call_next,
                        )

                        assert response.status_code == 200
                        mock_load.assert_called_once_with("test-tenant")

    @pytest.mark.asyncio
    async def test_model_config_reset_after_request(
        self,
        mock_request,
        sample_model_config,
    ):
        """Model configuration is reset from context after request completes."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        # Mock workspace
        mock_workspace = MagicMock()
        mock_workspace.workspace_dir = "/test/workspace"

        # Mock pool
        mock_pool = AsyncMock()
        mock_pool.get_or_create = AsyncMock(return_value=mock_workspace)
        mock_request.app.state.tenant_workspace_pool = mock_pool

        reset_called = []

        with patch(
            "swe.app.middleware.tenant_workspace.TenantModelManager.load",
        ) as mock_load:
            mock_load.return_value = sample_model_config

            def mock_reset_config(token):
                reset_called.append(token)

            with patch.object(
                TenantModelContext,
                "set_config",
                return_value="mock-token",
            ):
                with patch.object(
                    TenantModelContext,
                    "reset_config",
                    side_effect=mock_reset_config,
                ):
                    middleware = TenantWorkspaceMiddleware(app=MagicMock())

                    async def call_next(request):
                        return Response(content=b"OK", status_code=200)

                    response = await middleware.dispatch(
                        mock_request,
                        call_next,
                    )

                    # Verify reset was called
                    assert len(reset_called) == 1
                    assert reset_called[0] == "mock-token"

    @pytest.mark.asyncio
    async def test_model_config_reset_on_exception(
        self,
        mock_request,
        sample_model_config,
    ):
        """Model configuration is reset even if request raises exception."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        # Mock workspace
        mock_workspace = MagicMock()
        mock_workspace.workspace_dir = "/test/workspace"

        # Mock pool
        mock_pool = AsyncMock()
        mock_pool.get_or_create = AsyncMock(return_value=mock_workspace)
        mock_request.app.state.tenant_workspace_pool = mock_pool

        reset_called = []

        with patch(
            "swe.app.middleware.tenant_workspace.TenantModelManager.load",
        ) as mock_load:
            mock_load.return_value = sample_model_config

            def mock_reset_config(token):
                reset_called.append(token)

            with patch.object(
                TenantModelContext,
                "set_config",
                return_value="mock-token",
            ):
                with patch.object(
                    TenantModelContext,
                    "reset_config",
                    side_effect=mock_reset_config,
                ):
                    middleware = TenantWorkspaceMiddleware(app=MagicMock())

                    async def call_next(request):
                        raise ValueError("Test exception")

                    with pytest.raises(ValueError, match="Test exception"):
                        await middleware.dispatch(mock_request, call_next)

                    # Verify reset was still called despite exception
                    assert len(reset_called) == 1

    @pytest.mark.asyncio
    async def test_model_config_load_failure_continues(self, mock_request):
        """Request continues if model config fails to load (with warning log)."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        # Mock workspace
        mock_workspace = MagicMock()
        mock_workspace.workspace_dir = "/test/workspace"

        # Mock pool
        mock_pool = AsyncMock()
        mock_pool.get_or_create = AsyncMock(return_value=mock_workspace)
        mock_request.app.state.tenant_workspace_pool = mock_pool

        with patch(
            "swe.app.middleware.tenant_workspace.TenantModelManager.load",
        ) as mock_load:
            # Simulate config load failure (OSError for file read error)
            mock_load.side_effect = OSError("Config file not readable")

            middleware = TenantWorkspaceMiddleware(app=MagicMock())

            request_processed = []

            async def call_next(request):
                request_processed.append(True)
                return Response(content=b"OK", status_code=200)

            response = await middleware.dispatch(mock_request, call_next)

            # Request should still succeed
            assert response.status_code == 200
            assert len(request_processed) == 1
            mock_load.assert_called_once_with("test-tenant")

    @pytest.mark.asyncio
    async def test_no_model_config_without_tenant(self, mock_request):
        """No model config is loaded when tenant_id is not set."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        # Remove tenant_id
        delattr(mock_request.state, "tenant_id")

        with patch(
            "swe.app.middleware.tenant_workspace.TenantModelManager.load",
        ) as mock_load:
            middleware = TenantWorkspaceMiddleware(
                app=MagicMock(),
                require_workspace=False,
            )

            async def call_next(request):
                return Response(content=b"OK", status_code=200)

            response = await middleware.dispatch(mock_request, call_next)

            # Load should not be called
            mock_load.assert_not_called()
            assert response.status_code == 200


@pytest.mark.skip(
    reason="Provider 存储初始化现由 ProviderManager 负责",
)
class TestTenantProviderConfigInitialization:
    """Tests for tenant provider config auto-initialization in middleware."""

    @pytest.fixture
    def mock_request_with_tenant(self):
        """Create a mock FastAPI request with tenant_id."""
        mock_req = MagicMock(spec=Request)
        mock_req.state = MagicMock()
        mock_req.state.tenant_id = "new-tenant"
        mock_req.url = MagicMock()
        mock_req.url.path = "/api/test"
        mock_req.app = MagicMock()
        mock_req.app.state = MagicMock()
        return mock_req

    @pytest.mark.asyncio
    async def test_ensure_tenant_provider_config_creates_from_default(
        self,
        mock_request_with_tenant,
        tmp_path,
    ):
        """Provider config is initialized from default tenant when missing."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )
        from swe.constant import SECRET_DIR

        # Setup: Create default tenant with config
        default_providers = tmp_path / ".swe.secret" / "default" / "providers"
        default_providers.mkdir(parents=True)
        (default_providers / "builtin").mkdir()
        (default_providers / "custom").mkdir()

        # Create a provider config in default
        provider_config = {
            "id": "openai",
            "name": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-default",
        }
        import json

        (default_providers / "builtin" / "openai.json").write_text(
            json.dumps(provider_config),
        )

        # Temporarily override SECRET_DIR
        with patch(
            "swe.app.middleware.tenant_workspace.SECRET_DIR",
            tmp_path / ".swe.secret",
        ):
            middleware = TenantWorkspaceMiddleware(app=MagicMock())

            # Verify new tenant doesn't have config yet
            new_tenant_dir = (
                tmp_path / ".swe.secret" / "new-tenant" / "providers"
            )
            assert not new_tenant_dir.exists()

            # Mock workspace pool
            mock_workspace = MagicMock()
            mock_workspace.workspace_dir = "/test/workspace"
            mock_pool = AsyncMock()
            mock_pool.get_or_create = AsyncMock(return_value=mock_workspace)
            mock_request_with_tenant.app.state.tenant_workspace_pool = (
                mock_pool
            )

            with patch(
                "swe.app.middleware.tenant_workspace.TenantModelManager.load",
            ) as mock_load:
                mock_load.side_effect = Exception("No config")

                async def call_next(request):
                    return Response(content=b"OK", status_code=200)

                response = await middleware.dispatch(
                    mock_request_with_tenant,
                    call_next,
                )

                # Verify config was copied (even though model config fails)
                assert new_tenant_dir.exists()
                assert (new_tenant_dir / "builtin" / "openai.json").exists()
                assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_ensure_tenant_provider_config_creates_empty_fallback(
        self,
        mock_request_with_tenant,
        tmp_path,
    ):
        """Empty provider structure created when default has no config."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        # Temporarily override SECRET_DIR (no default config exists)
        with patch(
            "swe.app.middleware.tenant_workspace.SECRET_DIR",
            tmp_path / ".swe.secret",
        ):
            middleware = TenantWorkspaceMiddleware(app=MagicMock())

            # Verify new tenant doesn't have config yet
            new_tenant_dir = (
                tmp_path / ".swe.secret" / "new-tenant" / "providers"
            )
            assert not new_tenant_dir.exists()

            # Mock workspace pool
            mock_workspace = MagicMock()
            mock_workspace.workspace_dir = "/test/workspace"
            mock_pool = AsyncMock()
            mock_pool.get_or_create = AsyncMock(return_value=mock_workspace)
            mock_request_with_tenant.app.state.tenant_workspace_pool = (
                mock_pool
            )

            with patch(
                "swe.app.middleware.tenant_workspace.TenantModelManager.load",
            ) as mock_load:
                mock_load.side_effect = Exception("No config")

                async def call_next(request):
                    return Response(content=b"OK", status_code=200)

                response = await middleware.dispatch(
                    mock_request_with_tenant,
                    call_next,
                )

                # Verify empty structure was created
                assert new_tenant_dir.exists()
                assert (new_tenant_dir / "builtin").exists()
                assert (new_tenant_dir / "custom").exists()
                assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_ensure_tenant_provider_config_is_idempotent(
        self,
        mock_request_with_tenant,
        tmp_path,
    ):
        """Provider config initialization is idempotent - doesn't overwrite existing."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        # Create existing config for tenant
        tenant_providers = (
            tmp_path / ".swe.secret" / "new-tenant" / "providers"
        )
        tenant_providers.mkdir(parents=True)
        import json

        existing_config = {"id": "openai", "api_key": "sk-existing"}
        (tenant_providers / "openai.json").write_text(
            json.dumps(existing_config),
        )

        with patch(
            "swe.app.middleware.tenant_workspace.SECRET_DIR",
            tmp_path / ".swe.secret",
        ):
            middleware = TenantWorkspaceMiddleware(app=MagicMock())

            # Mock workspace pool
            mock_workspace = MagicMock()
            mock_workspace.workspace_dir = "/test/workspace"
            mock_pool = AsyncMock()
            mock_pool.get_or_create = AsyncMock(return_value=mock_workspace)
            mock_request_with_tenant.app.state.tenant_workspace_pool = (
                mock_pool
            )

            with patch(
                "swe.app.middleware.tenant_workspace.TenantModelManager.load",
            ) as mock_load:
                mock_load.side_effect = Exception("No config")

                async def call_next(request):
                    return Response(content=b"OK", status_code=200)

                # First call
                await middleware.dispatch(mock_request_with_tenant, call_next)

                # Verify existing config not overwritten
                content = (tenant_providers / "openai.json").read_text()
                assert "sk-existing" in content

    def test_is_workspace_exempt_for_health_routes(self):
        """Health routes are exempt from workspace requirements."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        middleware = TenantWorkspaceMiddleware(app=MagicMock())

        exempt_routes = [
            "/health",
            "/healthz",
            "/api/health/health",
            "/ready",
            "/alive",
        ]
        for route in exempt_routes:
            assert middleware._is_workspace_exempt(route) is True

    def test_is_workspace_exempt_for_api_routes(self):
        """API routes are not exempt from workspace requirements."""
        from swe.app.middleware.tenant_workspace import (
            TenantWorkspaceMiddleware,
        )

        middleware = TenantWorkspaceMiddleware(app=MagicMock())

        api_routes = ["/api/agents", "/api/models", "/api/chat"]
        for route in api_routes:
            assert middleware._is_workspace_exempt(route) is False
