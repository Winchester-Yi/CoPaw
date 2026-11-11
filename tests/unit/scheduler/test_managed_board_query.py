# -*- coding: utf-8 -*-
"""真实 SQL 验证新看板统计、成员尝试和分页，而非固定查询结果。"""

import pytest
from monitor.app.models.managed_board import BoardDetailQuery

from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup
from tests.unit.scheduler.test_managed_batch_retry import (
    failed_batch,
    retry_body,
)


@pytest.mark.parametrize("query", ["report", "model-in-one-member"])
@pytest.mark.asyncio
async def test_board_combines_time_status_and_global_keyword(
    callback_setup,
    query,
):
    from datetime import datetime

    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    db = callback_setup[0]
    await failed_batch(callback_setup)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET model_id='model-in-one-member'",
    )
    result = await ManagedBoardQuery(db).batches(
        "bank",
        start=datetime(2026, 10, 16, 9),
        end=datetime(2026, 10, 16, 9),
        status="failed",
        query=query,
    )
    assert result["total"] == 1
    assert result["stats"]["failed_members"] == 1
    assert result["items"][0]["status"] == "failed"
    excluded = await ManagedBoardQuery(db).batches(
        "bank",
        start=datetime(2026, 10, 16, 9, 0, 1),
        query=query,
    )
    assert excluded["total"] == 0
    assert excluded["items"] == []


@pytest.mark.asyncio
async def test_board_stats_and_items_share_one_read_snapshot(callback_setup):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    db = callback_setup[0]
    await failed_batch(callback_setup)
    fetch_all = db.fetch_all
    changed = False

    async def read_then_change_status(query, params=None):
        nonlocal changed
        rows = await fetch_all(query, params)
        if rows and "total_batches" in rows[0] and not changed:
            db.conn.execute(
                "UPDATE swe_cron_execution_runs SET status='queued',settled_status=NULL",
            )
            changed = True
        return rows

    db.fetch_all = read_then_change_status
    result = await ManagedBoardQuery(db).batches("bank")
    assert changed
    assert (
        result["stats"]["failed_members"] == result["items"][0]["failed_count"]
    )


@pytest.mark.parametrize("page, status", [(2, None), (1, "completed")])
@pytest.mark.asyncio
async def test_board_empty_page_preserves_global_stats(
    callback_setup,
    page,
    status,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    await failed_batch(callback_setup)
    result = await ManagedBoardQuery(callback_setup[0]).batches(
        "bank",
        page=page,
        page_size=1,
        status=status,
    )
    assert result["items"] == []
    assert result["total"] == (1 if status is None else 0)
    assert result["stats"]["total_members"] == (1 if status is None else 0)


@pytest.mark.parametrize("corruption", ["identity", "execution", "missing"])
@pytest.mark.asyncio
async def test_board_rejects_bad_relation_outside_selected_status(
    callback_setup,
    corruption,
):
    import json

    from monitor.app.services.cron.managed_board import (
        ManagedBoardQuery,
        ManagedBoardUnavailable,
    )

    db = callback_setup[0]
    _, batch = await failed_batch(callback_setup)
    if corruption == "missing":
        db.conn.execute("DELETE FROM swe_cron_managed_batch_members")
    else:
        raw = db.conn.execute(
            "SELECT members_json FROM swe_cron_managed_trigger_receipts WHERE batch_id=?",
            (batch,),
        ).fetchone()[0]
        manifest = json.loads(raw)
        if corruption == "identity":
            manifest[0]["identity"][0] = "ALICE"
        else:
            manifest[0]["execution_id"] = "foreign-execution"
        db.conn.execute(
            "UPDATE swe_cron_managed_trigger_receipts SET members_json=? WHERE batch_id=?",
            (json.dumps(manifest), batch),
        )
    with pytest.raises(ManagedBoardUnavailable):
        await ManagedBoardQuery(db).batches("bank", status="completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "failed", "completed"])
@pytest.mark.parametrize("page", [1, 2])
async def test_list_aggregates_once_and_preserves_filtered_totals(
    callback_setup,
    status,
    page,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    db = callback_setup[0]
    await failed_batch(callback_setup)
    statements = []
    db.conn.set_trace_callback(statements.append)
    result = await ManagedBoardQuery(db).batches(
        "bank",
        status=status,
        page=page,
        page_size=1,
    )
    aggregates = [q for q in statements if "GROUP BY c.receipt_id" in q]
    assert len(aggregates) == 1
    assert aggregates[0].count("GROUP BY c.receipt_id") == 1
    expected = 0 if status == "completed" else 1
    assert result["total"] == expected
    assert result["stats"]["total_members"] == expected
    assert result["stats"]["failed_members"] == expected
    assert len(result["items"]) == (expected if page == 1 else 0)
    assert result["page"] == page
    assert result["page_size"] == 1
    assert not (await ManagedBoardQuery(db).batches("other"))["items"]


@pytest.mark.asyncio
async def test_board_counts_current_member_without_growing_after_retry(
    callback_setup,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    service = ManagedBoardQuery(db)
    first = await service.batches("bank", page_size=1)
    assert (
        first["stats"]["total_members"]
        == first["stats"]["failed_members"]
        == 1
    )
    assert first["items"][0]["status"] == "failed"
    assert first["items"][0]["logical_fire_at"].endswith("+00:00")
    retry = BatchRetryService(db)
    await retry.retry(
        caller,
        batch,
        retry_body(await retry.preview(caller, batch)),
    )
    detail = await service.detail(
        "bank",
        batch,
        BoardDetailQuery(event_limit=1),
    )
    assert detail["batch"]["member_count"] == 1
    assert detail["batch"]["status"] == "pending"
    assert detail["members"][0]["business_attempt"] == 2
    member = detail["members"][0]
    history = await service.member_attempts("bank", batch, member["member_id"])
    assert [
        (row["business_attempt"], row["current"]) for row in history["items"]
    ] == [
        (1, False),
        (2, True),
    ]
    assert history["items"][0]["execution_id"] == member["member_id"]
    assert history["items"][0]["status"] == "error"
    assert history["items"][1]["execution_id"] == member["execution_id"]
    with pytest.raises(LookupError):
        await service.member_attempts("other", batch, member["member_id"])
    assert detail["event_total"] > 1
    assert len(detail["events"]) == 1
    assert not (
        await service.detail(
            "bank",
            batch,
            BoardDetailQuery(member_status="error"),
        )
    )["members"]
    assert (
        await service.detail(
            "bank",
            batch,
            BoardDetailQuery(member_status="error"),
        )
    )["batch"]["member_count"] == 1
    assert not (await service.batches("other"))["items"]
    assert not (await service.batches("BANK"))["items"]
    with pytest.raises(LookupError):
        await service.detail("other", batch)


@pytest.mark.asyncio
async def test_board_no_false_complete_for_async_pending_or_missing_relation(
    callback_setup,
):
    from monitor.app.services.cron.managed_board import (
        ManagedBoardQuery,
        ManagedBoardUnavailable,
    )

    db = callback_setup[0]
    _, batch = await failed_batch(callback_setup)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='success',settled_status=NULL",
    )
    service = ManagedBoardQuery(db)
    pending = await service.batches("bank")
    assert pending["items"][0]["status"] == "running"
    assert pending["items"][0]["done_count"] == 0
    db.conn.execute("DELETE FROM swe_cron_managed_batch_members")
    with pytest.raises(ManagedBoardUnavailable):
        await service.detail("bank", batch)


@pytest.mark.asyncio
async def test_board_search_escapes_wildcards_and_stats_are_not_page_sums(
    callback_setup,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    db = callback_setup[0]
    await failed_batch(callback_setup)
    service = ManagedBoardQuery(db)
    assert (await service.batches("bank", page=2, page_size=1))["stats"][
        "total_members"
    ] == 1
    assert not (await service.batches("bank", query="%"))["items"]
    assert not (await service.batches("bank", query="_"))["items"]


@pytest.mark.asyncio
async def test_zero_member_batch_is_received_but_invalid_manifest_is_unavailable(
    callback_setup,
):
    from datetime import datetime, timezone
    from monitor.app.services.cron.managed_board import (
        ManagedBoardQuery,
        ManagedBoardUnavailable,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )
    from tests.unit.scheduler.test_managed_cron_callbacks import (
        create_source,
        body,
    )

    db, caller, _ = callback_setup
    created = await create_source(callback_setup, "batch_enabled")
    db.conn.execute("UPDATE swe_cron_jobs_v2 SET enabled=0")
    accepted = await ManagedSchedulingService(
        db,
        clock=lambda: datetime(2026, 10, 16, 1, tzinfo=timezone.utc),
    ).accept(caller, body(db, created, kind="batch"), "batch")
    service = ManagedBoardQuery(db)
    row = (await service.batches("bank"))["items"][0]
    assert row["batch_id"] == accepted["batch_id"]
    assert row["status"] == "received"
    assert row["member_count"] == 0
    assert row["completion_rate"] is None
    db.conn.execute(
        "UPDATE swe_cron_managed_trigger_receipts SET members_json='{}'",
    )
    with pytest.raises(ManagedBoardUnavailable):
        await service.batches("bank", status="completed")


@pytest.mark.asyncio
async def test_board_display_fields_use_current_execution_and_exact_history(
    callback_setup,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    db.conn.execute(
        "UPDATE swe_cron_executions SET error_message='workflow secret value missing: auth_token'",
    )
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET provider_id='frozen-provider',model_id='frozen-model'",
    )
    db.conn.execute(
        "UPDATE swe_cron_job_states SET dispatch_state='batch_paused'",
    )
    detail = await ManagedBoardQuery(db).detail("bank", batch)
    item = detail["batch"]
    assert (item["provider_id"], item["model_id"]) == (
        "frozen-provider",
        "frozen-model",
    )
    assert item["dispatch_paused"] is True
    assert item["success_count"] == item["skipped_count"] == 0
    assert item["lock_owner"] is None
    assert (
        detail["members"][0]["error_message"]
        == "workflow secret value missing: auth_token"
    )
    preview = await BatchRetryService(db).preview(caller, batch)
    assert (
        preview["items"][0]["error_message"]
        == detail["members"][0]["error_message"]
    )
    assert "dispatch_owner" not in str(detail)

    db.conn.execute(
        "UPDATE swe_cron_executions SET tenant_id='foreign-user',error_message='foreign-secret'",
    )
    detail = await ManagedBoardQuery(db).detail("bank", batch)
    preview = await BatchRetryService(db).preview(caller, batch)
    assert detail["members"][0]["error_message"] == "cron_execution_failed"
    assert preview["items"][0]["error_message"] == "cron_execution_failed"
    assert "foreign-secret" not in str([detail, preview])


@pytest.mark.parametrize(
    "status, settled, success, skipped",
    [
        ("success", "success", 1, 0),
        ("skipped", None, 0, 1),
        ("cancelled", None, 0, 1),
    ],
)
@pytest.mark.asyncio
async def test_board_result_counts_do_not_treat_skip_as_success(
    callback_setup,
    status,
    settled,
    success,
    skipped,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery

    db = callback_setup[0]
    _, batch = await failed_batch(callback_setup)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status=?,settled_status=?,error_code=NULL",
        (status, settled),
    )
    detail = await ManagedBoardQuery(db).detail("bank", batch)
    assert detail["batch"]["success_count"] == success
    assert detail["batch"]["skipped_count"] == skipped
    assert detail["batch"]["done_count"] == 1
    if status == "success":
        assert detail["members"][0]["error_message"] == ""


@pytest.mark.parametrize(
    "metadata, expected",
    [
        (
            '{"error_detail":"配置失效","private":"must-not-expose"}',
            "配置失效",
        ),
        ("{invalid", "cron_execution_failed"),
        ("[]", "cron_execution_failed"),
    ],
)
@pytest.mark.asyncio
async def test_board_missing_history_uses_only_error_summary(
    callback_setup,
    metadata,
    expected,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    db.conn.execute("DELETE FROM swe_cron_executions")
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET result_json=?",
        (metadata,),
    )
    detail = await ManagedBoardQuery(db).detail("bank", batch)
    preview = await BatchRetryService(db).preview(caller, batch)
    assert detail["members"][0]["error_message"] == expected
    assert preview["items"][0]["error_message"] == expected
    assert "must-not-expose" not in str([detail, preview])


@pytest.mark.asyncio
async def test_async_child_failure_has_readable_message_without_inventing_detail(
    callback_setup,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    db.conn.execute("UPDATE swe_cron_executions SET error_message=''")
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='success',error_code=NULL,settled_status='error',settled_error_code='cron_async_subtasks_failed'",
    )
    detail = await ManagedBoardQuery(db).detail("bank", batch)
    preview = await BatchRetryService(db).preview(caller, batch)
    assert detail["members"][0]["error_message"] == "子任务执行失败"
    assert preview["items"][0]["error_message"] == "子任务执行失败"
    assert (
        detail["members"][0]["settled_error_code"]
        == "cron_async_subtasks_failed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["Prompt", "Business", None])
async def test_board_and_retry_preview_preserve_raw_error_type(
    callback_setup,
    error_type,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery
    from scheduler.app.services.cron.managed.batch_retry import (
        BatchRetryService,
    )

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    await db.execute(
        "UPDATE swe_cron_executions SET error_type=%s",
        (error_type,),
    )
    detail = await ManagedBoardQuery(db).detail(
        "bank",
        batch,
        BoardDetailQuery(),
    )
    preview = await BatchRetryService(db).preview(caller, batch)
    assert detail["members"][0]["error_type"] == error_type
    assert preview["items"][0]["error_type"] == error_type
    assert detail["batch"]["status"] == "failed"
