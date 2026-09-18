# -*- coding: utf-8 -*-
"""Tests for high-frequency question APIs."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, time as datetime_time, timedelta

import pytest
from pydantic import ValidationError

from monitor.app.models.high_frequency_question import (  
    HighFrequencyQuestionMessageQueryRequest,
    HighFrequencyQuestionResultSaveRequest,
    HighFrequencyQuestionScheduledTaskRequest,
    HighFrequencyQuestionTaskSubmitRequest,
)
from monitor.app.routers.high_frequency_question import _resolve_source_id
from monitor.app.services.tracing import high_frequency_question as hfq_service_module
from monitor.app.services.tracing.high_frequency_question import (
    HighFrequencyQuestionService,
)


def test_message_query_requires_valid_time_range():
    with pytest.raises(ValidationError):
        HighFrequencyQuestionMessageQueryRequest(
            source_id="RMASSIST",
            start_time="2026-07-30 00:00:00",
            end_time="2026-07-23 00:00:00",
        )


def test_result_save_rejects_invalid_scope_bbk_pair():
    with pytest.raises(ValidationError):
        HighFrequencyQuestionResultSaveRequest(
            source_id="RMASSIST",
            batch_id="HFQ_20260730_030000",
            stat_start_time="2026-07-23 00:00:00",
            stat_end_time="2026-07-30 00:00:00",
            results=[
                {
                    "scope_type": "ALL",
                    "bbk_id": "110",
                    "rank_no": 1,
                    "topic_name": "查询客户保险持仓",
                    "message_count": 10,
                    "valid_message_count": 20,
                    "user_count": 15,
                    "total_skill_used_count": 12,
                    "skill_used_count": 8,
                    "sample_questions": [],
                },
            ],
        )


def test_result_save_rejects_duplicate_rank_key():
    with pytest.raises(ValidationError):
        HighFrequencyQuestionResultSaveRequest(
            source_id="RMASSIST",
            batch_id="HFQ_20260730_030000",
            stat_start_time="2026-07-23 00:00:00",
            stat_end_time="2026-07-30 00:00:00",
            results=[
                _result_item(),
                _result_item(),
            ],
        )


def test_resolve_source_id_prefers_header_then_body_then_default():
    assert _resolve_source_id(" RMASSIST ", "default") == "RMASSIST"
    assert _resolve_source_id(None, " body-source ") == "body-source"
    assert _resolve_source_id(None, None) == "default"


@pytest.mark.asyncio
async def test_query_messages_uses_expected_filters():
    db = _FakeDb(
        rows=[
            {
                "trace_id": "trace-001",
                "user_id": "136807",
                "session_id": "session-001",
                "bbk_id": "110",
                "user_message": "查保险",
                "start_time": datetime(2026, 7, 29, 10, 20, 0),
                "skills_used": '["保险助手", "客户分析"]',
            },
            {
                "trace_id": "trace-002",
                "user_id": "136807",
                "session_id": "session-002",
                "bbk_id": "110",
                "user_message": "再查一次",
                "start_time": datetime(2026, 7, 29, 10, 25, 0),
                "skills_used": "[]",
            },
            {
                "trace_id": "trace-003",
                "user_id": "246810",
                "session_id": "session-003",
                "bbk_id": "110",
                "user_message": "查理赔",
                "start_time": datetime(2026, 7, 29, 10, 30, 0),
                "skills_used": None,
            },
        ],
    )
    service = HighFrequencyQuestionService(db)
    request = HighFrequencyQuestionMessageQueryRequest(
        source_id="RMASSIST",
        start_time="2026-07-23 00:00:00",
        end_time="2026-07-30 00:00:00",
        bbk_id="110",
    )

    response = await service.query_messages(request)

    assert response.total == 3
    assert response.message_count == 3
    assert response.user_count == 2
    assert response.data[0].content == "查保险"
    assert response.data[0].skills_used == ["保险助手", "客户分析"]
    dumped_message = response.data[0].model_dump()
    assert "message_id" not in dumped_message
    assert "session_id" not in dumped_message
    assert "message_time" not in dumped_message
    sql = db.fetch_all_calls[0][0]
    params = db.fetch_all_calls[0][1]
    assert "skills_used" in sql
    assert "source_id = %s" in sql
    assert "status = 'completed'" in sql
    assert "session_id NOT LIKE %s" in sql
    assert "TRIM(user_message) NOT IN" in sql
    assert "bbk_id = %s" in sql
    assert params[0] == "RMASSIST"
    assert "cron-task%" in params
    assert params[-2] == "110"
    assert params[-1] == 10001


@pytest.mark.asyncio
async def test_save_results_deletes_and_batch_inserts_in_transaction():
    db = _FakeDb()
    service = HighFrequencyQuestionService(db)
    request = HighFrequencyQuestionResultSaveRequest(
        source_id="RMASSIST",
        batch_id="HFQ_20260730_030000",
        stat_start_time="2026-07-23 00:00:00",
        stat_end_time="2026-07-30 00:00:00",
        results=[_result_item()],
    )

    response = await service.save_results(request)

    assert response.saved_count == 1
    assert db.conn.began is True
    assert db.conn.committed is True
    assert db.conn.rolled_back is False
    assert "DELETE FROM swe_high_frequency_question_result" in db.cursor.executes[0][0]
    assert db.cursor.executes[0][1] == ("RMASSIST", "HFQ_20260730_030000")
    insert_sql, insert_params = db.cursor.many[0]
    assert "INSERT INTO swe_high_frequency_question_result" in insert_sql
    assert "source_id" in insert_sql
    assert "user_count" in insert_sql
    assert "total_skill_used_count" in insert_sql
    assert "skill_used_count" in insert_sql
    assert "top_skill" in insert_sql
    assert insert_params[0][0] == "RMASSIST"
    assert insert_params[0][1] == "HFQ_20260730_030000"
    assert insert_params[0][10] == 15
    assert insert_params[0][11] == 12
    assert insert_params[0][12] == 8
    assert insert_params[0][13] == "保险助手"


@pytest.mark.asyncio
async def test_save_results_allows_empty_top_skill_and_array_bbk_dis():
    db = _FakeDb()
    service = HighFrequencyQuestionService(db)
    request = HighFrequencyQuestionResultSaveRequest(
        source_id="RMASSIST",
        batch_id="HFQ_20260730_030000",
        stat_start_time="2026-07-23 00:00:00",
        stat_end_time="2026-07-30 00:00:00",
        results=[
            _result_item(
                top_skill="",
                bbk_dis=[
                    {"bbk_id": 110, "count": 300},
                    {"bbk_id": 121, "count": 220},
                ],
            ),
        ],
    )

    response = await service.save_results(request)

    assert response.saved_count == 1
    insert_params = db.cursor.many[0][1][0]
    assert insert_params[13] is None
    assert insert_params[14] == (
        '[{"bbk_id": 110, "count": 300}, {"bbk_id": 121, "count": 220}]'
    )


def test_result_save_rejects_skill_used_count_above_message_count():
    with pytest.raises(ValidationError):
        HighFrequencyQuestionResultSaveRequest(
            source_id="RMASSIST",
            batch_id="HFQ_20260730_030000",
            stat_start_time="2026-07-23 00:00:00",
            stat_end_time="2026-07-30 00:00:00",
            results=[_result_item(skill_used_count=11)],
        )


def test_result_save_rejects_total_skill_used_count_above_valid_message_count():
    with pytest.raises(ValidationError):
        HighFrequencyQuestionResultSaveRequest(
            source_id="RMASSIST",
            batch_id="HFQ_20260730_030000",
            stat_start_time="2026-07-23 00:00:00",
            stat_end_time="2026-07-30 00:00:00",
            results=[_result_item(total_skill_used_count=21)],
        )


def test_result_save_rejects_inconsistent_batch_user_count():
    with pytest.raises(ValidationError):
        HighFrequencyQuestionResultSaveRequest(
            source_id="RMASSIST",
            batch_id="HFQ_20260730_030000",
            stat_start_time="2026-07-23 00:00:00",
            stat_end_time="2026-07-30 00:00:00",
            results=[
                _result_item(rank_no=1),
                _result_item(rank_no=2, user_count=16),
            ],
        )


def test_result_save_rejects_inconsistent_batch_total_skill_used_count():
    with pytest.raises(ValidationError):
        HighFrequencyQuestionResultSaveRequest(
            source_id="RMASSIST",
            batch_id="HFQ_20260730_030000",
            stat_start_time="2026-07-23 00:00:00",
            stat_end_time="2026-07-30 00:00:00",
            results=[
                _result_item(rank_no=1),
                _result_item(rank_no=2, total_skill_used_count=13),
            ],
        )


@pytest.mark.asyncio
async def test_query_results_returns_summary_and_topic_skill_metrics():
    db = _FakeDb(
        rows=[
            {
                "rank_no": 1,
                "topic_name": "查询客户保险持仓",
                "message_count": 10,
                "valid_message_count": 20,
                "user_count": 15,
                "total_skill_used_count": 12,
                "skill_used_count": 8,
                "top_skill": "保险助手",
                "bbk_dis": '{"110": 6}',
                "sample_questions": '["查保险"]',
            },
            {
                "rank_no": 2,
                "topic_name": "查询理赔进度",
                "message_count": 5,
                "valid_message_count": 20,
                "user_count": 15,
                "total_skill_used_count": 12,
                "skill_used_count": 2,
                "top_skill": "",
                "bbk_dis": "{}",
                "sample_questions": "[]",
            },
        ],
        fetch_one_results=[
            {
                "batch_id": "batch-001",
                "stat_start_time": datetime(2026, 7, 23, 0, 0, 0),
                "stat_end_time": datetime(2026, 7, 30, 0, 0, 0),
                "result_updated_at": datetime(2026, 7, 30, 10, 0, 0),
                "result_count": 2,
            },
        ],
    )
    service = HighFrequencyQuestionService(db)

    response = await service.query_results(
        HighFrequencyQuestionTaskSubmitRequest(
            source_id="RMASSIST",
            start_time="2026-07-23 00:00:00",
            end_time="2026-07-30 00:00:00",
            bbk_id="110",
        ),
    )

    assert response.state == "AVAILABLE"
    assert response.message_count == 20
    assert response.user_count == 15
    assert response.total_skill_used_count == 12
    assert response.topic_count == 2
    assert response.skill_gap_topic_count == 1
    assert response.topics[0].skill_used_count == 8
    assert response.topics[0].top_skill == "保险助手"
    assert response.topics[0].bbk_dis == {"110": 6}
    assert response.topics[1].top_skill is None
    topic_sql = db.fetch_all_calls[0][0]
    assert "user_count" in topic_sql
    assert "total_skill_used_count" in topic_sql
    assert "skill_used_count" in topic_sql
    assert "top_skill" in topic_sql


@pytest.mark.asyncio
async def test_wait_for_result_rows_polls_until_rows_exist(monkeypatch):
    db = _FakeDb(fetch_one_results=[{"count": 0}, {"count": 3}])
    service = HighFrequencyQuestionService(db)
    monkeypatch.setattr(hfq_service_module, "HFQ_RESULT_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(
        hfq_service_module,
        "HFQ_RESULT_POLL_INTERVAL_SECONDS",
        1.0,
    )
    monkeypatch.setattr(hfq_service_module.asyncio, "sleep", _noop_sleep)

    result_count = await service._wait_for_result_rows(
        source_id="RMASSIST",
        batch_id="task-001",
    )

    assert result_count == 3
    assert len(db.fetch_one_calls) == 2


@pytest.mark.asyncio
async def test_submit_task_force_bypasses_recent_result(monkeypatch):
    created_tasks: list[tuple[str, object]] = []
    scheduled_tasks: list[object] = []
    db = _FakeDb(
        fetch_one_results=[
            {
                "batch_id": "existing-batch",
                "stat_start_time": datetime(2026, 7, 23, 0, 0, 0),
                "stat_end_time": datetime(2026, 7, 30, 23, 59, 59),
                "result_updated_at": datetime(2026, 7, 30, 10, 0, 0),
                "result_count": 1,
            },
        ],
    )
    service = HighFrequencyQuestionService(db)

    async def fake_create_async_task(**kwargs):
        created_tasks.append((kwargs["task_id"], kwargs["criteria"]))

    async def fake_run_workflow_and_finish_task(**_kwargs):
        return None

    def fake_create_task(coro):
        scheduled_tasks.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(service, "_create_async_task", fake_create_async_task)
    monkeypatch.setattr(
        service,
        "_run_workflow_and_finish_task",
        fake_run_workflow_and_finish_task,
    )
    monkeypatch.setattr(hfq_service_module.asyncio, "create_task", fake_create_task)

    response = await service.submit_task(
        HighFrequencyQuestionTaskSubmitRequest(
            source_id="RMASSIST",
            start_time="2026-07-23 00:00:00",
            end_time="2026-07-30 23:59:59",
            bbk_id="110",
            force=True,
        ),
    )

    assert response.state == "RUNNING"
    assert created_tasks
    assert scheduled_tasks


def test_submit_scheduled_task_builds_forced_seven_calendar_day_request(monkeypatch):
    db = _FakeDb()
    service = HighFrequencyQuestionService(db)
    captured: dict[str, object] = {}

    async def fake_submit_task(request, *, actor_user_id=None, actor_user_name=None):
        captured["request"] = request
        captured["actor_user_id"] = actor_user_id
        captured["actor_user_name"] = actor_user_name
        return {
            "state": "RUNNING",
            "task_id": "task-001",
            "batch_id": "task-001",
            "status": "running",
            "source_id": request.source_id,
            "stat_start_time": request.start_time,
            "stat_end_time": request.end_time,
            "scope_type": "ORG",
            "bbk_id": request.bbk_id,
            "topics": [],
        }

    monkeypatch.setattr(service, "submit_task", fake_submit_task)

    response = asyncio.run(
        service.submit_scheduled_task(
            HighFrequencyQuestionScheduledTaskRequest(
                source_id=" RMASSIST ",
                bbk_id=" 110 ",
            ),
        ),
    )

    request = captured["request"]
    assert isinstance(request, HighFrequencyQuestionTaskSubmitRequest)
    assert request.source_id == "RMASSIST"
    assert request.bbk_id == "110"
    assert request.force is True
    assert request.start_time.time() == datetime_time.min
    assert request.end_time.time() == datetime_time(23, 59, 59)
    assert request.end_time.date() - request.start_time.date() == timedelta(days=6)
    assert request.end_time.microsecond == 0
    assert captured["actor_user_id"] == hfq_service_module.SYSTEM_ACTOR_ID
    assert captured["actor_user_name"] == hfq_service_module.SYSTEM_ACTOR_NAME
    assert response["batch_id"] == "task-001"


def test_call_workflow_sends_batch_id_without_task_id(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"message": "success"}

    class _FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url, *, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _FakeResponse()

    monkeypatch.setattr(hfq_service_module, "HFQ_WORKFLOW_URL", "http://workflow")
    monkeypatch.setattr(hfq_service_module, "HFQ_WORKFLOW_API_KEY", "api-key")
    monkeypatch.setattr(hfq_service_module, "HFQ_WORKFLOW_OPEN_ID", "open-id")
    monkeypatch.setattr(hfq_service_module.httpx, "AsyncClient", _FakeClient)

    service = HighFrequencyQuestionService(_FakeDb())
    request = HighFrequencyQuestionTaskSubmitRequest(
        source_id="RMASSIST",
        start_time="2026-09-03 16:07:15",
        end_time="2026-09-10 16:07:15",
        bbk_id="100",
    )
    criteria = service._normalize_criteria(request)

    asyncio.run(service._call_workflow(task_id="task-001", criteria=criteria))

    payload = captured["json"]
    assert isinstance(payload, dict)
    input_params = payload["inputParams"]
    assert input_params["batch_id"] == "task-001"
    assert "task_id" not in input_params
    assert input_params["start_time"] == "2026-09-03 16:07:15"
    assert input_params["end_time"] == "2026-09-10 16:07:15"
    assert input_params["bbk_id"] == "100"
    assert captured["headers"] == {
        "API-Key": "api-key",
        "Content-Type": "application/json",
    }


def _result_item(**overrides) -> dict:
    item = {
        "scope_type": "ORG",
        "bbk_id": "110",
        "rank_no": 1,
        "topic_name": "查询客户保险持仓",
        "message_count": 10,
        "valid_message_count": 20,
        "user_count": 15,
        "total_skill_used_count": 12,
        "skill_used_count": 8,
        "top_skill": "保险助手",
        "bbk_dis": [],
        "sample_questions": ["查保险"],
    }
    item.update(overrides)
    return item


class _FakeDb:
    def __init__(
        self,
        rows: list[dict] | None = None,
        fetch_one_results: list[dict | None] | None = None,
    ) -> None:
        self.rows = rows or []
        self.fetch_one_results = fetch_one_results or []
        self.fetch_all_calls: list[tuple[str, tuple]] = []
        self.fetch_one_calls: list[tuple[str, tuple]] = []
        self.cursor = _FakeCursor()
        self.conn = _FakeConnection(self.cursor)

    async def fetch_all(self, query: str, params: tuple) -> list[dict]:
        self.fetch_all_calls.append((query, params))
        return self.rows

    async def fetch_one(self, query: str, params: tuple) -> dict | None:
        self.fetch_one_calls.append((query, params))
        if self.fetch_one_results:
            return self.fetch_one_results.pop(0)
        return None

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def _noop_sleep(_delay: float) -> None:
    return None


class _FakeConnection:
    def __init__(self, cursor: "_FakeCursor") -> None:
        self.cursor_obj = cursor
        self.began = False
        self.committed = False
        self.rolled_back = False

    async def begin(self) -> None:
        self.began = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    def cursor(self) -> "_FakeCursor":
        return self.cursor_obj


class _FakeCursor:
    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple]] = []
        self.many: list[tuple[str, list[tuple]]] = []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, query: str, params: tuple) -> None:
        self.executes.append((query, params))

    async def executemany(self, query: str, params: list[tuple]) -> None:
        self.many.append((query, params))
