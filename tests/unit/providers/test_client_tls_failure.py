# -*- coding: utf-8 -*-
"""A failed preparation is request owned and does not retain traceback frames."""

import pytest


def test_failure_preserves_error_details_and_resets_binding():
    from swe.providers.client_tls import (
        PreparedTLSFailure,
        bind_prepared_client_tls,
        prepared_http_client_kwargs,
    )

    error = FileNotFoundError(2, "missing", "tenant-ca.pem")
    with bind_prepared_client_tls(PreparedTLSFailure(error)):
        for _ in range(2):
            with pytest.raises(FileNotFoundError) as caught:
                prepared_http_client_kwargs("openai")
            assert caught.value.filename == "tenant-ca.pem"
            assert caught.value is not error
            assert error.__traceback__ is None
        assert prepared_http_client_kwargs("anthropic") == {}
    assert prepared_http_client_kwargs("openai") == {}
