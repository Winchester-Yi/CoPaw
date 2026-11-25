# -*- coding: utf-8 -*-
"""模型池趋势使用真实执行时间和持久派发尝试。"""

from datetime import datetime, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup
from tests.unit.scheduler.test_managed_batch_retry import failed_batch


@pytest.mark.asyncio
async def test_worker_trends_counts_started_settled_and_retried(
    callback_setup,
):
    from monitor.app.services.cron.managed_worker_trends import worker_trends

    db = callback_setup[0]
    await failed_batch(callback_setup)
    db.conn.executescript("""
        UPDATE swe_cron_execution_runs SET provider_id='provider',model_id='model',
          spec_json=JSON_SET(spec_json,'$.task_type','agent'),
          created_at='2026-10-16 01:01:00',started_at='2026-10-16 01:02:00',
          settled_at='2026-10-16 01:14:00',settled_status='error';
        UPDATE swe_cron_managed_dispatch_events
          SET occurred_at='2026-10-16 01:01:00'
          WHERE event_type='dispatching';
        INSERT INTO swe_cron_managed_dispatch_events
          (event_id,event_key,tenant_id,source_id,agent_id,job_id,execution_id,
           event_type,actor_id,occurred_at,details_json)
          SELECT 'test-redelivery','test-redelivery',tenant_id,source_id,agent_id,
            job_id,execution_id,'transport_redelivery',actor_id,
            '2026-10-16 01:11:00','{"dispatch_attempt":2}'
          FROM swe_cron_execution_runs;
    """)
    start = datetime(2026, 10, 16, 1, tzinfo=timezone.utc)
    end = start.replace(minute=20)
    result = await worker_trends(db, "bank", "provider", "model", start, end)
    first, second = result["points"]
    assert (first["total"], first["requests"]) == (1, 1)
    assert "starts_per_minute" not in first
    assert (
        second["total"],
        second["failed"],
        second["requests"],
        second["retries"],
    ) == (1, 1, 2, 1)
    other = await worker_trends(db, "other", "provider", "model", start, end)
    assert all(
        point["requests"] == point["total"] == 0 for point in other["points"]
    )
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET spec_json=JSON_SET(spec_json,'$.task_type','text')",
    )
    text_only = await worker_trends(
        db,
        "bank",
        "provider",
        "model",
        start,
        end,
    )
    assert all(
        point["requests"] == point["total"] == 0
        for point in text_only["points"]
    )
