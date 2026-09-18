# -*- coding: utf-8 -*-
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient


def _make_app(mock_db):
    from fastapi import FastAPI
    from market.app.routers.categories import router
    from market.app.deps import get_db

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: mock_db
    return app


def test_get_categories_returns_list():
    mock_db = AsyncMock()
    mock_db.is_connected = True
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": 1,
                "source_id": "src_a",
                "name": "数据分析",
                "sort_order": 0,
                "branch_visible": 1,
                "skill_count": 2,
                "created_at": None,
            },
            {
                "id": 2,
                "source_id": "src_a",
                "name": "报表",
                "sort_order": 1,
                "branch_visible": 0,
                "skill_count": 0,
                "created_at": None,
            },
        ],
    )
    app = _make_app(mock_db)
    client = TestClient(app)
    response = client.get(
        "/api/market/categories",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["name"] == "数据分析"
    assert data[0]["branch_visible"] is True
    assert data[0]["skill_count"] == 2


def test_get_categories_missing_source_id_returns_400():
    mock_db = AsyncMock()
    mock_db.is_connected = True
    app = _make_app(mock_db)
    client = TestClient(app)
    response = client.get("/api/market/categories")
    assert response.status_code == 400


def test_get_categories_db_not_connected_returns_503():
    mock_db = MagicMock()
    mock_db.is_connected = False
    app = _make_app(mock_db)
    client = TestClient(app)
    response = client.get(
        "/api/market/categories",
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )
    assert response.status_code == 503


def test_create_category_success():
    """测试成功创建分类."""
    mock_db = AsyncMock()
    mock_db.is_connected = True

    # 第一次 fetch_one: 检查同名（返回 None 表示无重复）
    # 第二次 fetch_one: 获取 max sort_order
    # 第三次 fetch_one: 插入后查询新记录
    mock_db.fetch_one = AsyncMock(
        side_effect=[
            None,  # 无同名分类
            {"max_order": 2},  # 当前最大 sort_order
            {
                "id": 3,
                "source_id": "src_a",
                "name": "新分类",
                "sort_order": 3,
                "branch_visible": 1,
                "skill_count": 0,
                "created_at": "2025-01-01T00:00:00",
            },
        ],
    )
    mock_db.execute = AsyncMock(return_value=1)

    app = _make_app(mock_db)
    client = TestClient(app)
    response = client.post(
        "/api/market/categories",
        json={"name": "新分类"},
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["id"] == 3
    assert data["name"] == "新分类"
    assert data["sort_order"] == 3
    assert data["branch_visible"] is True


def test_branch_user_only_sees_branch_visible_categories():
    mock_db = AsyncMock()
    mock_db.is_connected = True
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": 1,
                "source_id": "src_a",
                "name": "业务技能",
                "sort_order": 0,
                "branch_visible": 1,
                "skill_count": 1,
                "created_at": None,
            },
        ],
    )
    app = _make_app(mock_db)
    client = TestClient(app)

    response = client.get(
        "/api/market/categories",
        headers={
            "X-Source-Id": "src_a",
            "X-Bbk-Id": "110",
            "X-User-Role": "admin",
        },
    )

    assert response.status_code == 200
    sql = mock_db.fetch_all.call_args.args[0]
    assert "COALESCE(c.branch_visible, 1) = 1" in sql


def test_head_office_sees_hidden_categories_regardless_of_role():
    mock_db = AsyncMock()
    mock_db.is_connected = True
    mock_db.fetch_all = AsyncMock(return_value=[])
    app = _make_app(mock_db)
    client = TestClient(app)

    response = client.get(
        "/api/market/categories",
        headers={
            "X-Source-Id": "src_a",
            "X-Bbk-Id": "100",
            "X-User-Role": "user",
        },
    )

    assert response.status_code == 200
    sql = mock_db.fetch_all.call_args.args[0]
    assert "WHERE c.source_id = %s ORDER BY" in sql


def test_update_category_supports_name_and_visibility():
    mock_db = AsyncMock()
    mock_db.is_connected = True
    mock_db.fetch_one = AsyncMock(
        side_effect=[
            None,
            {
                "id": 1,
                "source_id": "src_a",
                "name": "工具技能",
                "sort_order": 0,
                "branch_visible": 0,
                "skill_count": 0,
                "created_at": None,
            },
        ],
    )
    app = _make_app(mock_db)
    client = TestClient(app)

    response = client.patch(
        "/api/market/categories/1",
        json={"name": "工具技能", "branch_visible": False},
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )

    assert response.status_code == 200
    assert response.json()["branch_visible"] is False


def test_reorder_categories_updates_all_sort_orders():
    mock_db = AsyncMock()
    mock_db.is_connected = True
    mock_db.execute_many = AsyncMock(return_value=3)
    app = _make_app(mock_db)
    client = TestClient(app)

    response = client.put(
        "/api/market/categories/reorder",
        json={"category_ids": [3, 1, 2]},
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert mock_db.execute_many.call_args.args[1] == [
        (0, 3, "src_a"),
        (1, 1, "src_a"),
        (2, 2, "src_a"),
    ]


def test_delete_category_with_skills_returns_409():
    mock_db = AsyncMock()
    mock_db.is_connected = True
    mock_db.fetch_one = AsyncMock(return_value={"skill_count": 2})
    app = _make_app(mock_db)
    client = TestClient(app)

    response = client.delete(
        "/api/market/categories/1",
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["skill_count"] == 2
    mock_db.execute.assert_not_called()


def test_delete_empty_category_returns_204():
    mock_db = AsyncMock()
    mock_db.is_connected = True
    mock_db.fetch_one = AsyncMock(return_value={"skill_count": 0})
    app = _make_app(mock_db)
    client = TestClient(app)

    response = client.delete(
        "/api/market/categories/1",
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )

    assert response.status_code == 204
    mock_db.execute.assert_awaited_once()


def test_create_category_duplicate_name_returns_409():
    """测试重复分类名返回 409."""
    mock_db = AsyncMock()
    mock_db.is_connected = True
    mock_db.fetch_one = AsyncMock(return_value={"id": 1})  # 同名已存在

    app = _make_app(mock_db)
    client = TestClient(app)
    response = client.post(
        "/api/market/categories",
        json={"name": "数据分析"},
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )
    assert response.status_code == 409
    assert "已存在" in response.json()["detail"]


def test_create_category_missing_source_id_returns_400():
    """测试缺少 source_id 返回 400."""
    mock_db = AsyncMock()
    mock_db.is_connected = True

    app = _make_app(mock_db)
    client = TestClient(app)
    response = client.post(
        "/api/market/categories",
        json={"name": "新分类"},
    )
    assert response.status_code == 400


def test_create_category_empty_name_returns_400():
    """测试空分类名返回 400."""
    mock_db = AsyncMock()
    mock_db.is_connected = True

    app = _make_app(mock_db)
    client = TestClient(app)
    response = client.post(
        "/api/market/categories",
        json={"name": "   "},
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )
    assert response.status_code == 400


def test_create_category_db_not_connected_returns_503():
    """测试数据库不可用返回 503."""
    mock_db = MagicMock()
    mock_db.is_connected = False

    app = _make_app(mock_db)
    client = TestClient(app)
    response = client.post(
        "/api/market/categories",
        json={"name": "新分类"},
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )
    assert response.status_code == 503
