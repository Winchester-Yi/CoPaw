# -*- coding: utf-8 -*-
import pytest

from swe.observability.outbox import sign_event, verify_event


def test_outbox_authenticates_event_and_requires_configured_key(monkeypatch):
    monkeypatch.delenv("SWE_AGENTOBS_OUTBOX_KEY_ID", raising=False)
    monkeypatch.delenv("SWE_AGENTOBS_OUTBOX_SECRET", raising=False)
    with pytest.raises(ValueError):
        sign_event({"event_id": "one"})
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "active")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "a" * 32)
    record = sign_event({"event_id": "one"})
    assert verify_event(record) == {"event_id": "one"}
    record["event"]["event_id"] = "two"
    with pytest.raises(ValueError):
        verify_event(record)


def test_outbox_accepts_retained_key_after_rotation(monkeypatch):
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "old")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "a" * 32)
    record = sign_event({"event_id": "one"})
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "new")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "b" * 32)
    monkeypatch.setenv(
        "SWE_AGENTOBS_OUTBOX_VERIFY_KEYS",
        '{"old":"' + "a" * 32 + '"}',
    )
    assert verify_event(record)["event_id"] == "one"
