# -*- coding: utf-8 -*-
"""Tests for internal text asset APIs."""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import swe.app._app as app_module
import swe.app.auth as auth_module
import swe.constant as constant_module
from swe.app.routers import internal as internal_module
from swe.app.routers.internal import public_router, router
from swe.config.context import encode_scope_id


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.include_router(public_router)
    app.state.multi_agent_manager = SimpleNamespace(
        reload_agent=AsyncMock(return_value=True),
    )
    return TestClient(app)


def _set_working_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        internal_module,
        "WORKING_DIR",
        tmp_path,
        raising=False,
    )


def _set_app_working_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        app_module,
        "WORKING_DIR",
        tmp_path,
        raising=False,
    )
    monkeypatch.setattr(
        constant_module,
        "WORKING_DIR",
        tmp_path,
        raising=False,
    )


def test_internal_text_asset_read_returns_404_for_missing_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    client = _build_client()

    response = client.get("/internal/assets/text/read?file_name=guide.txt")

    assert response.status_code == 404
    assert response.json()["detail"] == "Asset file not found"


def test_internal_text_asset_read_rejects_invalid_file_name(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    client = _build_client()

    response = client.get("/internal/assets/text/read?file_name=../guide.txt")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid file_name"


def test_internal_text_asset_read_rejects_invalid_utf8(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    asset_dir = tmp_path / "asset"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "broken.txt").write_bytes(b"\xff\xfe\xfd")
    client = _build_client()

    response = client.get("/internal/assets/text/read?file_name=broken.txt")

    assert response.status_code == 400
    assert response.json()["detail"] == "Asset file is not valid UTF-8"


def test_internal_text_asset_read_returns_utf8_content(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    asset_dir = tmp_path / "asset"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "guide.txt").write_text("hello\nworld", encoding="utf-8")
    client = _build_client()

    response = client.get("/internal/assets/text/read?file_name=guide.txt")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "file_name": "guide.txt",
        "content": "hello\nworld",
    }


def test_internal_text_asset_write_creates_scope_static_file_and_public_url(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("FILE_URL", "https://files.example")
    client = _build_client()
    scope_id = encode_scope_id("alice", "portal")

    response = client.post(
        "/internal/assets/text/write",
        json={
            "user_id": "alice",
            "source_id": "portal",
            "content": "<p>hello</p>",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["scope_id"] == scope_id
    assert re.match(r"^alice-\d{17}\.html$", payload["file_name"])
    assert payload["public_url"] == (
        f"https://files.example/static/{scope_id}/default/"
        f"{payload['file_name']}"
    )

    stored_file = (
        tmp_path
        / scope_id
        / "workspaces"
        / "default"
        / "static"
        / payload["file_name"]
    )
    assert stored_file.read_text(encoding="utf-8") == "<p>hello</p>"


def test_internal_text_asset_write_rejects_invalid_utf8_payload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    client = _build_client()

    response = client.post(
        "/internal/assets/text/write",
        content=(
            b'{"user_id":"alice","source_id":"portal",' b'"content":"\\ud800"}'
        ),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Content is not valid UTF-8"


def test_public_text_asset_read_returns_utf8_content_without_internal_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    asset_dir = tmp_path / "asset"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "public.txt").write_text("public text", encoding="utf-8")
    client = _build_client()

    response = client.get("/assets/text/read?file_name=public.txt")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "file_name": "public.txt",
        "content": "public text",
    }


def test_public_text_asset_write_creates_scope_static_file_without_internal_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("FILE_URL", "https://files.example")
    client = _build_client()
    scope_id = encode_scope_id("guest", "portal")

    response = client.post(
        "/assets/text/write",
        json={
            "user_id": "guest",
            "source_id": "portal",
            "content": "<p>public write</p>",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope_id"] == scope_id
    assert payload["public_url"] == (
        f"https://files.example/static/{scope_id}/default/"
        f"{payload['file_name']}"
    )
    assert (
        tmp_path
        / scope_id
        / "workspaces"
        / "default"
        / "static"
        / payload["file_name"]
    ).read_text(encoding="utf-8") == "<p>public write</p>"


def test_main_app_public_text_asset_read_skips_tenant_headers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    _set_app_working_dir(monkeypatch, tmp_path)
    asset_dir = tmp_path / "asset"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "public.txt").write_text("public text", encoding="utf-8")

    with TestClient(
        app_module.app,
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/api/assets/text/read?file_name=public.txt")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "file_name": "public.txt",
        "content": "public text",
    }


def test_main_app_public_text_asset_write_skips_tenant_headers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    _set_app_working_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("FILE_URL", "https://files.example")
    scope_id = encode_scope_id("guest", "portal")

    with TestClient(
        app_module.app,
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/api/assets/text/write",
            json={
                "user_id": "guest",
                "source_id": "portal",
                "content": "<p>public write</p>",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope_id"] == scope_id
    assert payload["public_url"] == (
        f"https://files.example/static/{scope_id}/default/"
        f"{payload['file_name']}"
    )
    assert (
        tmp_path
        / scope_id
        / "workspaces"
        / "default"
        / "static"
        / payload["file_name"]
    ).read_text(encoding="utf-8") == "<p>public write</p>"


def test_main_app_static_html_returns_text_html_content_type(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_app_working_dir(monkeypatch, tmp_path)
    static_file = (
        tmp_path / "mimecheck" / "workspaces" / "default" / "static" / "x.html"
    )
    static_file.parent.mkdir(parents=True, exist_ok=True)
    static_file.write_text("<p>mime ok</p>", encoding="utf-8")

    with TestClient(
        app_module.app,
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/static/mimecheck/default/x.html")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"].lower()


def test_main_app_public_text_asset_read_skips_auth_when_enabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _set_working_dir(monkeypatch, tmp_path)
    _set_app_working_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(auth_module, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth_module, "has_registered_users", lambda: True)
    asset_dir = tmp_path / "asset"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "public.txt").write_text("public text", encoding="utf-8")

    with TestClient(
        app_module.app,
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/api/assets/text/read?file_name=public.txt")

    assert response.status_code == 200
    assert response.json()["content"] == "public text"
