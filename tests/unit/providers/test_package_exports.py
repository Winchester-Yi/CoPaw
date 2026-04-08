# -*- coding: utf-8 -*-
"""Regression tests for providers package exports."""


def test_provider_manager_lazy_export_resolves_class():
    """ProviderManager package export should resolve to the concrete class."""
    from swe.providers import ProviderManager

    assert ProviderManager is not None
    assert ProviderManager.__name__ == "ProviderManager"
