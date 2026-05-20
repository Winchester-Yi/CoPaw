# -*- coding: utf-8 -*-
"""Unit tests for tenant identity middleware.

Tests valid/missing/invalid tenant ID handling, exempt endpoints,
and context binding behavior.
"""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# pylint: disable=protected-access

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

_CONTEXT_FILE = (
    Path(__file__).parent.parent.parent.parent
    / "src"
    / "swe"
    / "config"
    / "context.py"
)
_ORIGINAL_CONTEXT_MODULE = sys.modules.get("swe.config.context")
_context_spec = importlib.util.spec_from_file_location(
    "swe.config.context",
    _CONTEXT_FILE,
)
assert _context_spec is not None and _context_spec.loader is not None
context_module = importlib.util.module_from_spec(_context_spec)
sys.modules["swe.config.context"] = context_module
_context_spec.loader.exec_module(context_module)

_MIDDLEWARE_FILE = (
    Path(__file__).parent.parent.parent.parent
    / "src"
    / "swe"
    / "app"
    / "middleware"
    / "tenant_identity.py"
)
_PACKAGE_PATH = str(_MIDDLEWARE_FILE.parent)
if "swe.app.middleware" not in sys.modules:
    middleware_pkg = types.ModuleType("swe.app.middleware")
    middleware_pkg.__path__ = [_PACKAGE_PATH]
    sys.modules["swe.app.middleware"] = middleware_pkg

_middleware_spec = importlib.util.spec_from_file_location(
    "swe.app.middleware.tenant_identity",
    _MIDDLEWARE_FILE,
)
assert _middleware_spec is not None and _middleware_spec.loader is not None
tenant_identity = importlib.util.module_from_spec(_middleware_spec)
sys.modules["swe.app.middleware.tenant_identity"] = tenant_identity
_middleware_spec.loader.exec_module(tenant_identity)

if _ORIGINAL_CONTEXT_MODULE is None:
    sys.modules.pop("swe.config.context", None)
else:
    sys.modules["swe.config.context"] = _ORIGINAL_CONTEXT_MODULE


def build_test_app():
    app = FastAPI()
    app.add_middleware(
        tenant_identity.TenantIdentityMiddleware,
        require_tenant=True,
        default_tenant_id=None,
    )

    @app.get("/api/settings")
    def stateful_route():
        return {"ok": True}

    @app.get("/api/version")
    def exempt_route():
        return {"version": "test"}

    @app.get("/static/{user_id}/{filename}")
    def static_route(user_id: str, filename: str):
        return {"user_id": user_id, "filename": filename}

    return app


def test_missing_tenant_header_returns_400_for_stateful_route():
    client = TestClient(build_test_app(), raise_server_exceptions=False)
    response = client.get("/api/settings")
    assert response.status_code == 400
    assert response.json()["detail"] == "X-Tenant-Id header is required"


def test_missing_source_header_returns_400_for_stateful_route():
    client = TestClient(build_test_app(), raise_server_exceptions=False)
    response = client.get(
        "/api/settings",
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "X-Source-Id header is required"


def test_exempt_route_still_works_without_tenant_header():
    client = TestClient(build_test_app(), raise_server_exceptions=False)
    response = client.get("/api/version")
    assert response.status_code == 200


def test_static_route_works_without_tenant_header():
    client = TestClient(build_test_app(), raise_server_exceptions=False)
    response = client.get("/static/alice/demo.txt")
    assert response.status_code == 200
    assert response.json() == {"user_id": "alice", "filename": "demo.txt"}


def test_public_text_asset_api_routes_are_exempt() -> None:
    assert tenant_identity.is_tenant_exempt("/api/assets/text/read") is True
    assert tenant_identity.is_source_exempt("/api/assets/text/write") is True


def test_invalid_tenant_id_returns_400():
    client = TestClient(build_test_app(), raise_server_exceptions=False)
    response = client.get(
        "/api/settings",
        headers={
            "X-Tenant-Id": "../bad",
            "X-Source-Id": "source-a",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid X-Tenant-Id format"


def test_invalid_source_id_returns_400():
    client = TestClient(build_test_app(), raise_server_exceptions=False)
    response = client.get(
        "/api/settings",
        headers={
            "X-Tenant-Id": "tenant-a",
            "X-Source-Id": "../bad",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid X-Source-Id format"


class TestTenantIdentityExemptions:
    """Tests for tenant identity exempt routes."""

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_health_routes_exempt(self):
        """Health check routes are exempt from tenant requirements."""
        from swe.app.middleware.tenant_identity import is_tenant_exempt

        assert is_tenant_exempt("/health") is True
        assert is_tenant_exempt("/healthz") is True
        assert is_tenant_exempt("/api/health/health") is True
        assert is_tenant_exempt("/ready") is True
        assert is_tenant_exempt("/readyz") is True
        assert is_tenant_exempt("/alive") is True

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_version_route_exempt(self):
        """Version endpoint is exempt from tenant requirements."""
        from swe.app.middleware.tenant_identity import is_tenant_exempt

        assert is_tenant_exempt("/api/version") is True

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_docs_routes_exempt(self):
        """Documentation routes are exempt from tenant requirements."""
        from swe.app.middleware.tenant_identity import is_tenant_exempt

        assert is_tenant_exempt("/docs") is True
        assert is_tenant_exempt("/redoc") is True
        assert is_tenant_exempt("/openapi.json") is True

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_auth_routes_exempt(self):
        """Auth routes are exempt from tenant requirements."""
        from swe.app.middleware.tenant_identity import is_tenant_exempt

        assert is_tenant_exempt("/api/auth/login") is True
        assert is_tenant_exempt("/api/auth/register") is True

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_static_assets_exempt(self):
        """Static asset routes are exempt from tenant requirements."""
        from swe.app.middleware.tenant_identity import is_tenant_exempt

        assert is_tenant_exempt("/logo.png") is True
        assert is_tenant_exempt("/dark-logo.png") is True
        assert is_tenant_exempt("/assets/main.js") is True
        assert is_tenant_exempt("/assets/style.css") is True

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_stateful_routes_not_exempt(self):
        """Stateful API routes are not exempt from tenant requirements."""
        from swe.app.middleware.tenant_identity import is_tenant_exempt

        assert is_tenant_exempt("/api/settings") is False
        assert is_tenant_exempt("/api/agents") is False
        assert is_tenant_exempt("/api/console/chat") is False
        assert is_tenant_exempt("/api/workspace/files") is False


class TestTenantIdValidation:
    """Tests for tenant ID validation."""

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_valid_tenant_ids(self):
        """Valid tenant IDs pass validation."""
        from swe.app.middleware.tenant_identity import TenantIdentityMiddleware

        middleware = TenantIdentityMiddleware(app=Mock())

        assert middleware._is_valid_tenant_id("tenant-1") is True
        assert middleware._is_valid_tenant_id("tenant_1") is True
        assert middleware._is_valid_tenant_id("tenant.1") is True
        assert middleware._is_valid_tenant_id("Tenant1") is True
        assert middleware._is_valid_tenant_id("123-abc") is True
        assert middleware._is_valid_tenant_id("default") is True
        assert middleware._is_valid_tenant_id("a" * 50) is True

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_invalid_tenant_ids_path_traversal(self):
        """Tenant IDs with path traversal are rejected."""
        from swe.app.middleware.tenant_identity import TenantIdentityMiddleware

        middleware = TenantIdentityMiddleware(app=Mock())

        assert middleware._is_valid_tenant_id("../etc") is False
        assert middleware._is_valid_tenant_id("tenant/../other") is False
        assert middleware._is_valid_tenant_id("tenant/sub") is False

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_invalid_tenant_ids_empty_or_too_long(self):
        """Empty or too long tenant IDs are rejected."""
        from swe.app.middleware.tenant_identity import TenantIdentityMiddleware

        middleware = TenantIdentityMiddleware(app=Mock())

        assert middleware._is_valid_tenant_id("") is False
        assert middleware._is_valid_tenant_id("a" * 257) is False

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_invalid_tenant_ids_control_chars(self):
        """Tenant IDs with control characters are rejected."""
        from swe.app.middleware.tenant_identity import TenantIdentityMiddleware

        middleware = TenantIdentityMiddleware(app=Mock())

        assert middleware._is_valid_tenant_id("tenant\x00") is False
        assert middleware._is_valid_tenant_id("tenant\n") is False
        assert middleware._is_valid_tenant_id("tenant\t") is False


class TestTenantContextHelpers:
    """Tests for tenant context helper functions."""

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_get_tenant_id_from_request(self):
        """get_tenant_id_from_request extracts tenant ID from state."""
        from swe.app.middleware.tenant_identity import (
            get_tenant_id_from_request,
        )

        mock_request = Mock()
        mock_request.state = Mock()
        mock_request.state.tenant_id = "tenant-1"

        result = get_tenant_id_from_request(mock_request)
        assert result == "tenant-1"

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_get_tenant_id_returns_none_when_not_set(self):
        """get_tenant_id_from_request returns None when not set."""
        from swe.app.middleware.tenant_identity import (
            get_tenant_id_from_request,
        )

        mock_request = Mock()
        mock_request.state = Mock()
        mock_request.state.tenant_id = None

        result = get_tenant_id_from_request(mock_request)
        assert result is None

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_get_user_id_from_request(self):
        """get_user_id_from_request extracts user ID from state."""
        from swe.app.middleware.tenant_identity import get_user_id_from_request

        mock_request = Mock()
        mock_request.state = Mock()
        mock_request.state.user_id = "user-1"

        result = get_user_id_from_request(mock_request)
        assert result == "user-1"

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_require_tenant_id_raises_when_missing(self):
        """require_tenant_id raises HTTPException when missing."""
        from swe.app.middleware.tenant_identity import require_tenant_id
        from fastapi import HTTPException

        mock_request = Mock()
        mock_request.state = Mock()
        mock_request.state.tenant_id = None

        with pytest.raises(HTTPException) as exc_info:
            require_tenant_id(mock_request)
        assert exc_info.value.status_code == 400
        assert "Tenant context not available" in exc_info.value.detail

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_require_tenant_id_returns_value_when_set(self):
        """require_tenant_id returns tenant ID when set."""
        from swe.app.middleware.tenant_identity import require_tenant_id

        mock_request = Mock()
        mock_request.state = Mock()
        mock_request.state.tenant_id = "tenant-1"

        result = require_tenant_id(mock_request)
        assert result == "tenant-1"

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_require_user_id_raises_when_missing(self):
        """require_user_id raises HTTPException when missing."""
        from swe.app.middleware.tenant_identity import require_user_id
        from fastapi import HTTPException

        mock_request = Mock()
        mock_request.state = Mock()
        mock_request.state.user_id = None

        with pytest.raises(HTTPException) as exc_info:
            require_user_id(mock_request)
        assert exc_info.value.status_code == 400

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_require_user_id_returns_value_when_set(self):
        """require_user_id returns user ID when set."""
        from swe.app.middleware.tenant_identity import require_user_id

        mock_request = Mock()
        mock_request.state = Mock()
        mock_request.state.user_id = "user-1"

        result = require_user_id(mock_request)
        assert result == "user-1"


class TestTenantExemptRoutes:
    """Tests for TENANT_EXEMPT_ROUTES constant."""

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_exempt_routes_is_frozenset(self):
        """TENANT_EXEMPT_ROUTES is a frozenset for immutability."""
        from swe.app.middleware.tenant_identity import TENANT_EXEMPT_ROUTES

        assert isinstance(TENANT_EXEMPT_ROUTES, frozenset)

    @pytest.mark.skip(reason="Requires full app dependencies")
    def test_common_routes_exempt(self):
        """Common system routes are in exempt set."""
        from swe.app.middleware.tenant_identity import TENANT_EXEMPT_ROUTES

        assert "/health" in TENANT_EXEMPT_ROUTES
        assert "/api/version" in TENANT_EXEMPT_ROUTES
        assert "/api/auth/login" in TENANT_EXEMPT_ROUTES
