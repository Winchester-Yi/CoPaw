# -*- coding: utf-8 -*-
"""The B contract is additive, bounded, and contains no original content."""

from dataclasses import replace
from importlib import import_module
from typing import get_args, get_type_hints

import pytest

from swe.token_diagnostics.config import DiagnosticConfig


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("context_enabled", [False, True])
def test_b_schema_requires_both_enablement_flags(enabled, context_enabled):
    from swe.token_diagnostics.contract import (
        SCHEMA_VERSION,
        context_capture_enabled,
        schema_version,
    )

    config = replace(
        DiagnosticConfig(),
        enabled=enabled,
        context_enabled=context_enabled,
    )
    assert SCHEMA_VERSION == "1.0"
    assert context_capture_enabled(config) is (enabled and context_enabled)
    assert schema_version(config) == (
        "1.1" if enabled and context_enabled else "1.0"
    )


def test_b_is_disabled_by_default(monkeypatch):
    from swe.token_diagnostics.contract import schema_version

    monkeypatch.delenv("SWE_TOKEN_DIAGNOSTICS_CONTEXT_ENABLED", raising=False)
    monkeypatch.setattr("swe.constant.TRACING_ENABLED", True)
    config = DiagnosticConfig.from_env()
    assert config.context_enabled is False
    assert schema_version(config) == "1.0"


@pytest.mark.parametrize("setting", ["true", "TRUE", "1", "yes", "on"])
def test_b_context_environment_switch(setting, monkeypatch):
    from swe.token_diagnostics.contract import schema_version

    monkeypatch.setenv("SWE_TOKEN_DIAGNOSTICS_CONTEXT_ENABLED", setting)
    monkeypatch.setattr("swe.constant.TRACING_ENABLED", True)
    assert schema_version(DiagnosticConfig.from_env()) == "1.1"
    monkeypatch.setattr("swe.constant.TRACING_ENABLED", False)
    assert schema_version(DiagnosticConfig.from_env()) == "1.0"


@pytest.mark.parametrize("setting", ["false", "0", "", "-1", "INVALID"])
def test_invalid_context_switch_does_not_enable_b(setting, monkeypatch):
    monkeypatch.setenv("SWE_TOKEN_DIAGNOSTICS_CONTEXT_ENABLED", setting)
    assert DiagnosticConfig.from_env().context_enabled is False


@pytest.mark.parametrize(
    "module_name",
    [
        "swe.token_diagnostics.contract",
        "monitor.app.services.token_diagnostics.diagnostic_contract",
    ],
)
def test_event_version_allowlist_preserves_a_and_accepts_b(module_name):
    module = import_module(module_name)
    assert module.SUPPORTED_SCHEMA_VERSIONS == frozenset({"1.0", "1.1"})
    for version in ("1.0", "1.1"):
        assert module.validate_schema_version(version) == version
    for version in (None, "", "0.9", "1.2", "2.0", 1.1, [], {}):
        with pytest.raises(ValueError, match="schema version"):
            module.validate_schema_version(version)


def test_capture_states_are_controlled():
    from swe.token_diagnostics.context_contract import (
        CAPTURE_STATES,
        CaptureState,
        validate_capture_state,
    )

    assert CAPTURE_STATES == frozenset({"complete", "partial", "unavailable"})
    assert set(get_args(CaptureState)) == CAPTURE_STATES
    for state in CAPTURE_STATES:
        assert validate_capture_state(state) == state
    for state in ("success", "pending", "not_hit", None, [], {}):
        with pytest.raises(ValueError, match="capture state"):
            validate_capture_state(state)


def test_evaluation_states_are_controlled():
    from monitor.app.services.token_diagnostics.diagnostic_contract import (
        EVALUATION_STATES,
        EvaluationState,
        validate_evaluation_state,
    )

    assert EVALUATION_STATES == frozenset(
        {"candidate", "not_hit", "insufficient_evidence", "not_applicable"},
    )
    assert set(get_args(EvaluationState)) == EVALUATION_STATES
    for state in EVALUATION_STATES:
        assert validate_evaluation_state(state) == state
    for state in ("pending", "failed", "active", None, [], {}):
        with pytest.raises(ValueError, match="evaluation state"):
            validate_evaluation_state(state)


def test_b_budgets_are_fixed_and_item_limit_cannot_expand_them():
    from swe.token_diagnostics import context_contract as contract

    assert contract.B_SCHEMA_VERSION == "1.1"
    assert contract.REPRESENTATION_VERSION == "client-ordered-text-v1"
    assert contract.PREFIX_BLOCK_BYTES == 256
    assert contract.CONTEXT_MAX_BYTES == 256 * 1024
    assert contract.CONTEXT_MAX_BLOCKS == 1024
    assert contract.CONTEXT_CAPTURE_SECONDS == 0.005
    assert contract.CONTEXT_MAX_ITEMS == 2000
    config = replace(DiagnosticConfig(), context_enabled=True)
    for limit in (-1, 0, "INVALID", "999999999", 10**20, None, True):
        bounded = contract.context_item_limit(replace(config, max_items=limit))
        assert 1 <= bounded <= 2000
    assert contract.context_item_limit(replace(config, max_items=10)) == 10


def test_b_environment_cannot_expand_approved_capture_budgets(monkeypatch):
    from swe.token_diagnostics import context_contract as contract

    for name in (
        "CONTEXT_MAX_BYTES",
        "CONTEXT_MAX_BLOCKS",
        "CONTEXT_CAPTURE_SECONDS",
        "CONTEXT_MAX_ITEMS",
    ):
        monkeypatch.setenv("SWE_TOKEN_DIAGNOSTICS_" + name, "999999999")
    config = replace(DiagnosticConfig.from_env(), context_enabled=True)
    assert contract.context_item_limit(config) == 2000
    assert contract.CONTEXT_MAX_BYTES == 256 * 1024
    assert contract.CONTEXT_MAX_BLOCKS == 1024
    assert contract.CONTEXT_CAPTURE_SECONDS == 0.005
    assert config.max_record_bytes == 512 * 1024
    assert config.max_records == 1000
    assert config.max_bytes == 16 * 1024 * 1024
    assert config.retention_days == 30


def test_rule_ids_do_not_replace_legacy_rules():
    from monitor.app.services.token_diagnostics import diagnostic_contract
    from monitor.app.services.token_diagnostics.rules import RULE_IDS

    assert len(diagnostic_contract.RULE_IDS) == 8
    assert len(set(diagnostic_contract.RULE_IDS)) == 8
    assert set(diagnostic_contract.RULE_IDS).isdisjoint(RULE_IDS)
    assert diagnostic_contract.CANDIDATE_LIMIT == 100
    assert diagnostic_contract.HISTORY_WINDOW_HOURS == 24
    assert diagnostic_contract.RULE_VERSION == 1
    assert diagnostic_contract.THRESHOLD_VERSION == 1


def test_context_dtos_freeze_intervals_and_nullable_source_identity():
    from swe.token_diagnostics.context_contract import (
        CapturedContext,
        ContextOccurrence,
        PrefixBlock,
    )

    assert CapturedContext.__required_keys__ == {
        "context_id",
        "context_role",
        "representation_version",
        "estimator_version",
        "key_version",
        "capture_state",
        "capture_reason",
        "text_bytes",
        "encoded_bytes",
        "expected_item_count",
        "occurrences",
        "blocks",
    }
    occurrence = get_type_hints(ContextOccurrence)
    for field in ("source_object_id", "source_version"):
        assert type(None) in get_args(occurrence[field])
    assert {
        "occurrence_index",
        "category",
        "attribution",
        "content_digest",
        "structure_digest",
        "text_start",
        "text_end",
        "encoded_start",
        "encoded_end",
        "token_start",
        "token_end",
    } <= occurrence.keys()
    assert PrefixBlock.__required_keys__ == {
        "block_index",
        "chained_digest",
        "encoded_start",
        "encoded_end",
        "text_start",
        "text_end",
    }
    forbidden = {"raw_text", "prompt", "arguments", "result"}
    for dto in (CapturedContext, ContextOccurrence, PrefixBlock):
        assert forbidden.isdisjoint(dto.__annotations__)


def test_evaluation_dtos_use_explicit_typed_scalar_values():
    from monitor.app.services.token_diagnostics.diagnostic_contract import (
        Evaluation,
        EvaluationValue,
        PrefixBounds,
    )

    assert PrefixBounds.__required_keys__ == {
        "lower_text_bytes",
        "upper_text_bytes",
        "first_difference_index",
    }
    assert Evaluation.__required_keys__ == {
        "evaluation_id",
        "object_type",
        "object_id",
        "rule_id",
        "rule_version",
        "threshold_version",
        "input_revision",
        "snapshot_id",
        "state",
        "values",
        "gaps",
        "refs",
        "findings",
    }
    hints = get_type_hints(Evaluation)
    assert hints["values"] == list[EvaluationValue]
    value_hints = get_type_hints(EvaluationValue)
    assert value_hints["integer_value"] == int | None
    assert value_hints["decimal_value"] == float | None
    enum_type = next(
        kind
        for kind in get_args(value_hints["enum_value"])
        if kind is not type(None)
    )
    assert get_args(enum_type)
    forbidden = {"raw_text", "prompt", "arguments", "result"}
    for dto in (PrefixBounds, Evaluation, EvaluationValue):
        assert forbidden.isdisjoint(dto.__annotations__)
