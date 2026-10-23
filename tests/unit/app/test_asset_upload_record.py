# -*- coding: utf-8 -*-
"""资产上传记录模块的路由与存储测试。"""

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from swe.app.asset_upload_record.models import (
    AssetUploadFileNameList,
    AssetUploadRecord,
    PaginatedAssetUploadRecords,
    TemplateItem,
    TemplateResultResponse,
    TemplateSearchResponse,
)
from swe.app.asset_upload_record.router import router as upload_record_router
from swe.app.asset_upload_record.store import AssetUploadRecordStore

upload_record_router_module = importlib.import_module(
    "swe.app.asset_upload_record.router",
)


@pytest.fixture
def mock_db():
    """构造一个可编排返回值的数据库桩。"""
    db = MagicMock()
    db.is_connected = True
    db.fetch_all = AsyncMock()
    db.fetch_one = AsyncMock()
    db.execute = AsyncMock(return_value=1)
    return db


# ---- Store 层测试 ----


@pytest.mark.asyncio
async def test_list_all_file_names_from_store(mock_db):
    """store 应查询所有 id+file_name 并返回字典列表。"""
    mock_db.fetch_all.return_value = [
        {"id": 1, "file_name": "report.pdf", "template_flag": "main-1"},
        {"id": 2, "file_name": "data.xlsx", "template_flag": None},
        {"id": 3, "file_name": "image.png", "template_flag": None},
    ]
    store = AssetUploadRecordStore(mock_db)

    result = await store.list_all_file_names()

    assert result == [
        {"id": 1, "file_name": "report.pdf", "template_flag": "main-1"},
        {"id": 2, "file_name": "data.xlsx", "template_flag": None},
        {"id": 3, "file_name": "image.png", "template_flag": None},
    ]
    mock_db.fetch_all.assert_awaited_once()
    query = mock_db.fetch_all.call_args[0][0]
    assert (
        "SELECT id, file_name, template_flag FROM swe_asset_upload_record"
        in query
    )


@pytest.mark.asyncio
async def test_list_all_file_names_returns_empty_when_db_not_connected():
    """数据库未连接时 store 应返回空列表。"""
    db = MagicMock()
    db.is_connected = False
    store = AssetUploadRecordStore(db)

    result = await store.list_all_file_names()

    assert result == []


@pytest.mark.asyncio
async def test_list_records_from_store(mock_db):
    """store 分页查询应正确构建 SQL 并转换行记录。"""
    mock_db.fetch_all.return_value = [
        {
            "id": 1,
            "file_name": "test.pdf",
            "file_size": 1024,
            "asset_path": "asset/test.pdf",
            "source_id": "src-1",
            "template_flag": None,
            "created_at": None,
            "updated_at": None,
        },
    ]
    store = AssetUploadRecordStore(mock_db)

    result = await store.list_records(page=1, page_size=20)

    assert len(result) == 1
    assert result[0].file_name == "test.pdf"
    assert result[0].file_size == 1024


# ---- Service 层测试 ----


@pytest.mark.asyncio
async def test_list_all_file_names_from_service(mock_db):
    """service 应调用 store 并返回 AssetUploadFileNameList。"""
    mock_db.fetch_all.return_value = [
        {"id": 1, "file_name": "a.pdf", "template_flag": "main-a"},
        {"id": 2, "file_name": "b.xlsx", "template_flag": None},
    ]
    store = AssetUploadRecordStore(mock_db)

    from swe.app.asset_upload_record.service import AssetUploadRecordService

    service = AssetUploadRecordService(store)
    result = await service.list_all_file_names()

    assert isinstance(result, AssetUploadFileNameList)
    assert len(result.data) == 2
    assert result.data[0].templateId == 1
    assert result.data[0].templateName == "a.pdf"
    assert result.data[0].templateFlag == "main-a"
    assert result.data[1].templateId == 2
    assert result.data[1].templateName == "b.xlsx"
    assert result.data[1].templateFlag is None


# ---- 路由层测试 ----


def test_list_file_names_route_returns_names(monkeypatch):
    """GET /template/file-templates 应返回统一格式响应。"""

    class _FakeService:
        async def list_all_file_names(self):
            return AssetUploadFileNameList(
                data=[
                    TemplateItem(
                        templateId=123,
                        templateName="report.pdf",
                        templateFlag="main-1",
                    ),
                    TemplateItem(templateId=124, templateName="data.xlsx"),
                ],
            )

    app = FastAPI()
    app.include_router(upload_record_router)
    monkeypatch.setattr(
        upload_record_router_module,
        "_service",
        _FakeService(),
    )

    client = TestClient(app)
    response = client.get("/template/file-templates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    assert payload["error"] is None
    assert len(payload["data"]) == 2
    assert payload["data"][0]["templateId"] == 123
    assert payload["data"][0]["templateName"] == "report.pdf"
    assert payload["data"][0]["templateFlag"] == "main-1"


def test_query_records_route_returns_paginated(monkeypatch):
    """GET /template/records 应返回分页结构。"""

    class _FakeService:
        async def query_records(self, **kwargs):
            return PaginatedAssetUploadRecords(
                items=[
                    AssetUploadRecord(
                        id=1,
                        file_name="test.pdf",
                        file_size=1024,
                        asset_path="asset/test.pdf",
                    ),
                ],
                total=1,
                page=1,
                page_size=20,
            )

    app = FastAPI()

    @app.middleware("http")
    async def _inject_state(request: Request, call_next):
        request.state.source_id = "src-1"
        return await call_next(request)

    app.include_router(upload_record_router)
    monkeypatch.setattr(
        upload_record_router_module,
        "_service",
        _FakeService(),
    )

    client = TestClient(app)
    response = client.get("/template/records")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["file_name"] == "test.pdf"


def test_list_file_names_returns_503_when_not_initialized():
    """模块未初始化时 GET /template/file-templates 应返回 503。"""
    app = FastAPI()
    app.include_router(upload_record_router)

    # _service 默认为 None，模拟未初始化
    importlib.reload(upload_record_router_module)

    client = TestClient(app)
    response = client.get("/template/file-templates")

    assert response.status_code == 503


def test_query_records_returns_503_when_not_initialized():
    """模块未初始化时 GET /template/records 应返回 503。"""
    app = FastAPI()
    app.include_router(upload_record_router)

    importlib.reload(upload_record_router_module)

    client = TestClient(app)
    response = client.get("/template/records")

    assert response.status_code == 503


# ---- 搜索接口测试 ----


@pytest.mark.asyncio
async def test_get_template_id_by_name_from_store(mock_db):
    """store 应根据 file_name 查询 id。"""
    mock_db.fetch_one.return_value = {"id": 123}
    store = AssetUploadRecordStore(mock_db)

    result = await store.get_template_id_by_name("report.pdf")

    assert result == 123
    mock_db.fetch_one.assert_awaited_once()
    query, params = mock_db.fetch_one.call_args[0]
    assert (
        "SELECT id FROM swe_asset_upload_record WHERE file_name = %s" in query
    )
    assert params == ("report.pdf",)


@pytest.mark.asyncio
async def test_get_template_id_by_name_returns_none_when_not_found(mock_db):
    """store 查不到时应返回 None。"""
    mock_db.fetch_one.return_value = None
    store = AssetUploadRecordStore(mock_db)

    result = await store.get_template_id_by_name("notexist.pdf")

    assert result is None


@pytest.mark.asyncio
async def test_search_template_id_from_service(mock_db):
    """service 找到时应返回 200 + templateId。"""
    mock_db.fetch_one.return_value = {"id": 456}
    store = AssetUploadRecordStore(mock_db)

    from swe.app.asset_upload_record.service import AssetUploadRecordService

    service = AssetUploadRecordService(store)
    result = await service.search_template_id("data.xlsx")

    assert isinstance(result, TemplateSearchResponse)
    assert result.code == 200
    assert result.error is None
    assert result.data == 456


@pytest.mark.asyncio
async def test_search_template_id_not_found_from_service(mock_db):
    """service 找不到时应返回 404。"""
    mock_db.fetch_one.return_value = None
    store = AssetUploadRecordStore(mock_db)

    from swe.app.asset_upload_record.service import AssetUploadRecordService

    service = AssetUploadRecordService(store)
    result = await service.search_template_id("missing.pdf")

    assert result.code == 404
    assert result.error == "Template not found"
    assert result.data is None


def test_search_template_route_returns_id(monkeypatch):
    """GET /template/search?templateName=xxx 应返回 templateId。"""

    class _FakeService:
        async def search_template_id(self, template_name):
            assert template_name == "report.pdf"
            return TemplateSearchResponse(data=123)

    app = FastAPI()
    app.include_router(upload_record_router)
    monkeypatch.setattr(
        upload_record_router_module,
        "_service",
        _FakeService(),
    )

    client = TestClient(app)
    response = client.get(
        "/template/search",
        params={"templateName": "report.pdf"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    assert payload["error"] is None
    assert payload["data"] == 123


def test_search_template_route_returns_503_when_not_initialized():
    """模块未初始化时 GET /template/search 应返回 503。"""
    app = FastAPI()
    app.include_router(upload_record_router)

    importlib.reload(upload_record_router_module)

    client = TestClient(app)
    response = client.get(
        "/template/search",
        params={"templateName": "test.pdf"},
    )

    assert response.status_code == 503


# ---- /result 接口测试 ----


def test_result_route_returns_data(monkeypatch):
    """POST /template/result 应返回外部 API 的结果。"""

    class _FakeService:
        async def query_template_result(self, result_id, template_id):
            assert result_id == "12345"
            assert template_id == 1
            return TemplateResultResponse(
                code="200",
                message="OK",
                result=False,
                data={"TODAY_COUNT": "5", "TOTAL_AMOUNT": "295.15"},
            )

    app = FastAPI()
    app.include_router(upload_record_router)
    monkeypatch.setattr(
        upload_record_router_module,
        "_service",
        _FakeService(),
    )

    client = TestClient(app)
    response = client.post(
        "/template/result",
        json={"resultId": "12345", "templateId": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == "200"
    assert payload["message"] == "OK"
    assert payload["result"] is False
    assert payload["data"]["TODAY_COUNT"] == "5"
    assert payload["data"]["TOTAL_AMOUNT"] == "295.15"


def test_result_route_returns_503_when_not_initialized():
    """模块未初始化时 POST /template/result 应返回 503。"""
    app = FastAPI()
    app.include_router(upload_record_router)

    importlib.reload(upload_record_router_module)

    client = TestClient(app)
    response = client.post(
        "/template/result",
        json={"resultId": "12345", "templateId": 1},
    )

    assert response.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("insert_id", [41, 0, None])
async def test_insert_record_uses_facade_insert_id(insert_id):
    db = MagicMock(spec=["is_connected", "insert"])
    db.is_connected = True
    db.insert = AsyncMock(return_value=insert_id)

    result = await AssetUploadRecordStore(db).insert_record(
        file_name="客户资料.xlsx",
        file_size=123,
        asset_path="assets/file.xlsx",
        source_id="source-a",
        template_flag="template-a",
    )

    assert result == (insert_id or None)
    query, params = db.insert.await_args.args
    assert "ON DUPLICATE KEY UPDATE" in query
    assert params == (
        "客户资料.xlsx",
        123,
        "assets/file.xlsx",
        "source-a",
        "template-a",
    )
