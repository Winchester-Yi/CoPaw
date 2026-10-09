# -*- coding: utf-8 -*-
"""市场技能详情文件预览测试."""

import asyncio
import json
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_service(tmp_path):
    from market.database.connection import DatabaseConnection
    from market.marketplace.service import MarketplaceService

    mock_db = AsyncMock(spec=DatabaseConnection)
    mock_db.is_connected = True
    mock_db.execute = AsyncMock(return_value=1)
    svc = MarketplaceService(
        db=mock_db,
        marketplace_root=tmp_path / "market",
        swe_root=tmp_path / "swe",
    )
    svc.market_skill_registry.upsert_market_skill = AsyncMock(
        return_value=True,
    )
    return svc


def _make_app(tmp_path):
    from market.app.routers.skills_browse import router

    app = FastAPI()
    app.state.marketplace = _make_service(tmp_path)
    app.include_router(router, prefix="/api")
    return app


def _publish_skill(svc, source_id: str, name: str):
    from market.marketplace.schemas import PublishSkillRequest

    req = PublishSkillRequest(
        name=name,
        description="预览测试技能",
        creator_id="u1",
        creator_name="User One",
        skill_json={
            "name": name,
            "description": "预览测试技能",
        },
        skill_md="# 使用说明\n\n这是技能说明。",
    )
    item, _ = asyncio.run(svc.publish_skill(source_id, req))
    return item


def test_list_market_skill_files_hides_skill_json_and_contains_nested_files(
    tmp_path,
):
    from market.marketplace.fs import get_skill_dir

    svc = _make_service(tmp_path)
    item = _publish_skill(svc, "src_a", "preview_skill")
    skill_dir = get_skill_dir(tmp_path / "market", "src_a", item.item_id)
    (skill_dir / "docs").mkdir(parents=True)
    (skill_dir / "docs" / "guide.md").write_text(
        "# Guide\n\nhello",
        encoding="utf-8",
    )

    files = svc.list_market_skill_files("src_a", item.item_id, "100")

    names = [node["name"] for node in files]
    assert "SKILL.md" in names
    assert "skill.json" not in names
    docs_node = next(node for node in files if node["name"] == "docs")
    assert docs_node["type"] == "directory"
    assert docs_node["children"][0]["name"] == "guide.md"


def test_market_skill_preview_routes_return_tree_and_content(tmp_path):
    from market.marketplace.fs import get_skill_dir

    app = _make_app(tmp_path)
    item = _publish_skill(app.state.marketplace, "src_a", "preview_skill")
    skill_dir = get_skill_dir(tmp_path / "market", "src_a", item.item_id)
    (skill_dir / "assets").mkdir(parents=True)
    (skill_dir / "assets" / "logo.png").write_bytes(b"binary-image-data")

    client = TestClient(app)
    headers = {"X-Source-Id": "src_a", "X-Bbk-Id": "100"}

    tree_resp = client.get(
        f"/api/market/skills/{item.item_id}/files",
        headers=headers,
    )
    assert tree_resp.status_code == 200
    tree_names = [node["name"] for node in tree_resp.json()]
    assert "SKILL.md" in tree_names
    assert "skill.json" not in tree_names

    read_md_resp = client.get(
        f"/api/market/skills/{item.item_id}/files/SKILL.md",
        headers=headers,
    )
    assert read_md_resp.status_code == 200
    assert read_md_resp.json() == {
        "content": "# 使用说明\n\n这是技能说明。",
        "file_type": "markdown",
    }

    read_binary_resp = client.get(
        f"/api/market/skills/{item.item_id}/files/assets/logo.png",
        headers=headers,
    )
    assert read_binary_resp.status_code == 200
    assert read_binary_resp.json() == {
        "content": "",
        "file_type": "binary",
    }


def test_disabled_my_skill_file_routes_and_download_use_registered_package(
    tmp_path,
):
    from market.marketplace.fs import (
        get_user_disabled_skills_dir,
        get_user_skill_manifest_path,
    )

    user_id = "user-1"
    source_id = "src_a"
    skill_name = "disabled_skill"
    skill_dir = (
        get_user_disabled_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / skill_name
    )
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Disabled Skill\n\ncontents",
        encoding="utf-8",
    )
    manifest_path = get_user_skill_manifest_path(
        tmp_path / "swe",
        user_id,
        source_id=source_id,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-skill-manifest.v1",
                "layout_version": 2,
                "version": 1,
                "skills": {
                    skill_name: {
                        "enabled": False,
                        "source": "customized",
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    app = _make_app(tmp_path)
    client = TestClient(app)
    headers = {"X-Source-Id": source_id, "X-User-Id": user_id}

    tree_resp = client.get(
        f"/api/market/skills/mine/{skill_name}/files",
        headers=headers,
    )
    assert tree_resp.status_code == 200
    assert [node["name"] for node in tree_resp.json()] == ["SKILL.md"]

    read_resp = client.get(
        f"/api/market/skills/mine/{skill_name}/files/SKILL.md",
        headers=headers,
    )
    assert read_resp.status_code == 200
    assert read_resp.json() == {
        "content": "# Disabled Skill\n\ncontents",
        "file_type": "markdown",
    }

    download_resp = client.get(
        f"/api/market/skills/mine/{skill_name}/download",
        headers=headers,
    )
    assert download_resp.status_code == 200
    assert download_resp.content


def test_unregistered_active_my_skill_file_routes_use_active_package(tmp_path):
    from market.marketplace.fs import get_user_skills_dir

    user_id = "user-1"
    source_id = "src_a"
    skill_name = "legacy_skill"
    skill_dir = (
        get_user_skills_dir(
            tmp_path / "swe",
            user_id,
            source_id=source_id,
        )
        / skill_name
    )
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Legacy Skill\n\ncontents",
        encoding="utf-8",
    )
    (skill_dir / "notes.txt").write_text("before", encoding="utf-8")

    app = _make_app(tmp_path)
    client = TestClient(app)
    headers = {"X-Source-Id": source_id, "X-User-Id": user_id}

    tree_resp = client.get(
        f"/api/market/skills/mine/{skill_name}/files",
        headers=headers,
    )
    assert tree_resp.status_code == 200
    assert {node["name"] for node in tree_resp.json()} == {
        "SKILL.md",
        "notes.txt",
    }

    read_resp = client.get(
        f"/api/market/skills/mine/{skill_name}/files/notes.txt",
        headers=headers,
    )
    assert read_resp.status_code == 200
    assert read_resp.json()["content"] == "before"

    save_resp = client.put(
        f"/api/market/skills/mine/{skill_name}/files/notes.txt",
        headers=headers,
        json={"content": "after"},
    )
    assert save_resp.status_code == 200
    assert (skill_dir / "notes.txt").read_text(encoding="utf-8") == "after"

    download_resp = client.get(
        f"/api/market/skills/mine/{skill_name}/download",
        headers=headers,
    )
    assert download_resp.status_code == 200
    assert download_resp.content
