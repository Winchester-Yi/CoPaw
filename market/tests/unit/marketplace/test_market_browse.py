# -*- coding: utf-8 -*-
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock


def _make_app(tmp_path):
    from market.app.routers.market_browse import router
    from market.app.routers.mcp_browse import router as mcp_router
    from market.database.connection import DatabaseConnection
    from market.marketplace.service import MarketplaceService

    mock_db = AsyncMock(spec=DatabaseConnection)
    mock_db.is_connected = True
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": 1,
                "source_id": "src_a",
                "name": "业务",
                "sort_order": 0,
                "branch_visible": 1,
            },
            {
                "id": 2,
                "source_id": "src_a",
                "name": "工具",
                "sort_order": 1,
                "branch_visible": 0,
            },
        ],
    )
    mock_db.fetch_one = AsyncMock(
        return_value={"call_count": 0, "user_count": 0},
    )
    svc = MarketplaceService(
        db=mock_db,
        marketplace_root=tmp_path / "market",
        swe_root=tmp_path / "swe",
    )
    app = FastAPI()
    app.state.marketplace = svc
    app.include_router(router, prefix="/api")
    app.include_router(mcp_router, prefix="/api")
    return app


def _publish(svc, name, category_id, bbk_ids):
    from market.marketplace.schemas import PublishSkillRequest

    asyncio.run(
        svc.publish_skill(
            "src_a",
            PublishSkillRequest(
                name=name,
                description="desc",
                creator_id="u1",
                creator_name="User",
                skill_json={},
                skill_md="",
                category_id=category_id,
                bbk_ids=bbk_ids,
            ),
        ),
    )


def test_head_office_browse_facets_use_the_same_resource_set(tmp_path):
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    _publish(svc, "hq-global", 1, [])
    _publish(svc, "hq-explicit", 1, ["100"])
    _publish(svc, "branch-110", 1, ["110"])
    _publish(svc, "branch-120", 2, ["120"])
    _publish(svc, "hidden-110", 2, ["110"])

    response = TestClient(app).get(
        "/api/market/browse?resource_type=skill",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 5
    assert {item["name"] for item in data["items"]} == {
        "hq-global",
        "hq-explicit",
        "branch-110",
        "branch-120",
        "hidden-110",
    }
    assert {item["id"]: item["count"] for item in data["categories"]} == {
        1: 3,
        2: 2,
    }
    assert {item["bbk_id"]: item["count"] for item in data["branches"]} == {
        "100": 2,
        "110": 2,
        "120": 1,
    }


def test_browse_can_select_uncategorized_resources(tmp_path):
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    _publish(svc, "categorized", 1, [])
    _publish(svc, "uncategorized-hq", None, [])
    _publish(svc, "uncategorized-branch", None, ["110"])

    response = TestClient(app).get(
        "/api/market/browse?resource_type=skill&uncategorized=true",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert {item["name"] for item in data["items"]} == {
        "uncategorized-hq",
        "uncategorized-branch",
    }
    assert {item["id"]: item["count"] for item in data["categories"]} == {
        1: 1,
        2: 0,
        -1: 2,
    }


def test_browse_exposes_orphaned_category_and_keeps_facet_totals_consistent(
    tmp_path,
):
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    _publish(svc, "categorized", 1, [])
    _publish(svc, "uncategorized", None, [])
    _publish(svc, "orphaned", 17, [])

    response = TestClient(app).get(
        "/api/market/browse?resource_type=skill",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert sum(category["count"] for category in data["categories"]) == 3
    assert {item["id"]: item["count"] for item in data["categories"]} == {
        1: 1,
        2: 0,
        -1: 1,
        -2: 1,
    }

    orphaned_response = TestClient(app).get(
        "/api/market/browse?resource_type=skill&orphaned=true",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )
    assert orphaned_response.json()["total"] == 1
    assert [item["name"] for item in orphaned_response.json()["items"]] == [
        "orphaned",
    ]


def test_orphaned_and_uncategorized_filters_are_disjoint(tmp_path):
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    _publish(svc, "uncategorized", None, [])
    _publish(svc, "orphaned", 17, [])

    uncategorized_response = TestClient(app).get(
        "/api/market/browse?resource_type=skill&uncategorized=true",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )
    orphaned_response = TestClient(app).get(
        "/api/market/browse?resource_type=skill&orphaned=true",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )

    assert [
        item["name"] for item in uncategorized_response.json()["items"]
    ] == [
        "uncategorized",
    ]
    assert [item["name"] for item in orphaned_response.json()["items"]] == [
        "orphaned",
    ]


def test_orphaned_count_stays_consistent_when_branch_is_selected(tmp_path):
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    _publish(svc, "categorized", 1, ["110"])
    _publish(svc, "uncategorized", None, ["110"])
    _publish(svc, "orphaned", 17, ["110"])

    response = TestClient(app).get(
        "/api/market/browse?resource_type=skill&bbk_id=110",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert data["category_total"] == 3
    assert sum(category["count"] for category in data["categories"]) == 3
    assert {item["id"]: item["count"] for item in data["categories"]} == {
        1: 1,
        2: 0,
        -1: 1,
        -2: 1,
    }


def test_head_office_branch_selection_excludes_head_office_resources(tmp_path):
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    _publish(svc, "hq-global", 1, [])
    _publish(svc, "hq-explicit", 1, ["100"])
    _publish(svc, "branch-110", 1, ["110"])
    _publish(svc, "branch-120", 2, ["120"])

    response = TestClient(app).get(
        "/api/market/browse?resource_type=skill&bbk_id=110",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert [item["name"] for item in data["items"]] == ["branch-110"]
    assert {item["id"]: item["count"] for item in data["categories"]} == {
        1: 1,
        2: 0,
    }
    assert {item["bbk_id"]: item["count"] for item in data["branches"]} == {
        "100": 2,
        "110": 1,
        "120": 1,
    }
    assert data["category_total"] == 1
    assert data["branch_total"] == 4


def test_branch_user_cannot_see_hidden_category_and_facets_follow_category_filter(
    tmp_path,
):
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    _publish(svc, "hq-global", 1, [])
    _publish(svc, "branch-110", 1, ["110"])
    _publish(svc, "hidden-110", 2, ["110"])
    _publish(svc, "orphaned-110", 17, ["110"])
    _publish(svc, "other-branch", 1, ["120"])

    response = TestClient(app).get(
        "/api/market/browse?resource_type=skill&category_id=1",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "110"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert {item["name"] for item in data["items"]} == {
        "hq-global",
        "branch-110",
    }
    assert {item["id"]: item["count"] for item in data["categories"]} == {1: 2}
    assert (
        sum(category["count"] for category in data["categories"])
        == data["total"]
    )
    assert {item["bbk_id"]: item["count"] for item in data["branches"]} == {
        "100": 1,
        "110": 1,
    }


def test_branch_facet_keeps_visible_branches_with_zero_count(tmp_path):
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    _publish(svc, "branch-110-cat-1", 1, ["110"])
    _publish(svc, "branch-120-cat-2", 2, ["120"])

    response = TestClient(app).get(
        "/api/market/browse?resource_type=skill&category_id=1",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert {item["bbk_id"]: item["count"] for item in data["branches"]} == {
        "100": 0,
        "110": 1,
        "120": 0,
    }


def test_branch_user_cannot_open_hidden_mcp_category_detail(tmp_path):
    from market.marketplace.schemas import PublishMCPRequest

    app = _make_app(tmp_path)
    item, _ = asyncio.run(
        app.state.marketplace.publish_mcp(
            "src_a",
            PublishMCPRequest(
                client_key="hidden-mcp",
                name="hidden-mcp",
                creator_id="u1",
                category_id=2,
                bbk_ids=["110"],
                config={"command": "echo"},
            ),
        ),
    )
    app.state.marketplace.db.fetch_all = AsyncMock(return_value=[{"id": 1}])

    response = TestClient(app).get(
        f"/api/market/mcp/{item.item_id}",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "110"},
    )

    assert response.status_code == 404


def test_branch_user_selecting_head_office_only_returns_head_office_resources(
    tmp_path,
):
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    _publish(svc, "hq-global", 1, [])
    _publish(svc, "hq-explicit", 1, ["100"])
    _publish(svc, "branch-110", 1, ["110"])

    response = TestClient(app).get(
        "/api/market/browse?resource_type=skill&bbk_id=100",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "110"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert {item["name"] for item in data["items"]} == {
        "hq-global",
        "hq-explicit",
    }


def test_mcp_browse_does_not_expose_skill_categories(tmp_path):
    from market.marketplace.fs import save_index
    from market.marketplace.models import MarketItem

    app = _make_app(tmp_path)
    save_index(
        app.state.marketplace.marketplace_root,
        "src_a",
        [
            MarketItem(
                item_id="mcp-hq",
                item_type="mcp",
                name="mcp-hq",
                creator_id="u1",
                category_id=1,
                bbk_ids=[],
            ),
            MarketItem(
                item_id="mcp-branch",
                item_type="mcp",
                name="mcp-branch",
                creator_id="u1",
                category_id=1,
                bbk_ids=["110"],
            ),
            MarketItem(
                item_id="mcp-hidden",
                item_type="mcp",
                name="mcp-hidden",
                creator_id="u1",
                category_id=2,
                bbk_ids=["110"],
            ),
        ],
    )

    response = TestClient(app).get(
        "/api/market/browse?resource_type=mcp",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "110"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert {item["name"] for item in data["items"]} == {
        "mcp-hq",
        "mcp-branch",
        "mcp-hidden",
    }
    assert data["categories"] == []
    assert {item["bbk_id"]: item["count"] for item in data["branches"]} == {
        "100": 1,
        "110": 2,
    }


def test_mcp_browse_does_not_expose_uncategorized_skill_category(tmp_path):
    from market.marketplace.fs import save_index
    from market.marketplace.models import MarketItem

    app = _make_app(tmp_path)
    save_index(
        app.state.marketplace.marketplace_root,
        "src_a",
        [
            MarketItem(
                item_id="mcp-categorized",
                item_type="mcp",
                name="mcp-categorized",
                creator_id="u1",
                category_id=1,
                bbk_ids=[],
            ),
            MarketItem(
                item_id="mcp-uncategorized",
                item_type="mcp",
                name="mcp-uncategorized",
                creator_id="u1",
                category_id=None,
                bbk_ids=[],
            ),
        ],
    )

    response = TestClient(app).get(
        "/api/market/browse?resource_type=mcp",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert {item["name"] for item in data["items"]} == {
        "mcp-categorized",
        "mcp-uncategorized",
    }
    assert data["categories"] == []


def test_mcp_browse_does_not_expose_orphaned_skill_category(tmp_path):
    from market.marketplace.fs import save_index
    from market.marketplace.models import MarketItem

    app = _make_app(tmp_path)
    save_index(
        app.state.marketplace.marketplace_root,
        "src_a",
        [
            MarketItem(
                item_id="mcp-orphaned",
                item_type="mcp",
                name="mcp-orphaned",
                creator_id="u1",
                category_id=17,
                bbk_ids=[],
            ),
        ],
    )

    response = TestClient(app).get(
        "/api/market/browse?resource_type=mcp",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert [item["name"] for item in data["items"]] == ["mcp-orphaned"]
    assert data["categories"] == []


def test_selected_facets_keep_independent_all_totals(tmp_path):
    app = _make_app(tmp_path)
    svc = app.state.marketplace
    _publish(svc, "hq-cat-1", 1, [])
    _publish(svc, "branch-cat-1", 1, ["110"])
    _publish(svc, "branch-cat-2", 2, ["110"])

    response = TestClient(app).get(
        "/api/market/browse?resource_type=skill&category_id=1&bbk_id=110",
        headers={"X-Source-Id": "src_a", "X-Bbk-Id": "100"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["category_total"] == 2
    assert data["branch_total"] == 2
