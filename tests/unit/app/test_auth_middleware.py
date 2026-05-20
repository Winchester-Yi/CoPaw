# -*- coding: utf-8 -*-
"""Regression tests for auth middleware public-route exemptions."""

from unittest.mock import MagicMock

from swe.app.auth import AuthMiddleware


def test_public_text_asset_api_routes_skip_auth() -> None:
    request = MagicMock()
    request.method = "GET"
    request.url.path = "/api/assets/text/read"
    request.client = MagicMock(host="203.0.113.10")

    original_enabled = AuthMiddleware._should_skip_auth.__globals__[
        "is_auth_enabled"
    ]
    original_registered = AuthMiddleware._should_skip_auth.__globals__[
        "has_registered_users"
    ]
    try:
        AuthMiddleware._should_skip_auth.__globals__["is_auth_enabled"] = (
            lambda: True
        )
        AuthMiddleware._should_skip_auth.__globals__[
            "has_registered_users"
        ] = lambda: True

        assert AuthMiddleware._should_skip_auth(request) is True
    finally:
        AuthMiddleware._should_skip_auth.__globals__["is_auth_enabled"] = (
            original_enabled
        )
        AuthMiddleware._should_skip_auth.__globals__[
            "has_registered_users"
        ] = original_registered
