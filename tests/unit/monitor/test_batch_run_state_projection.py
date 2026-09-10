import pytest

from tests.unit.monitor.test_cron_dispatch_monitor import FakeDb


@pytest.mark.asyncio
async def test_skip_counts_are_terminal_and_pause_is_separate(monkeypatch):
    from monitor.app.services.cron import query_service as module

    db = FakeDb(
        one_results=[
            {
                "total_batches": 1,
                "total_intents": 3,
                "completed_intents": 1,
                "failed_intents": 0,
                "skipped_intents": 2,
            }
        ],
        all_results=[
            [
                {
                    "batch_id": "b",
                    "status": "completed",
                    "total_count": 3,
                    "completed_count": 1,
                    "failed_count": 0,
                    "skipped_count": 2,
                    "dispatch_paused": 1,
                }
            ]
        ],
    )
    monkeypatch.setattr(module, "get_db_connection", lambda: db)
    result = await module.QueryService().get_dispatch_batches(
        source_id="source-a"
    )
    assert result.stats.pending_intents == 0
    assert result.stats.skipped_intents == 2
    assert result.items[0].skipped_count == 2
    assert result.items[0].dispatch_paused is True
    assert result.items[0].status == "completed"
    sql = " ".join(db.fetch_all_calls[0][0].split())
    assert "c.tenant_id=b.tenant_id" in sql
    assert "c.source_id=b.source_id" in sql
    assert "c.parent_job_id=b.parent_job_id" in sql


def test_old_batch_projection_preserves_unknown_control_state():
    from monitor.app.services.cron.query_service import QueryService

    item = QueryService()._map_dispatch_batch({"batch_id": "old"})
    assert item.dispatch_paused is None
    assert item.skipped_count == 0
