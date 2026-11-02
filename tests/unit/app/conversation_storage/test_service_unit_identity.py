# -*- coding: utf-8 -*-
import pytest

from swe.app.conversation_storage import identity


def test_service_is_required_and_exact_in_full_scope():
    with pytest.raises(TypeError):
        identity.WorkspaceScope("t", "s", "a")  # pylint: disable=missing-kwoa
    scope = identity.WorkspaceScope(
        "t",
        "s",
        "a",
        service_unit_id="LQ13.03@a_prd",
    )
    assert scope.values == ("LQ13.03@a_prd", "t", "s", "a")
    assert scope.sql_values == (b"LQ13.03@a_prd", b"t", b"s", b"a")
    assert identity.WorkspaceScope.from_values(scope.values) == scope
    assert identity.WorkspaceScope.from_row(scope.fields) == scope
    assert scope != identity.WorkspaceScope(
        "t",
        "s",
        "a",
        service_unit_id="lq13.03@a_prd",
    )
    with pytest.raises(ValueError):
        identity.WorkspaceScope.from_values(["t", "s", "a"])


@pytest.mark.parametrize(
    "value",
    ["", " ", " a", "a ", "a\n", "a\x00b", "x" * 257],
)
def test_service_rejects_invalid_deployment_identity(value):
    with pytest.raises(ValueError):
        identity.service_unit_bytes(value)


def test_service_environment_has_no_fallback(monkeypatch):
    monkeypatch.delenv("CMB_CAAS_SERVICEUNITID", raising=False)
    with pytest.raises(ValueError):
        identity.service_unit_from_env()
    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "LQ13.03@a_prd")
    assert identity.service_unit_from_env() == "LQ13.03@a_prd"
