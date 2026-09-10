import pytest

from tests.unit.scheduler.test_cron_execution_reconciliation import _SqliteDb


@pytest.mark.asyncio
async def test_manual_retry_keeps_attempt_and_only_adds_one_chance(
    monkeypatch,
):
    from scheduler.app.services.cron import batch_operations as ops

    db = _SqliteDb()
    db.connection.executescript("""
        ALTER TABLE swe_cron_dispatch_events ADD COLUMN batch_id TEXT;
        ALTER TABLE swe_cron_dispatch_events ADD COLUMN source_id TEXT;
        ALTER TABLE swe_cron_dispatch_events ADD COLUMN details TEXT;
        CREATE TABLE swe_cron_jobs (
            id TEXT, tenant_id TEXT, source_id TEXT,
            enabled INTEGER, status TEXT
        );
        INSERT INTO swe_cron_jobs VALUES
            ('job-1','tenant-1','source-a',1,'active');
        CREATE TABLE swe_cron_dispatch_batches (
            batch_id TEXT PRIMARY KEY, source_id TEXT, status TEXT,
            tenant_id TEXT DEFAULT 'tenant-1', parent_job_id TEXT DEFAULT 'parent',
            completed_count INTEGER, failed_count INTEGER, completed_at TEXT,
            skipped_count INTEGER DEFAULT 0,
            updated_at TEXT, total_count INTEGER DEFAULT 1,
            lock_owner TEXT DEFAULT '', locked_at TEXT
        );
        CREATE TABLE swe_cron_dispatch_controls (
            source_id TEXT,tenant_id TEXT,parent_job_id TEXT,paused INTEGER,
            version INTEGER,resumed_at TEXT,updated_by TEXT,created_at TEXT,updated_at TEXT
        );
        INSERT INTO swe_cron_dispatch_controls (source_id,tenant_id,parent_job_id,paused)
            VALUES ('source-a','tenant-1','parent',0);
        INSERT INTO swe_cron_dispatch_batches
            (batch_id,source_id,status,completed_count,failed_count,
             completed_at,updated_at) VALUES
            ('batch-1','source-a','failed',0,1,NULL,NULL);
    """)
    monkeypatch.setattr(ops, "get_db_connection", lambda: db)
    db.add_intent(
        status="failed", attempt_count=3, error_message="network error"
    )
    body = ops.RetryRequest(candidates=[{"id": 7, "attempt_count": 3}])
    result = await ops.retry_failed("source-a", "batch-1", "admin", body)
    assert result["queued"] == 1
    assert db.intent()["status"] == "pending"
    assert db.intent()["attempt_count"] == 3
    assert db.intent()["max_attempts"] == 4
    assert (await ops.retry_failed("source-a", "batch-1", "admin", body))[
        "queued"
    ] == 0
    assert (
        db.connection.execute(
            "SELECT COUNT(*) FROM swe_cron_dispatch_events"
        ).fetchone()[0]
        == 1
    )
    db.connection.execute(
        "UPDATE swe_cron_dispatch_intents "
        "SET status='failed', attempt_count=4"
    )
    assert (await ops.retry_failed("source-a", "batch-1", "admin", body))[
        "queued"
    ] == 0
    db.connection.execute(
        "UPDATE swe_cron_dispatch_intents "
        "SET error_message='cron auth user_info is expired'"
    )
    current = ops.RetryRequest(candidates=[{"id": 7, "attempt_count": 4}])
    with pytest.raises(ValueError, match="确认已修复"):
        await ops.retry_failed("source-a", "batch-1", "admin", current)
    assert db.intent()["status"] == "failed"
    preview = await ops.failure_preview(
        "source-a", "batch-1", ["auth_expired"]
    )
    assert preview["counts"] == [
        {"failure_type": "auth_expired", "count": 1, "label": "鉴权过期"}
    ]
    assert preview["items"][0]["attempt_count"] == 4
    with pytest.raises(LookupError):
        await ops.retry_failed("source-b", "batch-1", "admin", current)
    current.confirm_resolved = True
    assert (await ops.retry_failed("source-a", "batch-1", "admin", current))[
        "queued"
    ] == 1
    assert db.intent()["max_attempts"] == 5
    db.connection.execute(
        "UPDATE swe_cron_dispatch_intents SET status='failed', attempt_count=5"
    )
    db.connection.execute("UPDATE swe_cron_jobs SET enabled=0")
    current.candidates[0].attempt_count = 5
    assert (await ops.retry_failed("source-a", "batch-1", "admin", current))[
        "queued"
    ] == 0
    db.connection.close()


@pytest.mark.parametrize(
    "message,expected",
    [
        (
            "鉴权过期: cron auth user_info is expired; "
            "please refresh cron auth configuration",
            "auth_expired",
        ),
        ("获取子任务状态超时", "subtask_timeout"),
        ("子任务执行失败", "subtask_failed"),
        ("channel not found", "configuration"),
        ("Rate limit exceeded", "model_rate_limit"),
        ("dispatch outcome unknown past stale timeout", "outcome_unknown"),
        ("unrecognized failure", "other"),
    ],
)
def test_failure_categories(message, expected):
    from scheduler.app.services.cron.batch_operations import classify_failure

    assert classify_failure(message) == expected


def test_sql_classification_matches_python_for_all_categories():
    from scheduler.app.services.cron.batch_operations import (
        FAILURE_RULES,
        failure_case_sql,
        classify_failure,
    )

    db = _SqliteDb()
    for _, _, patterns in FAILURE_RULES:
        for message in patterns:
            db.connection.execute("DELETE FROM swe_cron_dispatch_intents")
            db.add_intent(error_message=message)
            assert db.connection.execute(
                f"SELECT {failure_case_sql()} FROM swe_cron_dispatch_intents"
            ).fetchone()[0] == classify_failure(message)
    db.connection.close()


@pytest.mark.parametrize(
    "error",
    [
        "Agent execution failed: model_call_failed: "
        "The model provider rate-limited this request. "
        '{"returnCode": "LAILGW0429","errorMsg": "过多的访问请求"}',
        "Agent execution failed: model_call_failed: "
        "The model provider rate-limited this request. "
        '{"returnCode":"LAILGW0433",'
        '"errorMsg":"输出Token数已达到每分钟上限"}',
        '{"returnCode":"LAILGW0429","errorMsg":"gateway rejected"}',
        '{"returnCode":"LAILGW0433","errorMsg":"gateway rejected"}',
    ],
)
def test_gateway_rate_limit_errors_are_classified_in_python_and_sql(error):
    from scheduler.app.services.cron.batch_operations import (
        classify_failure,
        failure_case_sql,
    )

    assert classify_failure(error) == "model_rate_limit"
    db = _SqliteDb()
    try:
        db.add_intent(error_message=error)
        result = db.connection.execute(
            f"SELECT {failure_case_sql()} FROM swe_cron_dispatch_intents"
        ).fetchone()[0]
        assert result == "model_rate_limit"
    finally:
        db.connection.close()
