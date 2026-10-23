# -*- coding: utf-8 -*-
"""Actual paginated list attempts are counted, including SDK failures."""

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("fail_second", [False, True])
def test_s3_page_calls_are_counted_without_changing_pagination(
    monkeypatch,
    fail_second,
):
    from swe import performance_metrics
    from swe.app.backup.s3_client import S3BackupClient

    metrics = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", metrics)
    calls = []

    def list_objects(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"IsTruncated": True, "NextContinuationToken": "page-2"}
        if fail_second:
            raise ValueError("SDK failed")
        return {"IsTruncated": False}

    client = object.__new__(S3BackupClient)
    client._s3 = SimpleNamespace(list_objects_v2=list_objects)
    client.bucket = "test"
    client.prefix = "backup"
    if fail_second:
        with pytest.raises(ValueError, match="SDK failed"):
            client.list_backups(instance_id="instance-a")
    else:
        assert client.list_backups(instance_id="instance-a")["backups"] == {}
    assert calls[1]["ContinuationToken"] == "page-2"
    assert metrics.snapshot()["counters"]["s3_list_pages"] == 2
