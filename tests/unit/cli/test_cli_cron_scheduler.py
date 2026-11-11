# -*- coding: utf-8 -*-
"""Real CLI requests exercise the Scheduler contract at the HTTP boundary."""

import json

import httpx
import pytest
from click.testing import CliRunner

from swe.cli import cron_cmd


def detail():
    return {
        "management_owner": "scheduler",
        "agent_id": "default",
        "definition_version": 7,
        "management_version": 11,
        "management_status": "ready",
        "dispatch_state": "ordinary",
        "spec": {
            "id": "j1",
            "name": "原任务",
            "tenant_id": "alice",
            "source_id": "bank",
            "enabled": True,
            "task_type": "agent",
            "schedule": {
                "type": "cron",
                "cron": "0 9 * * *",
                "timezone": "Asia/Shanghai",
            },
            "dispatch": {
                "type": "channel",
                "channel": "console",
                "mode": "final",
                "target": {
                    "user_id": "alice",
                    "session_id": "original-session",
                },
            },
            "request": {
                "user_id": "alice",
                "session_id": "original-session",
                "input": [
                    {
                        "role": "user",
                        "type": "message",
                        "content": [{"type": "text", "text": "报告"}],
                    },
                ],
            },
            "meta": {
                "creator_user_id": "alice",
                "task_chat_id": "original-chat",
                "task_session_id": "original-session",
            },
        },
        "state": {"last_status": "success"},
        "task": {
            "chat_id": "original-chat",
            "session_id": "original-session",
            "is_paused": False,
        },
    }


def receipt(status="completed"):
    return {
        "operation": {
            "operation_id": "op1",
            "job_id": "j1",
            "action": "update",
            "status": status,
            "stage": "completed" if status == "completed" else "prepare",
            "error_code": None,
            "expected_version": 7,
            "version": 3,
            "logical_delete_applied": False,
        },
    }


@pytest.fixture
def invoke(monkeypatch):
    monkeypatch.setenv("SWE_TENANT_ID", "alice")
    monkeypatch.setenv("SWE_SOURCE_ID", "bank")
    monkeypatch.delenv("SWE_SCHEDULER_API_URL", raising=False)
    calls, bases = [], []

    def run(args, serve=None, *, scope=True):
        def handler(request):
            calls.append(request)
            if serve:
                return serve(request)
            if request.method == "GET":
                if request.url.path.endswith("/operations/pending"):
                    return httpx.Response(200, json=receipt("pending"))
                if request.url.path.endswith("/operations/op1"):
                    return httpx.Response(200, json=receipt()["operation"])
                if "/executions/" in request.url.path:
                    return httpx.Response(
                        200,
                        json={
                            "job_id": "j1",
                            "execution_id": "execution1",
                            "status": "success",
                        },
                    )
                return httpx.Response(200, json=detail())
            return httpx.Response(200, json=receipt())

        def client(base):
            bases.append(base)
            return httpx.Client(
                base_url=(
                    base.rstrip("/")
                    if base.rstrip("/").endswith("/api")
                    else base.rstrip("/") + "/api"
                ),
                transport=httpx.MockTransport(handler),
            )

        monkeypatch.setattr(cron_cmd, "client", client)
        suffix = (
            ["--tenant-id", "alice", "--source-id", "bank"] if scope else []
        )
        return CliRunner().invoke(cron_cmd.cron_group, [*args, *suffix])

    return run, calls, bases


def test_list_pages_scheduler_only_and_returns_all_jobs(invoke):
    run, calls, bases = invoke

    def serve(request):
        assert request.url.path == "/api/scheduler/cron/jobs"
        job = {
            **detail()["spec"],
            "management_owner": "scheduler",
            "agent_id": "default",
            "definition_version": 7,
        }
        if request.url.params.get("cursor"):
            return httpx.Response(
                200,
                json={"items": [{**job, "id": "j2"}], "next_cursor": None},
            )
        return httpx.Response(
            200,
            json={"items": [job], "next_cursor": "page2"},
        )

    result = run(["list"], serve)
    assert result.exit_code == 0, result.output
    assert [item["id"] for item in json.loads(result.output)] == ["j1", "j2"]
    assert len(calls) == 2
    assert calls[0].headers["X-User-Id"] == "alice"
    assert ":9100" in bases[0]


@pytest.mark.parametrize("command", ["get", "state"])
def test_read_uses_detail_and_preserves_task_binding(invoke, command):
    run, calls, _ = invoke
    result = run([command, "j1"])
    assert result.exit_code == 0, result.output
    assert calls[0].url.path == "/api/scheduler/cron/jobs/j1"
    assert (
        json.loads(result.output)["task"]["session_id"] == "original-session"
    )


@pytest.mark.parametrize(
    "command,method,suffix,version_field,version",
    [
        ("delete", "DELETE", "", "expected_definition_version", 7),
        ("pause", "PUT", "/enabled", "expected_management_version", 11),
        ("resume", "PUT", "/enabled", "expected_management_version", 11),
        ("run", "POST", "/run", "expected_definition_version", 7),
    ],
)
def test_mutation_reads_correct_version_and_sends_request_id(
    invoke,
    command,
    method,
    suffix,
    version_field,
    version,
):
    run, calls, _ = invoke

    def serve(request):
        if request.method == "GET":
            return httpx.Response(200, json=detail())
        from scheduler.app.routers.managed_cron_controls import (
            EnableRequest,
            RunRequest,
        )
        from scheduler.app.routers.managed_cron_management import DeleteRequest

        {
            "delete": DeleteRequest,
            "pause": EnableRequest,
            "resume": EnableRequest,
            "run": RunRequest,
        }[command].model_validate(json.loads(request.content))
        if command == "run":
            return httpx.Response(
                202,
                json={
                    "execution": {
                        "execution_id": "execution1",
                        "job_id": "j1",
                        "status": "queued",
                    },
                    "reused": False,
                },
            )
        return httpx.Response(202, json=receipt("pending"))

    result = run([command, "j1"], serve)
    assert result.exit_code == 0, result.output
    request = calls[-1]
    body = json.loads(request.content)
    assert request.method == method
    assert request.url.path == "/api/scheduler/cron/jobs/j1" + suffix
    assert body[version_field] == version
    assert body["request_id"]
    if command in {"pause", "resume"}:
        assert body["enabled"] is (command == "resume")
    data = json.loads(result.output)
    assert data.get("operation", data.get("execution"))["status"] in {
        "pending",
        "queued",
    }


def test_create_envelope_and_stable_request_id(invoke, tmp_path):
    run, calls, _ = invoke
    path = tmp_path / "job.json"
    path.write_text(json.dumps(detail()["spec"]), encoding="utf-8")
    result = run(["--request-id", "create-request", "create", "-f", str(path)])
    assert result.exit_code == 0, result.output
    body = json.loads(calls[-1].content)
    assert body["request_id"] == "create-request"
    assert body["dispatch_state"] == "ordinary"
    assert not body["spec"].get("id")
    assert json.loads(result.output)["operation"]["job_id"] == "j1"


def test_update_uses_version_without_replacing_original_ids(invoke):
    run, calls, _ = invoke
    result = run(["update", "j1", "--name", "新名称"])
    assert result.exit_code == 0, result.output
    body = json.loads(calls[-1].content)
    assert body["expected_definition_version"] == 7
    assert body["spec"]["id"] == "j1"
    assert body["spec"]["name"] == "新名称"
    assert body["spec"]["request"]["session_id"] == "original-session"
    assert body["spec"]["meta"]["task_chat_id"] == "original-chat"


def test_update_rejects_enabled_change_without_partial_write(invoke):
    run, calls, _ = invoke
    result = run(["update", "j1", "--no-enabled"])
    assert result.exit_code != 0
    assert "pause" in result.output
    assert all(request.method == "GET" for request in calls)


def test_missing_source_fails_before_http_instead_of_using_legacy(
    invoke,
    monkeypatch,
):
    run, calls, _ = invoke
    monkeypatch.delenv("SWE_SOURCE_ID")
    result = run(["list", "--tenant-id", "alice"], scope=False)
    assert result.exit_code != 0
    assert "source" in result.output.lower()
    assert not calls


def test_runtime_identity_and_actor_override(invoke):
    run, calls, _ = invoke
    result = run(["--user-id", "actual-actor", "get", "j1"], scope=False)
    assert result.exit_code == 0, result.output
    assert calls[0].headers["X-User-Id"] == "actual-actor"
    assert calls[0].headers["X-Tenant-Id"] == "alice"
    assert calls[0].headers["X-Source-Id"] == "bank"
    assert "X-User-Role" not in calls[0].headers


@pytest.mark.parametrize(
    "args,path,method",
    [
        (["operation", "j1"], "/operations/pending", "GET"),
        (["operation", "j1", "op1"], "/operations/op1", "GET"),
        (["retry", "j1", "op1"], "/operations/op1/retry", "POST"),
        (["execution", "j1", "execution1"], "/executions/execution1", "GET"),
    ],
)
def test_recovery_commands_use_original_operation_or_execution(
    invoke,
    args,
    path,
    method,
):
    run, calls, _ = invoke
    result = run(args)
    assert result.exit_code == 0, result.output
    assert calls[-1].url.path == "/api/scheduler/cron/jobs/j1" + path
    assert calls[-1].method == method


def test_failure_prints_receipt_without_retrying_or_falling_back(invoke):
    run, calls, _ = invoke
    result = run(
        ["pause", "j1"],
        lambda request: (
            httpx.Response(200, json=detail())
            if request.method == "GET"
            else httpx.Response(503, json=receipt("failed"))
        ),
    )
    assert result.exit_code != 0
    assert "op1" in result.output
    assert "j1" in result.output
    assert len(calls) == 2


def test_repeated_cursor_is_an_error(invoke):
    run, calls, _ = invoke
    result = run(
        ["list"],
        lambda request: httpx.Response(
            200,
            json={"items": [], "next_cursor": "same"},
        ),
    )
    assert result.exit_code != 0
    assert len(calls) == 2


def test_scheduler_environment_url_and_explicit_override(invoke, monkeypatch):
    run, _, bases = invoke
    monkeypatch.setenv("SWE_SCHEDULER_API_URL", "https://scheduler.test/api")
    result = run(["get", "j1"])
    assert result.exit_code == 0, result.output
    assert bases[-1] == "https://scheduler.test/api"
    run(["get", "j1", "--base-url", "https://override.test"])
    assert bases[-1] == "https://override.test"


def test_update_cannot_silently_change_target_user(invoke):
    run, calls, _ = invoke
    result = run(["update", "j1", "--target-user", "bob"])
    assert result.exit_code != 0
    assert "target user" in result.output
    assert all(request.method == "GET" for request in calls)


def test_operation_receipt_must_belong_to_requested_job(invoke):
    run, _, _ = invoke

    def serve(request):
        if request.method == "GET":
            return httpx.Response(200, json=detail())
        data = receipt()
        data["operation"]["job_id"] = "some-other-job"
        return httpx.Response(200, json=data)

    result = run(["pause", "j1"], serve)
    assert result.exit_code != 0
    assert "receipt" in result.output


def test_query_operation_rejects_different_id(invoke):
    run, _, _ = invoke
    data = receipt()["operation"]
    data["operation_id"] = "different-operation"
    result = run(
        ["operation", "j1", "op1"],
        lambda request: httpx.Response(200, json=data),
    )
    assert result.exit_code != 0
    assert "operation" in result.output


def test_timeout_exposes_request_id_without_leaking_or_retrying(invoke):
    run, calls, _ = invoke

    def serve(request):
        if request.method == "GET":
            return httpx.Response(200, json=detail())
        raise httpx.ReadTimeout("private-credential", request=request)

    result = run(["--request-id", "original-request", "pause", "j1"], serve)
    assert result.exit_code != 0
    assert "original-request" in result.output
    assert "private-credential" not in result.output
    assert len(calls) == 2


def test_update_metadata_preserves_complete_agent_input(invoke):
    run, calls, _ = invoke
    data = detail()
    original = [
        {
            "role": "user",
            "type": "message",
            "content": [
                {"type": "text", "text": "报告"},
                {"type": "image", "image_url": "https://image.example/a.png"},
            ],
        },
    ]
    data["spec"]["request"]["input"] = original
    period = {"start_date": "2027-01-01", "end_date": "2027-12-31"}
    data["spec"]["run_date_range"] = period
    data["spec"]["schedule"].update(
        cron="0 9 LW * * 2027",
        run_on_non_workday=True,
    )
    result = run(
        ["update", "j1", "--name", "保留输入"],
        lambda request: httpx.Response(
            200,
            json=data if request.method == "GET" else receipt(),
        ),
    )
    assert result.exit_code == 0, result.output
    assert (
        json.loads(calls[-1].content)["spec"]["request"]["input"] == original
    )
    updated = json.loads(calls[-1].content)["spec"]
    assert updated["run_date_range"] == period
    assert updated["schedule"]["cron"] == "0 9 LW * * 2027"
    assert updated["schedule"]["run_on_non_workday"] is True


def test_update_workflow_name_without_rebuilding_content(invoke):
    run, calls, _ = invoke
    data = detail()
    data["spec"].update(
        task_type="workflow",
        skill_ids="workflow-skill",
        workflow_binding_id="original-binding",
    )
    data["spec"]["request"]["input"] = None
    result = run(
        ["update", "j1", "--name", "工作流新名称"],
        lambda request: httpx.Response(
            200,
            json=data if request.method == "GET" else receipt(),
        ),
    )
    assert result.exit_code == 0, result.output
    spec = json.loads(calls[-1].content)["spec"]
    assert spec["workflow_binding_id"] == "original-binding"
    assert spec["request"]["input"] is None


@pytest.mark.parametrize(
    "expression",
    ["0 9 * * *", "0 9 LW * * 2027", "0 9 * * MON 2026-2029"],
)
def test_create_minimal_cli_payload_is_accepted_by_scheduler_schema(
    invoke,
    expression,
):
    from scheduler.app.routers.managed_cron_management import CreateRequest
    from scheduler.app.services.cron.managed.definition import normalize_spec
    from scheduler.app.services.cron.managed.identity import CallerIdentity

    run, calls, _ = invoke
    result = run(
        [
            "create",
            "--type",
            "agent",
            "--name",
            "自动绑定会话",
            "--cron",
            expression,
            "--channel",
            "console",
            "--text",
            "报告",
            "--timezone",
            "Asia/Shanghai",
        ],
    )
    assert result.exit_code == 0, result.output
    body = CreateRequest.model_validate(json.loads(calls[-1].content))
    spec = normalize_spec(
        body.spec,
        CallerIdentity.from_headers(calls[-1].headers),
        "server-created-id",
    )
    assert spec["id"] == "server-created-id"
    assert spec["request"]["session_id"] == "cron-task:server-created-id"
    assert spec["request"]["user_id"] == "alice"
    assert spec["schedule"]["cron"] == expression
    assert spec["schedule"]["run_on_non_workday"] is False


def test_explicit_global_scheduler_host_port_overrides_environment(
    invoke,
    monkeypatch,
):
    from swe.cli import main

    run, _, bases = invoke
    monkeypatch.setenv("SWE_SCHEDULER_API_URL", "https://environment.test/api")
    monkeypatch.setattr(main, "read_last_api", lambda: ("old-swe", 8088))
    run(["get", "j1"])
    result = CliRunner().invoke(
        main.cli,
        [
            "--host",
            "scheduler.test",
            "--port",
            "9200",
            "cron",
            "get",
            "j1",
            "--tenant-id",
            "alice",
            "--source-id",
            "bank",
        ],
    )
    assert result.exit_code == 0, result.output
    assert bases[-1] == "http://scheduler.test:9200"


@pytest.mark.parametrize(
    "change",
    [
        {"management_owner": "legacy"},
        {"agent_id": "other-agent"},
        {"tenant_id": "bob"},
    ],
)
def test_list_rejects_non_scheduler_or_foreign_identity(invoke, change):
    run, _, _ = invoke
    job = {
        **detail()["spec"],
        "management_owner": "scheduler",
        "agent_id": "default",
        **change,
    }
    result = run(
        ["list"],
        lambda request: httpx.Response(
            200,
            json={"items": [job], "next_cursor": None},
        ),
    )
    assert result.exit_code != 0
    assert "identity mismatch" in result.output


@pytest.mark.parametrize("status", [404, 409])
def test_query_error_never_falls_back_or_retries(invoke, status):
    run, calls, _ = invoke
    result = run(
        ["get", "j1"],
        lambda request: httpx.Response(
            status,
            json={
                "detail": (
                    "cron_job_not_found"
                    if status == 404
                    else "cron_definition_version_conflict"
                ),
            },
        ),
    )
    assert result.exit_code != 0
    assert str(status) in result.output
    assert len(calls) == 1
    assert calls[0].url.path == "/api/scheduler/cron/jobs/j1"


def test_runtime_encoded_scope_is_decoded_for_header(invoke, monkeypatch):
    from swe.config.context import encode_scope_id

    run, calls, _ = invoke
    monkeypatch.setenv("SWE_TENANT_ID", encode_scope_id("alice", "bank"))
    result = run(["get", "j1"], scope=False)
    assert result.exit_code == 0, result.output
    assert calls[0].headers["X-Tenant-Id"] == "alice"


def test_invalid_version_blocks_write(invoke):
    run, calls, _ = invoke
    data = detail()
    data["definition_version"] = True
    result = run(
        ["delete", "j1"],
        lambda request: httpx.Response(200, json=data),
    )
    assert result.exit_code != 0
    assert "definition_version" in result.output
    assert all(request.method == "GET" for request in calls)


def test_retry_does_not_claim_a_new_request_id_was_submitted(invoke):
    run, calls, _ = invoke
    result = run(["retry", "j1", "op1"])
    assert result.exit_code == 0, result.output
    assert calls[-1].content == b""
    assert "request_id" not in json.loads(result.output)
