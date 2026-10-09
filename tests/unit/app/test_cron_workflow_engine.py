"""Workflow runs call a configured HTTP endpoint without AgentRunner."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from swe.app.crons.workflow.engine import WorkflowCallError, WorkflowEngine
from swe.app.crons.workflow.models import WorkflowConfig


def _config(**overrides) -> WorkflowConfig:
    payload = {
        "binding_id": "binding-1",
        "version": 2,
        "skill_id": "skill-a",
        "url": "https://workflow.example/run",
        "method": "POST",
        "headers": {"X-Sap": {"source": "env", "key": "sapId"}},
        "body": {
            "inputParams": {
                "sapId": {"source": "env", "key": "sapId"},
                "branch": {"source": "env", "key": "bbkOrgId"},
                "kind": {"source": "literal", "value": "daily"},
            },
        },
        "success_rule": {"path": ["code"], "equals": "OK"},
        "result_fields": {"result": ["data", "result"]},
        "renderer_key": "direct",
        "provider_id": "provider-a",
        "model_id": "model-a",
    }
    payload.update(overrides)
    return WorkflowConfig.model_validate(payload)


@pytest.mark.asyncio
async def test_workflow_engine_maps_request_and_extracts_result():
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"code": "OK", "data": {"result": "完成"}}
        )

    transport = httpx.MockTransport(respond)
    engine = WorkflowEngine(
        allowed_hosts={"workflow.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
        ),
    )

    outcome = await engine.execute(
        _config(),
        env={"sapId": "user-1", "bbkOrgId": "100"},
        target_user_id="user-1",
    )

    assert len(requests) == 1
    assert requests[0].headers["X-Sap"] == "user-1"
    assert requests[0].content == (
        b'{"inputParams":{"sapId":"user-1","branch":"100","kind":"daily"}}'
    )
    assert outcome.display_text == "完成"
    assert outcome.selected_result == {"result": "完成"}
    assert "user-1" not in str(outcome.input_snapshot)


@pytest.mark.asyncio
async def test_workflow_engine_rejects_identity_mismatch_before_http():
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"code": "OK", "data": {"result": "x"}}
        )

    engine = WorkflowEngine(
        allowed_hosts={"workflow.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            timeout=timeout,
        ),
    )

    with pytest.raises(ValueError, match="sapId"):
        await engine.execute(
            _config(),
            env={"sapId": "another-user", "bbkOrgId": "100"},
            target_user_id="user-1",
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_workflow_engine_treats_business_failure_as_one_failed_attempt():
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"code": "NO", "data": {"result": "x"}}
        )

    engine = WorkflowEngine(
        allowed_hosts={"workflow.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            timeout=timeout,
        ),
    )

    with pytest.raises(WorkflowCallError, match="技能任务执行失败") as error:
        await engine.execute(
            _config(),
            env={"sapId": "user-1", "bbkOrgId": "100"},
            target_user_id="user-1",
        )
    assert calls == 1
    assert error.value.error_code == "business_failure"


@pytest.mark.asyncio
async def test_workflow_engine_uses_registered_renderer():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"code": "OK", "data": {"result": {"score": 7}}},
        ),
    )
    engine = WorkflowEngine(
        allowed_hosts={"workflow.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
        ),
        renderers={
            "score": lambda fields: f"得分 {fields['result']['score']}"
        },
    )

    outcome = await engine.execute(
        _config(renderer_key="score"),
        env={"sapId": "user-1", "bbkOrgId": "100"},
        target_user_id="user-1",
    )

    assert outcome.display_text == "得分 7"


@pytest.mark.asyncio
async def test_workflow_http_failure_does_not_expose_endpoint_in_error():
    engine = WorkflowEngine(
        allowed_hosts={"workflow.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(503, json={"error": "down"}),
            ),
            timeout=timeout,
        ),
    )

    with pytest.raises(WorkflowCallError, match="技能任务执行失败") as error:
        await engine.execute(
            _config(),
            env={"sapId": "user-1", "bbkOrgId": "100"},
            target_user_id="user-1",
        )

    assert "workflow.example" not in str(error.value)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_workflow_engine_rejects_unapproved_endpoint_before_http():
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": "OK"})

    engine = WorkflowEngine(
        allowed_hosts={"another.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            timeout=timeout,
        ),
    )

    with pytest.raises(ValueError, match="approved"):
        await engine.execute(
            _config(),
            env={"sapId": "user-1", "bbkOrgId": "100"},
            target_user_id="user-1",
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_workflow_engine_uses_target_secret_with_header_prefix():
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(
            200, json={"code": "OK", "data": {"result": "done"}}
        )

    engine = WorkflowEngine(
        allowed_hosts={"workflow.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            timeout=timeout,
        ),
    )

    outcome = await engine.execute(
        _config(
            headers={
                "Authorization": {
                    "source": "secret",
                    "key": "auth_token",
                    "prefix": "Bearer ",
                },
            },
        ),
        env={"sapId": "user-1", "bbkOrgId": "100"},
        secrets={"auth_token": "sensitive-token"},
        target_user_id="user-1",
    )

    assert seen == ["Bearer sensitive-token"]
    assert "sensitive-token" not in str(outcome.input_snapshot)


def test_workflow_config_rejects_literal_authorization_header():
    with pytest.raises(ValidationError, match="credential"):
        _config(
            headers={
                "Authorization": {
                    "source": "literal",
                    "value": "Bearer secret",
                },
            },
        )


@pytest.mark.asyncio
async def test_workflow_engine_accepts_literal_api_key_header():
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"code": "OK", "data": {"result": "完成"}}
        )

    engine = WorkflowEngine(
        allowed_hosts={"workflow.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            timeout=timeout,
        ),
    )
    config = _config(
        headers={"API-Key": {"source": "literal", "value": "test-key"}},
    )

    outcome = await engine.execute(
        config,
        env={"sapId": "user-1", "bbkOrgId": "100"},
        target_user_id="user-1",
    )

    assert seen[0].headers["API-Key"] == "test-key"
    assert outcome.display_text == "完成"
    assert "test-key" not in str(outcome.input_snapshot)


def test_workflow_config_rejects_unknown_contract_fields():
    with pytest.raises(ValidationError, match="success_rul"):
        _config(success_rul={"path": ["code"], "equals": "OK"})


@pytest.mark.asyncio
async def test_workflow_engine_maps_path_parameter_and_json_array_body():
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"code": "OK", "data": {"result": "ok"}}
        )

    engine = WorkflowEngine(
        allowed_hosts={"workflow.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            timeout=timeout,
        ),
    )

    await engine.execute(
        _config(
            url="https://workflow.example/users/{user}/run",
            path_params={"user": {"source": "env", "key": "sapId"}},
            body=[{"id": {"source": "env", "key": "sapId"}}],
        ),
        env={"sapId": "user/1", "bbkOrgId": "100"},
        target_user_id="user/1",
    )

    assert len(seen) == 1
    assert seen[0].url.path == "/users/user/1/run"
    assert seen[0].url.raw_path.startswith(b"/users/user%2F1/run")
    assert seen[0].content == b'[{"id":"user/1"}]'


@pytest.mark.asyncio
async def test_workflow_engine_bounds_response_before_result_persistence():
    engine = WorkflowEngine(
        allowed_hosts={"workflow.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"code": "OK", "data": {"result": "x" * 1_100_000}},
                ),
            ),
            timeout=timeout,
        ),
    )

    with pytest.raises(RuntimeError, match="size limit"):
        await engine.execute(
            _config(),
            env={"sapId": "user-1", "bbkOrgId": "100"},
            target_user_id="user-1",
        )


@pytest.mark.asyncio
async def test_missing_configured_result_is_a_user_safe_failure():
    engine = WorkflowEngine(
        allowed_hosts={"workflow.example"},
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"code": "OK"}),
            ),
            timeout=timeout,
        ),
    )

    with pytest.raises(WorkflowCallError, match="技能任务结果不可用") as error:
        await engine.execute(
            _config(),
            env={"sapId": "user-1", "bbkOrgId": "100"},
            target_user_id="user-1",
        )

    assert error.value.error_code == "missing_result"
