# -*- coding: utf-8 -*-
"""持续治理操作边界双写测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from swe.app.routers.dream_logs import (
    ARCHIVE_INDEX_FILE,
    _dual_write_workspace_governance_records,
    _scan_orphan_files,
    router,
)
from swe.config.context import encode_scope_id


class _FakeGovernanceStore:
    """为归档清理测试提供文件治理读模型。"""

    def __init__(self, service: "_FakeGovernanceService") -> None:
        self.service = service
        self.is_available = True

    async def list_archive_items(self, source_id: str, **filters):
        """按路由传入的过滤条件返回归档文件状态。"""
        rows = [
            row
            for row in self.service.archive_records
            if row.source_id == source_id
        ]
        target_user_ids = filters.get("target_user_ids")
        if target_user_ids is not None:
            rows = [
                row for row in rows if row.target_user_id in target_user_ids
            ]
        target_agent_id = filters.get("target_agent_id")
        if target_agent_id:
            rows = [
                row for row in rows if row.target_agent_id == target_agent_id
            ]
        return rows


class _FakeGovernanceService:
    """记录路由触发的数据库读模型写入。"""

    def __init__(self) -> None:
        self.archive_records: list[SimpleNamespace] = []
        self.store = _FakeGovernanceStore(self)
        self.fail_archive_upsert = False
        self.fail_rollback = False
        self.fail_protected_upsert = False
        self.rollback_calls: list[dict[str, Any]] = []
        self.archive_calls: list[dict[str, Any]] = []
        self.delete_archive_calls: list[dict[str, Any]] = []
        self.protected_calls: list[dict[str, Any]] = []
        self.delete_protected_calls: list[dict[str, Any]] = []
        self.audit_calls: list[dict[str, Any]] = []
        self.health_calls: list[dict[str, Any]] = []
        self.governance_calls: list[dict[str, Any]] = []

    async def mark_governance_record_rollback(self, **kwargs):
        """记录 rollback 双写。"""
        if self.fail_rollback:
            raise RuntimeError("rollback db timeout")
        self.rollback_calls.append(kwargs)
        return True

    async def upsert_workspace_governance_record_with_health(self, **kwargs):
        """记录治理记录双写。"""
        self.governance_calls.append(kwargs)

    async def upsert_archive_items(self, **kwargs):
        """记录 archive 双写。"""
        if self.fail_archive_upsert:
            raise RuntimeError("archive db timeout")
        self.archive_calls.append(kwargs)

    async def delete_archive_items(self, **kwargs):
        """记录 archive 删除双写。"""
        self.delete_archive_calls.append(kwargs)
        ids = set(kwargs.get("archive_item_ids") or [])
        self.archive_records = [
            row
            for row in self.archive_records
            if not (
                row.source_id == kwargs.get("source_id")
                and row.target_user_id == kwargs.get("target_user_id")
                and row.target_agent_id == kwargs.get("target_agent_id")
                and row.archive_item_id in ids
            )
        ]

    async def upsert_cleanup_audit(self, record):
        """记录 cleanup audit 双写。"""
        self.audit_calls.append(record)

    async def upsert_protected_file(self, **kwargs):
        """记录保护文件双写。"""
        if self.fail_protected_upsert:
            raise RuntimeError("protected db timeout")
        self.protected_calls.append(kwargs)

    async def delete_protected_file(self, **kwargs):
        """记录删除保护文件双写。"""
        self.delete_protected_calls.append(kwargs)
        return True

    async def record_reconcile_health(self, **kwargs):
        """记录对账健康状态。"""
        self.health_calls.append(kwargs)


class _FakeTenantSourceStore:
    """提供当前 source 下的可管理用户。"""

    async def get_by_source(self, source_id: str):
        """返回固定用户集合。"""
        return [
            {
                "tenant_id": "alice",
                "source_id": source_id,
                "tenant_name": "Alice",
                "bbk_id": "bbk-1",
            },
            {
                "tenant_id": "manager",
                "source_id": source_id,
                "tenant_name": "Manager",
                "bbk_id": "bbk-admin",
            },
        ]


def _client(
    tmp_path: Path,
    monkeypatch,
) -> tuple[TestClient, _FakeGovernanceService]:
    """创建带 workspace 和 fake governance service 的测试客户端。"""
    service = _FakeGovernanceService()
    monkeypatch.setattr(
        "swe.app.workspace.tenant_init_source_store"
        ".get_tenant_init_source_store",
        _FakeTenantSourceStore,
    )
    app = FastAPI()
    app.state.continuous_governance_service = service

    @app.middleware("http")
    async def _attach_request_state(request: Request, call_next):
        source_id = request.headers.get("X-Source-Id", "source-a")
        tenant_id = request.headers.get("X-Tenant-Id", "manager")
        scope_id = encode_scope_id(tenant_id, source_id)
        request.state.source_id = source_id
        request.state.tenant_id = tenant_id
        request.state.scope_id = scope_id
        request.state.workspace = SimpleNamespace(
            workspace_dir=tmp_path / scope_id,
        )
        request.state.workspace.workspace_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        return await call_next(request)

    app.include_router(router)
    return TestClient(app), service


def _workspace(tmp_path: Path, tenant_id: str, source_id: str) -> Path:
    """定位测试 workspace。"""
    path = (
        tmp_path
        / encode_scope_id(tenant_id, source_id)
        / "workspaces"
        / "default"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_rollback_updates_database_record(tmp_path, monkeypatch) -> None:
    """rollback 成功更新 workspace 后应更新原 DB 记录。"""
    client, service = _client(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path, "manager", "source-a")
    backup = workspace / "backup" / "memory.md"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text("before", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("after", encoding="utf-8")
    (workspace / "dream_logs.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "id": "record-1",
                        "timestamp": "2026-05-24T09:00:00Z",
                        "trigger": "manual",
                        "status": "success",
                        "file_stats": {
                            "MEMORY.md": {"backup_path": "backup/memory.md"},
                        },
                    },
                ],
                "stats": {},
            },
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/dream-logs/rollback/record-1",
        headers={"X-Source-Id": "source-a", "X-Tenant-Id": "manager"},
    )

    assert response.status_code == 200
    assert service.rollback_calls[0]["source_id"] == "source-a"
    assert service.rollback_calls[0]["target_user_id"] == "manager"
    assert service.rollback_calls[0]["record_id"] == "record-1"


def test_rollback_failure_health_keeps_original_timestamp(
    tmp_path,
    monkeypatch,
) -> None:
    """rollback DB 失败后的待对账 payload 应保留实际 rollback 时间。"""
    client, service = _client(tmp_path, monkeypatch)
    service.fail_rollback = True
    workspace = _workspace(tmp_path, "manager", "source-a")
    backup = workspace / "backup" / "memory.md"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text("before", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("after", encoding="utf-8")
    (workspace / "dream_logs.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "id": "record-1",
                        "timestamp": "2026-05-24T09:00:00Z",
                        "trigger": "manual",
                        "status": "success",
                        "file_stats": {
                            "MEMORY.md": {"backup_path": "backup/memory.md"},
                        },
                    },
                ],
                "stats": {},
            },
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/dream-logs/rollback/record-1",
        headers={"X-Source-Id": "source-a", "X-Tenant-Id": "manager"},
    )

    assert response.status_code == 200
    payload = service.health_calls[0]["payload"]
    assert payload["record_id"] == "record-1"
    assert payload["rollback_timestamp"]
    assert payload["rollback_files"] == ["MEMORY.md"]


def test_archive_orphan_files_writes_archive_state(
    tmp_path,
    monkeypatch,
) -> None:
    """当前 workspace 归档操作应写入文件治理状态。"""
    client, service = _client(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path, "manager", "source-a")
    (workspace / "orphan.txt").write_text("orphan", encoding="utf-8")

    response = client.post(
        "/dream-logs/orphan-files/archive",
        json={"files": ["orphan.txt"], "reason": "manual"},
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    assert service.archive_calls[0]["source_id"] == "source-a"
    assert service.archive_calls[0]["target_user_id"] == "manager"
    assert (
        service.archive_calls[0]["items"][0]["original_path"] == "orphan.txt"
    )


def test_orphan_scan_keeps_system_state_files_and_directories(
    tmp_path,
) -> None:
    workspace = tmp_path
    (workspace / "old.txt").write_text("old", encoding="utf-8")
    (workspace / "system_jobs.json").write_text("{}", encoding="utf-8")

    hook_script = workspace / "hooks" / "scripts" / "guard.py"
    hook_script.parent.mkdir(parents=True)
    hook_script.write_text("print('guard')", encoding="utf-8")

    dialog_manifest = workspace / "dialog" / "chat-1" / "manifest.json"
    dialog_manifest.parent.mkdir(parents=True)
    dialog_manifest.write_text('{"boundaries":[]}', encoding="utf-8")

    orphan_paths = {item.path for item in _scan_orphan_files(workspace)}

    assert orphan_paths == {"old.txt"}


def test_archive_db_failure_records_reconcile_health(
    tmp_path,
    monkeypatch,
) -> None:
    """workspace 归档成功但 DB 写失败时应写入待对账状态。"""
    client, service = _client(tmp_path, monkeypatch)
    service.fail_archive_upsert = True
    workspace = _workspace(tmp_path, "manager", "source-a")
    (workspace / "orphan.txt").write_text("orphan", encoding="utf-8")
    (workspace / "orphan-2.txt").write_text("orphan", encoding="utf-8")

    response = client.post(
        "/dream-logs/orphan-files/archive",
        json={"files": ["orphan.txt", "orphan-2.txt"], "reason": "manual"},
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    assert service.health_calls[0]["entity_type"] == "archive_items"
    assert service.health_calls[0]["entity_id"].startswith("archive_items:")
    assert len(service.health_calls[0]["entity_id"]) <= 128
    assert len(service.health_calls[0]["payload"]["items"]) == 2
    assert "archive db timeout" in service.health_calls[0]["error"]


def test_purge_archive_writes_audit_and_removes_archive_state(
    tmp_path,
    monkeypatch,
) -> None:
    """管理员清理归档应删除 DB 文件状态并写入 cleanup audit。"""
    client, service = _client(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path, "alice", "source-a")
    archive_file = workspace / "governance" / "archive" / "files" / "archive-1"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text("old", encoding="utf-8")
    (workspace / ARCHIVE_INDEX_FILE).parent.mkdir(parents=True, exist_ok=True)
    (workspace / ARCHIVE_INDEX_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "archive-1",
                        "original_path": "old.md",
                        "archive_path": "governance/archive/files/archive-1",
                        "size_bytes": 3,
                        "mtime": "2026-05-24T09:00:00Z",
                        "archived_at": "2026-05-24T10:00:00Z",
                        "archived_by": "admin",
                        "archive_reason": "manual",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    response = client.request(
        "DELETE",
        "/dream-logs/archive/items",
        json={
            "archive_item_ids": ["archive-1"],
            "target_user_id": "alice",
            "target_agent_id": "default",
            "reason": "manual_clear",
        },
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    assert service.delete_archive_calls[0]["archive_item_ids"] == ["archive-1"]
    assert (
        service.audit_calls[0]["event_id"] == response.json()["audit_event_id"]
    )
    assert service.audit_calls[0]["target_user_id"] == "alice"


def test_purge_archive_accepts_post_without_delete_body_dependency(
    tmp_path,
    monkeypatch,
) -> None:
    """单个归档清理可走 POST，避免中间层丢弃 DELETE body。"""
    client, service = _client(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path, "alice", "source-a")
    archive_file = workspace / "governance" / "archive" / "files" / "archive-1"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text("old", encoding="utf-8")
    (workspace / ARCHIVE_INDEX_FILE).parent.mkdir(parents=True, exist_ok=True)
    (workspace / ARCHIVE_INDEX_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "archive-1",
                        "original_path": "old.md",
                        "archive_path": "governance/archive/files/archive-1",
                        "size_bytes": 3,
                        "mtime": "2026-05-24T09:00:00Z",
                        "archived_at": "2026-05-24T10:00:00Z",
                        "archived_by": "admin",
                        "archive_reason": "manual",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/dream-logs/archive/items",
        json={
            "archive_item_ids": ["archive-1"],
            "target_user_id": "alice",
            "target_agent_id": "default",
            "reason": "manual_clear",
        },
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    assert not archive_file.exists()
    assert service.delete_archive_calls[0]["archive_item_ids"] == ["archive-1"]


def test_purge_archive_uses_database_record_when_index_missing(
    tmp_path,
    monkeypatch,
) -> None:
    """单个归档清理应按页面读模型记录补齐本地索引缺失的归档项。"""
    client, service = _client(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path, "alice", "source-a")
    archive_file = (
        workspace / "governance" / "archive" / "files" / "archive-db"
    )
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text("old", encoding="utf-8")
    service.archive_records.append(
        SimpleNamespace(
            source_id="source-a",
            target_user_id="alice",
            target_agent_id="default",
            archive_item_id="archive-db",
            original_path="old.md",
            archive_path="governance/archive/files/archive-db",
            size_bytes=3,
            mtime="2026-05-24T09:00:00Z",
            archived_at="2026-05-24T10:00:00Z",
            archived_by="admin",
            archive_reason="manual",
            raw_item={},
        ),
    )

    response = client.post(
        "/dream-logs/archive/items",
        json={
            "archive_item_ids": ["archive-db"],
            "target_user_id": "alice",
            "target_agent_id": "default",
            "reason": "manual_clear",
        },
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    assert response.json()["files_deleted"] == ["old.md"]
    assert not archive_file.exists()
    assert service.delete_archive_calls[0]["archive_item_ids"] == [
        "archive-db",
    ]
    assert service.archive_records == []


def test_file_governance_rejects_invalid_target_agent_id(
    tmp_path,
    monkeypatch,
) -> None:
    """文件治理操作不得把 target_agent_id 当作未校验路径片段。"""
    client, _ = _client(tmp_path, monkeypatch)

    response = client.request(
        "DELETE",
        "/dream-logs/archive/items",
        json={
            "archive_item_ids": ["archive-1"],
            "target_user_id": "alice",
            "target_agent_id": "../escape",
            "reason": "manual_clear",
        },
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 400


def test_file_governance_rejects_overlong_target_agent_id(
    tmp_path,
    monkeypatch,
) -> None:
    """target_agent_id 不能超过数据库读模型字段长度。"""
    client, _ = _client(tmp_path, monkeypatch)

    response = client.request(
        "DELETE",
        "/dream-logs/archive/items",
        json={
            "archive_item_ids": ["archive-1"],
            "target_user_id": "alice",
            "target_agent_id": "a" * 129,
            "reason": "manual_clear",
        },
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 400


def test_purge_expired_accepts_missing_body(
    tmp_path,
    monkeypatch,
) -> None:
    """过期归档清理不应因前端未传 JSON body 返回 422。"""
    client, service = _client(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path, "alice", "source-a")
    archive_file = workspace / "governance" / "archive" / "files" / "archive-1"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text("old", encoding="utf-8")
    (workspace / ARCHIVE_INDEX_FILE).parent.mkdir(parents=True, exist_ok=True)
    (workspace / ARCHIVE_INDEX_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "archive-1",
                        "original_path": "old.md",
                        "archive_path": "governance/archive/files/archive-1",
                        "size_bytes": 3,
                        "mtime": "2000-01-01T00:00:00Z",
                        "archived_at": "2000-01-01T00:00:00Z",
                        "archived_by": "admin",
                        "archive_reason": "manual",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/dream-logs/archive/purge-expired",
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    assert response.json()["files_deleted"] == ["old.md"]
    assert service.delete_archive_calls[0]["archive_item_ids"] == ["archive-1"]


def test_purge_expired_uses_database_archive_records_when_index_missing(
    tmp_path,
    monkeypatch,
) -> None:
    """页面列表来自读模型时，过期清理也应能找到同一批归档记录。"""
    client, service = _client(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path, "alice", "source-a")
    archive_file = (
        workspace / "governance" / "archive" / "files" / "archive-db"
    )
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text("old", encoding="utf-8")
    service.archive_records.append(
        SimpleNamespace(
            source_id="source-a",
            target_user_id="alice",
            target_agent_id="default",
            archive_item_id="archive-db",
            original_path="old.md",
            archive_path="governance/archive/files/archive-db",
            size_bytes=3,
            mtime="2000-01-01T00:00:00Z",
            archived_at="2000-01-01T00:00:00Z",
            archived_by="admin",
            archive_reason="manual",
            raw_item={},
        ),
    )

    response = client.post(
        "/dream-logs/archive/purge-expired",
        json={"reason": "expired_10_days"},
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    assert response.json()["files_deleted"] == ["old.md"]
    assert not archive_file.exists()
    assert service.delete_archive_calls[0]["archive_item_ids"] == [
        "archive-db",
    ]
    assert service.archive_records == []


def test_purge_expired_uses_source_and_distinct_audit_events(
    tmp_path,
    monkeypatch,
) -> None:
    """批量清理过期归档应按 workspace 写独立 audit 事件。"""
    client, service = _client(tmp_path, monkeypatch)
    for tenant_id, archive_id in [
        ("alice", "archive-a"),
        ("manager", "archive-m"),
    ]:
        workspace = _workspace(tmp_path, tenant_id, "source-a")
        archive_file = (
            workspace / "governance" / "archive" / "files" / archive_id
        )
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        archive_file.write_text("old", encoding="utf-8")
        (workspace / ARCHIVE_INDEX_FILE).parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        (workspace / ARCHIVE_INDEX_FILE).write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            "id": archive_id,
                            "original_path": f"{tenant_id}.md",
                            "archive_path": f"governance/archive/files/{archive_id}",
                            "size_bytes": 3,
                            "mtime": "2000-01-01T00:00:00Z",
                            "archived_at": "2000-01-01T00:00:00Z",
                            "archived_by": "admin",
                            "archive_reason": "manual",
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )

    response = client.post(
        "/dream-logs/archive/purge-expired",
        json={"reason": "expired_10_days"},
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    assert len(service.audit_calls) == 2
    assert len({row["event_id"] for row in service.audit_calls}) == 2
    assert {row["source_id"] for row in service.audit_calls} == {"source-a"}


def test_purge_expired_skips_invalid_agent_directories(
    tmp_path,
    monkeypatch,
) -> None:
    """全量清理枚举历史 workspace 时不得把非法目录名写入读模型。"""
    client, service = _client(tmp_path, monkeypatch)
    valid_workspace = _workspace(tmp_path, "alice", "source-a")
    invalid_workspace = valid_workspace.parent / "bad..agent"
    for workspace, archive_id in [
        (valid_workspace, "archive-valid"),
        (invalid_workspace, "archive-invalid"),
    ]:
        archive_file = (
            workspace / "governance" / "archive" / "files" / archive_id
        )
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        archive_file.write_text("old", encoding="utf-8")
        (workspace / ARCHIVE_INDEX_FILE).parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        (workspace / ARCHIVE_INDEX_FILE).write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            "id": archive_id,
                            "original_path": f"{archive_id}.md",
                            "archive_path": f"governance/archive/files/{archive_id}",
                            "size_bytes": 3,
                            "mtime": "2000-01-01T00:00:00Z",
                            "archived_at": "2000-01-01T00:00:00Z",
                            "archived_by": "admin",
                            "archive_reason": "manual",
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )

    response = client.post(
        "/dream-logs/archive/purge-expired",
        json={"reason": "expired_10_days"},
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    assert response.json()["files_deleted"] == ["archive-valid.md"]
    assert [row["target_agent_id"] for row in service.audit_calls] == [
        "default",
    ]
    assert (
        invalid_workspace
        / "governance"
        / "archive"
        / "files"
        / "archive-invalid"
    ).exists()


def test_restore_archive_updates_archive_and_protected_state(
    tmp_path,
    monkeypatch,
) -> None:
    """恢复归档并保护时应删除归档状态并写保护状态。"""
    client, service = _client(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path, "alice", "source-a")
    archive_file = workspace / "governance" / "archive" / "files" / "archive-1"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text("old", encoding="utf-8")
    (workspace / ARCHIVE_INDEX_FILE).parent.mkdir(parents=True, exist_ok=True)
    (workspace / ARCHIVE_INDEX_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "archive-1",
                        "original_path": "restored.md",
                        "archive_path": "governance/archive/files/archive-1",
                        "size_bytes": 3,
                        "mtime": "2026-05-24T09:00:00Z",
                        "archived_at": "2026-05-24T10:00:00Z",
                        "archived_by": "admin",
                        "archive_reason": "manual",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/dream-logs/archive/restore",
        json={
            "archive_item_id": "archive-1",
            "target_user_id": "alice",
            "target_agent_id": "default",
            "protect_after_restore": True,
        },
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    assert service.delete_archive_calls[0]["archive_item_ids"] == ["archive-1"]
    assert service.protected_calls[0]["path"] == "restored.md"
    assert service.protected_calls[0]["target_user_id"] == "alice"


def test_restore_protect_failure_health_keeps_protected_metadata(
    tmp_path,
    monkeypatch,
) -> None:
    """恢复后保护 DB 失败时，待对账 payload 应保留实际保护元数据。"""
    client, service = _client(tmp_path, monkeypatch)
    service.fail_protected_upsert = True
    workspace = _workspace(tmp_path, "alice", "source-a")
    archive_file = workspace / "governance" / "archive" / "files" / "archive-1"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text("old", encoding="utf-8")
    (workspace / ARCHIVE_INDEX_FILE).parent.mkdir(parents=True, exist_ok=True)
    (workspace / ARCHIVE_INDEX_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "archive-1",
                        "original_path": "restored.md",
                        "archive_path": "governance/archive/files/archive-1",
                        "size_bytes": 3,
                        "mtime": "2026-05-24T09:00:00Z",
                        "archived_at": "2026-05-24T10:00:00Z",
                        "archived_by": "admin",
                        "archive_reason": "manual",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/dream-logs/archive/restore",
        json={
            "archive_item_id": "archive-1",
            "target_user_id": "alice",
            "target_agent_id": "default",
            "protect_after_restore": True,
        },
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
            "X-User-Id": "admin",
        },
    )

    assert response.status_code == 200
    protected_index = json.loads(
        (
            workspace / "governance" / "archive" / "protected_paths.json"
        ).read_text(encoding="utf-8"),
    )
    workspace_protected = protected_index["paths"][0]
    payload = service.health_calls[0]["payload"]
    assert payload["archive_item_id"] == "archive-1"
    assert payload["protected_at"] == workspace_protected["protected_at"]
    assert payload["protected_by"] == "admin"
    assert payload["exists"] is True
    assert payload["size_bytes"] == 3
    assert payload["mtime"]


def test_remove_protected_file_deletes_database_state(
    tmp_path,
    monkeypatch,
) -> None:
    """取消保护时应删除 DB 保护状态。"""
    client, service = _client(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path, "alice", "source-a")
    protected_path = (
        workspace / "governance" / "archive" / "protected_paths.json"
    )
    protected_path.parent.mkdir(parents=True, exist_ok=True)
    protected_path.write_text(
        json.dumps(
            {
                "version": 1,
                "paths": [
                    {
                        "path": "notes.md",
                        "protected_at": "2026-05-24T09:00:00Z",
                        "protected_by": "admin",
                        "reason": "manual",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    response = client.request(
        "DELETE",
        "/dream-logs/archive/protected-files",
        json={
            "target_user_id": "alice",
            "target_agent_id": "default",
            "path": "notes.md",
        },
        headers={
            "X-Source-Id": "source-a",
            "X-Tenant-Id": "manager",
            "X-User-Role": "manager",
        },
    )

    assert response.status_code == 200
    assert service.delete_protected_calls[0]["path"] == "notes.md"
    assert service.delete_protected_calls[0]["target_user_id"] == "alice"


def test_dual_write_workspace_governance_records_imports_new_records_only(
    tmp_path,
    monkeypatch,
) -> None:
    """dream 完成后只把新增 workspace 记录写入 DB。"""
    service = _FakeGovernanceService()
    monkeypatch.setattr(
        "swe.app.workspace.tenant_init_source_store"
        ".get_tenant_init_source_store",
        _FakeTenantSourceStore,
    )
    workspace = _workspace(tmp_path, "alice", "source-a")
    (workspace / "dream_logs.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "id": "old-record",
                        "timestamp": "2026-05-24T09:00:00Z",
                    },
                    {
                        "id": "new-record",
                        "timestamp": "2026-05-25T09:00:00Z",
                        "status": "success",
                        "trigger": "manual",
                    },
                ],
                "stats": {},
            },
        ),
        encoding="utf-8",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(continuous_governance_service=service),
        ),
        state=SimpleNamespace(source_id="source-a"),
        headers={},
    )

    import anyio

    async def _run_dual_write():
        await _dual_write_workspace_governance_records(
            request,
            workspace_dir=workspace,
            target_user_id="alice",
            target_agent_id="default",
            before_record_ids={"old-record"},
        )

    anyio.run(_run_dual_write)

    assert len(service.governance_calls) == 1
    assert service.governance_calls[0]["record"]["id"] == "new-record"
    assert service.governance_calls[0]["target_user_name"] == "Alice"
