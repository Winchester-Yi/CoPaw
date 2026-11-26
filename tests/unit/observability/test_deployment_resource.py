# -*- coding: utf-8 -*-
import io
import json

import pytest

from swe.__version__ import __version__
from swe.observability.config import EmitterConfig
from swe.observability.emitter import JsonlEmitter
from swe.observability.models import MetadataEvent, ObservabilityContext, Scope
from swe.observability.outbox import sign_event, verify_event
from swe.observability.projector import _semantic_json, _validate_payload


def _run_event() -> MetadataEvent:
    context = ObservabilityContext.new_run(
        scope=Scope("service-unit", "source", "tenant", "agent"),
        capture_enabled=True,
    )
    return MetadataEvent(
        event_name="run.accepted",
        obs_context=context,
        entity_kind="run",
        entity_id=context.obs_run_id,
        phase_key="accepted",
        local_seq=0,
        attributes={
            "started_at": context.run_started_at,
            "resource": {"service_name": "spoofed"},
        },
    )


@pytest.mark.parametrize("environment", ["prd", "custom-environment", None])
def test_local_jsonl_event_uses_deployment_resource(monkeypatch, environment):
    if environment is None:
        monkeypatch.delenv("SWE_ENV", raising=False)
    else:
        monkeypatch.setenv("SWE_ENV", environment)
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "private-tag=private-value")
    event = _run_event()
    stream = io.StringIO()
    emitter = JsonlEmitter(stream=stream)
    try:
        assert emitter.emit(event)
    finally:
        emitter.close()

    expected = {"service_name": "swe", "service_version": __version__}
    if environment == "prd":
        expected["deployment_environment"] = environment
    validated = _validate_payload(stream.getvalue())
    assert validated["producer_id"] == event.producer_id
    assert validated["resource"] == expected


def test_worker_forwarding_preserves_only_bounded_resource_labels(monkeypatch):
    monkeypatch.setenv("SWE_ENV", "prd")
    worker_event = _run_event().to_dict()
    worker_event["resource"] = {
        "service_name": "swe",
        "service_version": "a" * 65,
        "deployment_environment": "dev",
        "api_key": "private-value",
    }
    old_worker_event = {
        k: v for k, v in worker_event.items() if k != "resource"
    }
    stream = io.StringIO()
    emitter = JsonlEmitter(stream=stream)
    try:
        assert emitter.emit_raw(json.dumps(worker_event).encode())
        assert emitter.emit_raw(json.dumps(old_worker_event).encode())
    finally:
        emitter.close()

    current, historical = map(
        _validate_payload,
        stream.getvalue().splitlines(),
    )
    assert current["resource"] == {
        "service_name": "swe",
        "deployment_environment": "dev",
    }
    assert "resource" not in historical
    assert _semantic_json(current) == _semantic_json(historical)


@pytest.mark.parametrize("budget", ["max_event_bytes", "max_queue_bytes"])
def test_resource_yields_to_event_byte_budget(budget):
    event = _run_event().to_dict()
    event["links"] = [
        {"relation_type": "cause", "obs_span_id": event["event_id"]},
    ]
    historical = {
        key: value for key, value in event.items() if key != "resource"
    }
    historical_size = (
        len(json.dumps(historical, separators=(",", ":")).encode()) + 1
    )
    stream = io.StringIO()
    emitter = JsonlEmitter(
        EmitterConfig(**{budget: historical_size}),
        stream=stream,
    )
    try:
        assert emitter.emit(event)
    finally:
        assert emitter.close() == 0
    assert json.loads(stream.getvalue()) == historical


def test_outbox_preserves_signed_resource_and_legacy_absence(monkeypatch):
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "active")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "a" * 32)
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_VERIFY_KEYS", "{}")
    monkeypatch.setenv("SWE_ENV", "dev")
    event = _run_event().to_dict()
    historical = {k: v for k, v in event.items() if k != "resource"}
    signed = sign_event(event)
    old_signature = sign_event(historical)

    monkeypatch.setenv("SWE_ENV", "prd")
    assert verify_event(signed)["resource"]["deployment_environment"] == "dev"
    assert verify_event(old_signature) == historical

    signed["event"]["resource"]["deployment_environment"] = "prd"
    with pytest.raises(
        ValueError,
        match="Invalid observability outbox signature",
    ):
        verify_event(signed)
