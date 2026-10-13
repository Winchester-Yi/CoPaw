# -*- coding: utf-8 -*-
"""B query service follows the existing opt-in lifecycle."""

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,b_enabled",
    [(False, True), (True, False), (True, True)],
)
async def test_query_service_gate_and_close(monkeypatch, enabled, b_enabled):
    from monitor.app.services.token_diagnostics import lifecycle

    class Store:
        def __init__(self, *, context_enabled=False):
            self.context_enabled = context_enabled

        async def close(self):
            pass

    class Analyzer:
        def __init__(self, store):
            self.store = store

        def start(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(lifecycle, "TokenStore", Store)
    monkeypatch.setattr(lifecycle, "Analyzer", Analyzer)
    monkeypatch.setattr(
        lifecycle.EnvVarLoader,
        "get_bool",
        lambda key, default: {
            "SWE_TRACING_ENABLED": enabled,
            "SWE_TOKEN_DIAGNOSTICS_CONTEXT_ENABLED": b_enabled,
        }[key],
    )
    app = SimpleNamespace(state=SimpleNamespace())
    await lifecycle.start_services(app)
    try:
        service = getattr(app.state, "token_diagnostic_queries", None)
        assert bool(service) is (enabled and b_enabled)
        assert bool(app.state.token_queries) is enabled
        if service:
            assert service.store is app.state.token_store
    finally:
        await lifecycle.close_services(app)
    assert app.state.token_diagnostic_queries is None
