import asyncio
import json

import httpx
import pytest

from swe.jev.client import JevClient, logger
from swe.jev.config import JevConfig


@pytest.mark.parametrize("model", ["jev-deployed-model", ""])
@pytest.mark.parametrize("operation", ["relevance", "choose_model"])
async def test_request_sends_configured_service_model(
    monkeypatch, model, operation
):
    def respond(request):
        body = json.loads(request.content)
        if model:
            assert body["model"] == "jev-deployed-model"
        else:
            assert "model" not in body
        assert body["state"] == "task"
        if operation == "relevance":
            answers = {"a": {"type": "noul", "noul": 0.9}}
        else:
            assert body["questions"]["model"]["criteria"] == {"a": "Candidate"}
            answers = {"model": {"type": "choice", "choice": "a"}}
        return httpx.Response(200, json={"answers": answers})

    install_transport(monkeypatch, respond)
    client = JevClient(
        JevConfig(
            endpoint="https://jev.test/decide",
            timeout_seconds=1,
            model=model,
        ),
    )
    result = await getattr(client, operation)("task", {"a": "Candidate"})
    assert result == ({"a": 0.9} if operation == "relevance" else "a")


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )


def config():
    return JevConfig(endpoint="https://jev.test/decide", timeout_seconds=1)


async def test_independent_relevance_and_wire_protocol(monkeypatch):
    def respond(request):
        body = json.loads(request.content)
        assert body["state"] == "fix login"
        assert set(body["questions"]) == {"a", "b"}
        assert all(q["type"] == "noul" for q in body["questions"].values())
        assert "database cleanup" in body["questions"]["b"]["instructions"]
        return httpx.Response(
            200,
            json={
                "model": "jev",
                "usage": {"input_tokens": 12, "output_tokens": 0},
                "answers": {
                    "a": {"type": "noul", "noul": 0.9},
                    "b": {"type": "noul", "noul": 0.85},
                },
            },
        )

    install_transport(monkeypatch, respond)
    result = await JevClient(config()).relevance(
        "fix login",
        {"a": "auth", "b": "database cleanup"},
    )
    assert result == {"a": 0.9, "b": 0.85}


async def test_choice_uses_answer_not_service_model(monkeypatch):
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "model": "jev-service",
                "answers": {
                    "model": {
                        "type": "choice",
                        "choice": "cheap",
                        "confidence": 0.9,
                    },
                },
            },
        ),
    )
    assert (
        await JevClient(config()).choose_model(
            "summarize",
            {"cheap": "Small", "large": "Large"},
        )
        == "cheap"
    )


@pytest.mark.parametrize(
    "answers",
    [
        {},
        {"a": {"type": "noul", "noul": "0.9"}},
        {"a": {"type": "noul", "noul": 2}},
        {"a": {"type": "noul", "noul": True}},
        {"a": {"type": "choice", "noul": 0.9}},
        {
            "a": {"type": "noul", "noul": 0.9},
            "unknown": {"type": "noul", "noul": 0.8},
        },
    ],
)
async def test_malformed_scores_fail_open(monkeypatch, answers):
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"answers": answers},
        ),
    )
    assert await JevClient(config()).relevance("task", {"a": "text"}) is None


async def test_unknown_model_falls_back(monkeypatch):
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "answers": {"model": {"type": "choice", "choice": "foreign"}},
            },
        ),
    )
    assert (
        await JevClient(config()).choose_model("task", {"local": "Local"})
        is None
    )


async def test_timeout_falls_back_and_cancellation_propagates(monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("secret payload", request=request)

    install_transport(monkeypatch, timeout)
    assert await JevClient(config()).relevance("task", {"a": "text"}) is None
    monkeypatch.undo()

    async def cancel(request):
        raise asyncio.CancelledError()

    install_transport(monkeypatch, cancel)
    with pytest.raises(asyncio.CancelledError):
        await JevClient(config()).relevance("task", {"a": "text"})


async def test_empty_candidates_skip_http(monkeypatch):
    def unexpected(request):
        raise AssertionError("No HTTP expected")

    install_transport(monkeypatch, unexpected)
    assert await JevClient(config()).relevance("task", {}) == {}


@pytest.mark.parametrize(
    "status, body",
    [
        (422, '{"detail":"缺少必填字段"}'),
        (500, "Internal Server Error\nfailed"),
    ],
)
async def test_http_error_logs_status_and_body(
    monkeypatch, caplog, status, body
):
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(status, text=body),
    )
    assert await JevClient(config()).relevance("task", {"a": "text"}) is None
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "swe.jev.client"
    ]
    assert len(messages) == 1
    assert "reason=HTTPStatusError" in messages[0]
    assert f"status_code={status}" in messages[0]
    assert repr(body) in messages[0]
    assert "response_truncated=False" in messages[0]
    assert "\n" not in messages[0]


async def test_http_error_log_truncates_large_body(monkeypatch, caplog):
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(502, text="x" * 2000 + "OMITTED_TAIL"),
    )
    assert await JevClient(config()).relevance("task", {"a": "text"}) is None
    message = next(
        record.getMessage()
        for record in caplog.records
        if record.name == "swe.jev.client"
    )
    assert "status_code=502" in message
    assert "x" * 2000 in message
    assert "OMITTED_TAIL" not in message
    assert "response_truncated=True" in message
