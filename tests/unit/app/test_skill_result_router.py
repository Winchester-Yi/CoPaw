# -*- coding: utf-8 -*-
"""技能执行结果模块的路由与存储测试。"""

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from swe.app.skill_result.models import SkillResultCreate
from swe.app.skill_result.router import router as skill_result_router
from swe.app.skill_result.store import SkillResultStore

router_module = importlib.import_module("swe.app.skill_result.router")


def _make_db_with_insert(lastrowid: int) -> tuple[MagicMock, MagicMock]:
    """Return a database facade with no raw cursor interface."""
    db = MagicMock(spec=["is_connected", "insert"])
    db.is_connected = True
    db.insert = AsyncMock(return_value=lastrowid)
    return db, db


@pytest.mark.asyncio
async def test_create_stores_json_fields():
    """保存时应把 cust_list 与 metadata 序列化为 JSON 字符串。"""
    db, cur = _make_db_with_insert(1)
    store = SkillResultStore(db)

    payload = SkillResultCreate(
        trace_id="trace-1",
        skill_id="skill-1",
        user_id="10001",
        bbk="分行A",
        cust_list=["客户A", "客户B"],
        metadata={"note": "预留"},
        result_id="result-1",
    )

    record_id, trace_id = await store.create(payload, source_id="copaw")

    assert record_id == 1
    assert trace_id == "trace-1"
    query, params = cur.insert.call_args[0]
    assert "INSERT INTO swe_skill_result" in query
    assert params == (
        "copaw",
        "trace-1",
        "skill-1",
        "10001",
        "分行A",
        '["客户A", "客户B"]',
        '{"note": "预留"}',
        "result-1",
    )


@pytest.mark.asyncio
async def test_create_serializes_empty_list_and_none_metadata():
    """cust_list 为空应落库为 JSON 空数组，metadata 为 None 应落库为 NULL。"""
    db, cur = _make_db_with_insert(2)
    store = SkillResultStore(db)

    payload = SkillResultCreate(cust_list=[], metadata=None)

    record_id, _ = await store.create(payload, source_id="copaw")

    assert record_id == 2
    _, params = cur.insert.call_args[0]
    assert params[5] == "[]"
    assert params[6] is None


@pytest.mark.asyncio
async def test_create_returns_none_when_db_unavailable():
    """数据库不可用时应直接返回 (None, trace_id) 而不报错。"""
    db = MagicMock()
    db.is_connected = False
    store = SkillResultStore(db)

    payload = SkillResultCreate(trace_id="trace-x")
    record_id, trace_id = await store.create(payload, source_id=None)

    assert record_id is None
    assert trace_id == "trace-x"


def test_create_skill_result_route_saves_record(monkeypatch):
    """保存接口应返回记录 ID 与 trace_id。"""

    class _FakeService:
        async def create(self, payload, **kwargs):
            assert kwargs["source_id"] == "copaw"
            assert payload.bbk == "分行A"
            assert payload.user_id == "10001"
            return 5, "trace-5"

    app = FastAPI()

    @app.middleware("http")
    async def _inject_state(request: Request, call_next):
        request.state.source_id = "copaw"
        request.state.user_id = "10001"
        request.state.bbk_id = "分行A"
        return await call_next(request)

    app.include_router(skill_result_router)
    monkeypatch.setattr(router_module, "_service", _FakeService())

    client = TestClient(app)
    response = client.post(
        "/skill-result",
        json={
            "trace_id": "trace-5",
            "skill_id": "skill-5",
            "cust_list": ["客户A"],
            "metadata": {"k": "v"},
            "result_id": "result-5",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["id"] == 5
    assert payload["trace_id"] == "trace-5"


def test_create_skill_result_route_enriches_user_from_state(monkeypatch):
    """未显式传 user_id/bbk 时应从请求上下文自动填充。"""
    captured = {}

    class _FakeService:
        async def create(self, payload, **kwargs):
            captured["payload"] = payload
            return 6, "trace-6"

    app = FastAPI()

    @app.middleware("http")
    async def _inject_state(request: Request, call_next):
        request.state.source_id = "copaw"
        request.state.user_id = "10002"
        request.state.bbk_id = "分行B"
        return await call_next(request)

    app.include_router(skill_result_router)
    monkeypatch.setattr(router_module, "_service", _FakeService())

    client = TestClient(app)
    response = client.post(
        "/skill-result",
        json={"trace_id": "trace-6"},
    )

    assert response.status_code == 200
    assert captured["payload"].user_id == "10002"
    assert captured["payload"].bbk == "分行B"


def test_create_skill_result_route_returns_503_when_not_initialized():
    """模块未初始化时接口应返回 503。"""
    original = router_module._service
    router_module._service = None
    try:
        app = FastAPI()
        app.include_router(skill_result_router)
        client = TestClient(app)
        response = client.post("/skill-result", json={})
        assert response.status_code == 503
    finally:
        router_module._service = original
