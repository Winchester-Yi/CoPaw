# -*- coding: utf-8 -*-
"""技能就绪检查存储测试。"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from swe.app.skill_readiness.models import (
    SkillReadinessCheckResult,
    SkillReadinessConfig,
    SkillReadinessOwner,
    SkillReadinessUserResult,
)
from swe.app.skill_readiness.store import SkillReadinessStore


class _Session:
    def __init__(self, fetchone_result=None):
        self.calls = []
        self._fetchone_result = fetchone_result

    async def execute(self, query, params=None):
        self.calls.append((query, params))
        return 1

    async def fetch_one(self, query, params=None):
        self.calls.append((query, params))
        return self._fetchone_result


class _Acquire:
    def __init__(self, session):
        self.session = session
        self.entered = 0
        self.exited = 0
        self.error = None

    async def __aenter__(self):
        self.entered += 1
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        self.exited += 1
        self.error = exc
        return False


def _install_transaction_connection(mock_db, *, fetchone_result=None):
    session = _Session(fetchone_result)
    context = _Acquire(session)
    mock_db.acquire.return_value = context
    mock_db.transaction.return_value = context
    mock_db.named_lock.return_value = context
    return context, session


@pytest.fixture
def mock_db():
    """创建可断言 SQL 调用的数据库 mock。"""
    db = MagicMock()
    db.is_connected = True
    db.execute = AsyncMock(return_value=1)
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    return db


@pytest.fixture
def store(mock_db):
    """创建技能就绪检查存储。"""
    return SkillReadinessStore(mock_db)


@pytest.mark.asyncio
async def test_initialize_executes_required_ddl(mock_db) -> None:
    """初始化应创建四张业务表和必要索引。"""
    await SkillReadinessStore(mock_db).initialize()

    executed_sql = "\n".join(
        call.args[0] for call in mock_db.execute.await_args_list
    )

    assert (
        "CREATE TABLE IF NOT EXISTS swe_skill_readiness_configs"
        in executed_sql
    )
    assert (
        "CREATE TABLE IF NOT EXISTS swe_skill_readiness_runs" in executed_sql
    )
    assert (
        "CREATE TABLE IF NOT EXISTS swe_skill_readiness_owner_snapshots"
        in executed_sql
    )
    assert (
        "CREATE TABLE IF NOT EXISTS swe_skill_readiness_user_results"
        in executed_sql
    )
    assert (
        "CREATE TABLE IF NOT EXISTS swe_skill_readiness_check_results"
        in executed_sql
    )
    assert "idx_skill_readiness_runs_state" in executed_sql
    assert "idx_skill_readiness_check_lookup" in executed_sql
    assert "idx_skill_readiness_check_user" in executed_sql


@pytest.mark.asyncio
async def test_get_config_returns_none_when_missing(store, mock_db) -> None:
    """未配置时返回 None，由后续服务决定是否允许启动。"""
    mock_db.fetch_one.return_value = None

    result = await store.get_config("skill-a")

    assert result is None
    mock_db.fetch_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_config_parses_valid_row(store, mock_db) -> None:
    """存量 JSON 应解析为配置记录并暴露 enabled checks。"""
    updated_at = datetime.now()
    mock_db.fetch_one.return_value = {
        "skill_id": "skill-a",
        "config_text": json.dumps(
            {
                "checks": [
                    {"name": "cron_auth_valid", "enabled": True, "params": {}},
                    {"name": "unused", "enabled": False, "params": {"x": 1}},
                ],
            },
        ),
        "updated_at": updated_at,
    }

    record = await store.get_config("skill-a")

    assert record is not None
    assert record.skill_id == "skill-a"
    assert [check.name for check in record.config.enabled_checks()] == [
        "cron_auth_valid",
    ]
    assert record.updated_at == updated_at


@pytest.mark.asyncio
async def test_get_config_rejects_invalid_config(store, mock_db) -> None:
    """损坏或不符合 schema 的配置不能被静默接受。"""
    mock_db.fetch_one.return_value = {
        "skill_id": "skill-a",
        "config_text": json.dumps({"checks": [{"enabled": True}]}),
        "updated_at": None,
    }

    with pytest.raises(ValueError, match="invalid skill readiness config"):
        await store.get_config("skill-a")


@pytest.mark.asyncio
async def test_get_owner_snapshot_parses_cached_owners(store, mock_db) -> None:
    """overview 读取 owner 快照时应恢复用户明细和查询状态。"""
    updated_at = datetime.now()
    mock_db.fetch_one.return_value = {
        "source_id": "source-a",
        "skill_id": "skill-a",
        "status": "completed",
        "total_users": 2,
        "owner_users": 1,
        "failed_users": 1,
        "failure_summary": "bob: market down",
        "owners_json": json.dumps(
            [{"user_id": "alice", "user_name": "Alice"}],
        ),
        "updated_at": updated_at,
    }

    snapshot = await store.get_owner_snapshot("source-a", "skill-a")

    assert snapshot is not None
    assert snapshot.status == "completed"
    assert snapshot.owner_summary.total_users == 1
    assert snapshot.owner_summary.lookup_failed_users == 1
    assert [owner.user_id for owner in snapshot.owners] == ["alice"]
    assert snapshot.updated_at == updated_at


@pytest.mark.asyncio
async def test_record_owner_snapshot_persists_owner_json(
    store,
    mock_db,
) -> None:
    """后台 owner 查询完成后应保存快照，供 overview 下次直接读取。"""
    await store.record_owner_snapshot(
        "source-a",
        "skill-a",
        status="completed",
        total_users=2,
        owners=[SkillReadinessOwner(user_id="alice", user_name="Alice")],
        failed_users=1,
        failure_summary="bob: market down",
    )

    query, params = mock_db.execute.await_args.args
    assert "INSERT INTO swe_skill_readiness_owner_snapshots" in query
    assert params[0:6] == ("source-a", "skill-a", "completed", 2, 1, 1)
    assert json.loads(params[7])[0]["user_id"] == "alice"


@pytest.mark.asyncio
async def test_mark_owner_lookup_running_preserves_cached_snapshot(
    store,
    mock_db,
) -> None:
    """刷新开始时只标记 running，不清空已缓存的 owner 列表。"""
    claimed = await store.mark_owner_lookup_running("source-a", "skill-a")

    query, params = mock_db.execute.await_args.args
    assert claimed is True
    assert "ON DUPLICATE KEY UPDATE" in query
    assert "status = 'running'" in query
    assert "owners_json = VALUES" not in query
    assert "updated_at = updated_at" in query
    assert params == ("source-a", "skill-a")


@pytest.mark.asyncio
async def test_mark_owner_lookup_running_returns_false_for_existing_running(
    store,
    mock_db,
) -> None:
    """DB 层已有 running claim 时不再触发新的 owner 刷新。"""
    mock_db.execute.return_value = 0

    claimed = await store.mark_owner_lookup_running("source-a", "skill-a")

    assert claimed is False


@pytest.mark.asyncio
async def test_get_or_create_running_run_reuses_existing(
    store,
    mock_db,
) -> None:
    """同一 source/skill 已有 running run 时应复用。"""
    created_at = datetime.now()
    _install_transaction_connection(
        mock_db,
        fetchone_result={"lock_acquired": 1},
    )
    mock_db.fetch_one.return_value = {
        "run_id": "run-existing",
        "source_id": "source-a",
        "skill_id": "skill-a",
        "status": "running",
        "total_users": 3,
        "completed_users": 1,
        "failed_users": 0,
        "failure_summary": None,
        "created_at": created_at,
        "started_at": None,
        "completed_at": None,
        "updated_at": created_at,
    }

    result, reused = await store.get_or_create_running_run(
        "source-a",
        "skill-a",
        SkillReadinessConfig.model_validate(
            {"checks": [{"name": "cron_auth_valid"}]},
        ),
    )

    assert result.run_id == "run-existing"
    assert reused is True
    executed_sql = "\n".join(
        call.args[0] for call in mock_db.execute.await_args_list
    )
    assert "INSERT INTO swe_skill_readiness_runs" not in executed_sql


@pytest.mark.asyncio
async def test_create_run_persists_snapshot_and_returns_created_row(
    store,
    mock_db,
) -> None:
    """新建 run 时应持久化配置快照并返回数据库中的进度行。"""
    created_at = datetime.now()
    mock_db.fetch_one.side_effect = [
        {
            "run_id": "generated",
            "source_id": "source-a",
            "skill_id": "skill-a",
            "status": "running",
            "total_users": 0,
            "completed_users": 0,
            "failed_users": 0,
            "failure_summary": None,
            "created_at": created_at,
            "started_at": created_at,
            "completed_at": None,
            "updated_at": created_at,
        },
    ]

    result = await store.create_run(
        "source-a",
        "skill-a",
        SkillReadinessConfig.model_validate(
            {"checks": [{"name": "cron_auth_valid"}]},
        ),
    )

    query, params = mock_db.execute.await_args.args
    assert "INSERT INTO swe_skill_readiness_runs" in query
    assert params[1:3] == ("source-a", "skill-a")
    assert json.loads(params[7]) == {
        "checks": [{"name": "cron_auth_valid", "enabled": True, "params": {}}],
    }
    assert params[8] is None
    assert result.run_id == "generated"


@pytest.mark.asyncio
async def test_record_user_result_replaces_checks_and_updates_progress(
    store,
    mock_db,
) -> None:
    """用户结果写入应替换该用户 check 明细并按 abnormal 累加失败数。"""
    user_result = SkillReadinessUserResult(
        user_id="alice",
        user_name="Alice",
        bbk_id="bbk-1",
        aggregate_status="abnormal",
        summary="auth failed",
        duration_ms=35,
        checks=[
            SkillReadinessCheckResult(
                check_name="cron_auth_valid",
                display_name="Cron auth valid",
                status="fail",
                message="token expired",
                details={"code": "expired"},
                duration_ms=30,
            ),
        ],
    )
    mock_db.fetch_one.return_value = {
        "run_id": "run-1",
        "source_id": "source-a",
        "skill_id": "skill-a",
        "status": "running",
        "total_users": 1,
        "completed_users": 1,
        "failed_users": 1,
        "failure_summary": None,
        "created_at": datetime.now(),
        "started_at": datetime.now(),
        "completed_at": None,
        "updated_at": datetime.now(),
    }

    _, cursor = _install_transaction_connection(mock_db)

    await store.record_user_result("run-1", user_result)

    queries = [call[0] for call in cursor.calls]
    assert "INSERT INTO swe_skill_readiness_user_results" in queries[0]
    assert "DELETE FROM swe_skill_readiness_check_results" in queries[1]
    assert "INSERT INTO swe_skill_readiness_check_results" in queries[2]
    assert "completed_users = completed_users + %s" in queries[3]
    assert cursor.calls[3][1] == (1, 1, "run-1")


@pytest.mark.asyncio
async def test_record_user_result_uses_transaction(store, mock_db) -> None:
    """用户聚合、明细替换和进度递增必须在一个事务中提交。"""
    conn, _ = _install_transaction_connection(mock_db)
    mock_db.fetch_one.return_value = {
        "run_id": "run-1",
        "source_id": "source-a",
        "skill_id": "skill-a",
        "status": "running",
        "total_users": 1,
        "completed_users": 1,
        "failed_users": 0,
        "failure_summary": None,
        "created_at": datetime.now(),
        "started_at": datetime.now(),
        "completed_at": None,
        "updated_at": datetime.now(),
    }

    await store.record_user_result(
        "run-1",
        SkillReadinessUserResult(
            user_id="alice",
            aggregate_status="normal",
            summary="ok",
            duration_ms=1,
            checks=[],
        ),
    )

    mock_db.transaction.assert_called_once_with()
    assert conn.entered == conn.exited == 1
    assert conn.error is None


@pytest.mark.asyncio
async def test_create_index_ignores_duplicate_index_race(
    store,
    mock_db,
) -> None:
    """多实例同时初始化时，一个实例建好索引后另一个不应启动失败。"""
    mock_db.fetch_one.return_value = {"total": 0}
    mock_db.execute.side_effect = Exception("Duplicate key name")

    await store._create_index_if_missing(
        mock_db,
        "idx_skill_readiness_check_user",
        "swe_skill_readiness_check_results",
        "run_id, user_id, id",
    )


@pytest.mark.asyncio
async def test_get_check_summaries_counts_statuses(store, mock_db) -> None:
    """check 汇总应把 pass/fail/skip 分别计数。"""
    mock_db.fetch_all.return_value = [
        {
            "check_name": "cron_auth_valid",
            "display_name": "Cron auth valid",
            "total": 4,
            "pass_count": 2,
            "fail_count": 1,
            "skip_count": 1,
        },
    ]

    result = await store.get_check_summaries("run-1")

    assert len(result) == 1
    assert result[0].check_name == "cron_auth_valid"
    assert result[0].pass_count == 2
    assert result[0].fail_count == 1
    assert result[0].skip_count == 1


@pytest.mark.asyncio
async def test_list_user_results_filters_by_check_but_returns_all_checks(
    store,
    mock_db,
) -> None:
    """按 check 过滤用户时，返回的用户仍应包含其全部 check 明细。"""
    mock_db.fetch_one.return_value = {"total": 1}
    mock_db.fetch_all.side_effect = [
        [
            {
                "run_id": "run-1",
                "user_id": "alice",
                "user_name": "Alice",
                "bbk_id": "bbk-1",
                "aggregate_status": "abnormal",
                "summary": "auth failed",
                "duration_ms": 35,
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
            },
        ],
        [
            {
                "user_id": "alice",
                "check_name": "cron_auth_valid",
                "display_name": "Cron auth valid",
                "status": "fail",
                "message": "token expired",
                "details_json": json.dumps({"code": "expired"}),
                "duration_ms": 30,
            },
            {
                "user_id": "alice",
                "check_name": "profile_exists",
                "display_name": "Profile exists",
                "status": "pass",
                "message": "",
                "details_json": json.dumps({}),
                "duration_ms": 5,
            },
        ],
    ]

    items, total = await store.list_user_results(
        "run-1",
        check_name="cron_auth_valid",
        check_status="fail",
    )

    user_query, user_params = mock_db.fetch_all.await_args_list[0].args
    check_query, check_params = mock_db.fetch_all.await_args_list[1].args
    assert total == 1
    assert len(items) == 1
    assert [check.check_name for check in items[0].checks] == [
        "cron_auth_valid",
        "profile_exists",
    ]
    assert "EXISTS" in user_query
    assert user_params[:3] == ("run-1", "cron_auth_valid", "fail")
    assert "check_name = %s" not in check_query
    assert check_params == ("run-1", "alice")


@pytest.mark.asyncio
async def test_run_lock_release_failure_is_not_suppressed(store, mock_db):
    from swe.app.skill_readiness.store import SkillReadinessStoreUnavailable

    class FailingRelease(_Acquire):
        async def __aexit__(self, *_args):
            raise RuntimeError("lock release was not confirmed")

    mock_db.named_lock.return_value = FailingRelease(_Session())
    store.get_running_run = AsyncMock(return_value=object())
    with pytest.raises(
        SkillReadinessStoreUnavailable,
        match="release was not confirmed",
    ):
        await store.get_or_create_running_run(
            "source-a",
            "skill-a",
            SkillReadinessConfig.model_validate(
                {"checks": [{"name": "cron_auth_valid"}]},
            ),
        )
    lock_name = mock_db.named_lock.call_args.args[0]
    assert lock_name.startswith("swe_skill_ready:")
    assert len(lock_name) == 64
    assert mock_db.named_lock.call_args.kwargs == {
        "timeout": 10,
        "exact_name": True,
    }
