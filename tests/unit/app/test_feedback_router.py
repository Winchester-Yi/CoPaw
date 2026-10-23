# -*- coding: utf-8 -*-
"""反馈模块的路由与存储测试。"""

import importlib
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from swe.app.feedback.models import FeedbackCreate, FeedbackRecord
from swe.app.feedback.router import router as feedback_api_router
from swe.app.feedback.store import FeedbackStore

feedback_router_module = importlib.import_module("swe.app.feedback.router")


@pytest.fixture
def mock_db():
    """构造一个可编排返回值的数据库桩。"""
    db = MagicMock()
    db.is_connected = True
    db.fetch_one = AsyncMock()
    db.execute = AsyncMock(return_value=1)
    return db


@pytest.mark.asyncio
async def test_get_feedback_parses_record(mock_db):
    """查询反馈时应正确解析 JSON 选项与时间字段。"""
    mock_db.fetch_one.return_value = {
        "id": 7,
        "source_id": "copaw",
        "feedback_user_name": "张三",
        "feedback_user_sap": "10001",
        "feedback_branch": "分行A",
        "feedback_sub_branch": "支行B",
        "feedback_position": "客户经理",
        "cron_task_name": None,
        "cron_task_id": None,
        "response_id": "resp-1",
        "trace_id": "trace-1",
        "chat_id": "chat-1",
        "session_id": "session-1",
        "feedback_options": '["输出格式需调整","其他想法"]',
        "feedback_content": "建议压缩结论区",
        "created_at": datetime(2026, 5, 19, 10, 0, 0),
        "updated_at": datetime(2026, 5, 19, 10, 5, 0),
    }
    store = FeedbackStore(mock_db)

    result = await store.get_feedback(
        source_id="copaw",
        response_id="resp-1",
    )

    assert result is not None
    assert result.id == 7
    assert result.trace_id == "trace-1"
    assert result.feedback_options == ["输出格式需调整", "其他想法"]


@pytest.mark.asyncio
async def test_upsert_feedback_updates_existing_record_by_response_id(mock_db):
    """同一回答再次提交反馈时应更新原记录而不是重复插入。"""
    mock_db.fetch_one.side_effect = [
        {"trace_id": "trace-latest"},
        {
            "id": 11,
            "source_id": "copaw",
            "feedback_user_name": "李四",
            "feedback_user_sap": "10002",
            "feedback_branch": "分行A",
            "feedback_sub_branch": "支行B",
            "feedback_position": "理财经理",
            "cron_task_name": None,
            "cron_task_id": None,
            "response_id": "resp-2",
            "trace_id": "trace-old",
            "chat_id": "chat-2",
            "session_id": "session-2",
            "feedback_options": '["筛选逻辑不对"]',
            "feedback_content": "旧反馈",
            "created_at": datetime(2026, 5, 19, 10, 0, 0),
            "updated_at": datetime(2026, 5, 19, 10, 1, 0),
        },
    ]
    store = FeedbackStore(mock_db)

    feedback = FeedbackCreate(
        response_id="resp-2",
        session_id="session-2",
        feedback_content="新反馈内容",
        feedback_options=["分析维度需增删"],
        feedback_user_name="李四",
        feedback_user_sap="10002",
    )
    feedback_id, updated, trace_id = await store.upsert_feedback(
        feedback,
        source_id="copaw",
    )

    assert feedback_id == 11
    assert updated is True
    assert trace_id == "trace-latest"
    mock_db.execute.assert_awaited_once()
    assert "UPDATE swe_response_feedback" in mock_db.execute.call_args[0][0]


def test_get_current_feedback_route_returns_existing_record(monkeypatch):
    """路由应返回当前回答对应的已保存反馈。"""

    class _FakeService:
        async def get_feedback(self, **kwargs):
            assert kwargs["source_id"] == "copaw"
            assert kwargs["response_id"] == "resp-9"
            return FeedbackRecord(
                id=9,
                source_id="copaw",
                response_id="resp-9",
                trace_id="trace-9",
                feedback_content="建议补充依据",
                feedback_options=["其他想法"],
            )

    app = FastAPI()

    @app.middleware("http")
    async def _inject_state(request: Request, call_next):
        request.state.source_id = "copaw"
        return await call_next(request)

    app.include_router(feedback_api_router)
    monkeypatch.setattr(feedback_router_module, "_service", _FakeService())

    client = TestClient(app)
    response = client.get(
        "/feedback/current",
        params={"response_id": "resp-9"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["feedback"]["id"] == 9
    assert payload["feedback"]["trace_id"] == "trace-9"


def test_get_session_feedbacks_route_returns_items(monkeypatch):
    """按聊天 ID 和运行时会话 ID 查询时应返回当前会话下的反馈列表。"""

    class _FakeService:
        async def list_feedbacks_by_session(self, **kwargs):
            assert kwargs["source_id"] == "copaw"
            assert kwargs["chat_id"] == "chat-9"
            assert kwargs["session_id"] == "session-9"
            return [
                FeedbackRecord(
                    id=12,
                    source_id="copaw",
                    response_id="resp-12",
                    trace_id="trace-12",
                    chat_id="chat-9",
                    session_id="session-9",
                    feedback_content="建议补一个行动建议",
                    feedback_options=["其他想法"],
                ),
            ]

    app = FastAPI()

    @app.middleware("http")
    async def _inject_state(request: Request, call_next):
        request.state.source_id = "copaw"
        return await call_next(request)

    app.include_router(feedback_api_router)
    monkeypatch.setattr(feedback_router_module, "_service", _FakeService())

    client = TestClient(app)
    response = client.get(
        "/feedback/session",
        params={"chat_id": "chat-9", "session_id": "session-9"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["items"]) == 1
    assert payload["items"][0]["response_id"] == "resp-12"


@pytest.mark.asyncio
async def test_list_feedbacks_by_session_matches_chat_or_session(mock_db):
    """切换会话回填时应同时兼容真实聊天 ID 和运行时会话 ID。"""
    mock_db.fetch_all = AsyncMock(return_value=[])
    store = FeedbackStore(mock_db)

    await store.list_feedbacks_by_session(
        source_id="copaw",
        chat_id="chat-1",
        session_id="session-1",
    )

    query, params = mock_db.fetch_all.call_args[0]
    assert "(chat_id = %s OR session_id = %s)" in query
    assert params == ("chat-1", "session-1", "copaw")


@pytest.mark.asyncio
@pytest.mark.parametrize("insert_id", [42, 0, None])
async def test_create_feedback_uses_facade_insert_id(insert_id):
    db = MagicMock(spec=["is_connected", "insert"])
    db.is_connected = True
    db.insert = AsyncMock(return_value=insert_id)
    payload = FeedbackCreate(
        feedback_content="反馈内容",
        feedback_options=["准确"],
    )

    result = await FeedbackStore(db).upsert_feedback(
        payload,
        source_id="source-a",
    )

    assert result == (insert_id or None, False, None)
    query, params = db.insert.await_args.args
    assert "INSERT INTO swe_response_feedback" in query
    assert params[0] == "source-a"
    assert params[-2:] == ('["准确"]', "反馈内容")
