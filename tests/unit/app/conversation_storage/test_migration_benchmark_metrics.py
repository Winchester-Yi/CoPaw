# -*- coding: utf-8 -*-
"""Benchmark observation preserves results, exceptions and nested call counts."""

import json
from types import SimpleNamespace

import pytest

from tests.integrated.conversation_storage import (
    test_migration_10k_mysql as bench,
)


async def test_query_metrics_preserve_parameters_results_and_errors(
    monkeypatch,
):
    from swe.database.session import DatabaseSession

    marker = object()

    async def query(_session, sql, params, mode):
        assert params is marker
        assert mode == "one"
        if sql == "fail":
            raise ValueError("private parameter value")
        return marker

    monkeypatch.setattr(DatabaseSession, "_query", query)
    metrics = bench._track_sql_queries(monkeypatch)
    session = object.__new__(DatabaseSession)
    assert await session._query("ok", marker, "one") is marker
    with pytest.raises(ValueError, match="private parameter value"):
        await session._query("fail", marker, "one")
    assert metrics["calls"] == 2
    assert metrics["failed_calls"] == 1
    assert metrics["summed_seconds"] >= 0
    assert "private" not in json.dumps(metrics)


def test_bundle_metrics_preserve_nested_calls_errors_and_snapshot(monkeypatch):
    marker = object()

    def read(*_args, **_kwargs):
        return marker

    def publish(*_args, **_kwargs):
        return bench.BundleStore.read_extent()

    def fail(*_args, **_kwargs):
        raise ValueError("private body")

    for name, function in {
        "inspect": fail,
        "read_extent": read,
        "publish": publish,
        "confirm_durable": read,
    }.items():
        monkeypatch.setattr(bench.BundleStore, name, function)
    metrics = bench._track_bundle_io(monkeypatch)
    before = bench._metric_snapshot("start", {"calls": 0}, metrics)
    assert bench.BundleStore.publish() is marker
    with pytest.raises(ValueError, match="private body"):
        bench.BundleStore.inspect()
    assert metrics["publish"]["calls"] == 1
    assert metrics["read_extent"]["calls"] == 1
    assert metrics["inspect"]["failed_calls"] == 1
    assert metrics["confirm_durable"]["calls"] == 0
    assert before["bundle_io"]["publish"]["calls"] == 0
    assert "private" not in json.dumps(metrics)


async def test_failure_report_keeps_primary_error_and_excludes_cleanup_time(
    monkeypatch,
    tmp_path,
    capsys,
):
    clock = [0.0]

    async def prepare(*_args):
        return [(tmp_path, tmp_path)], [], []

    def freeze(*_args):
        clock[0] = 10.0
        raise ValueError("migration failed")

    def cleanup(*_args):
        clock[0] += 100.0
        raise OSError("cleanup failed")

    monkeypatch.setattr(bench, "_prepare_benchmark", prepare)
    monkeypatch.setattr(bench, "_dataset_metrics", lambda _data: {})
    monkeypatch.setattr(bench, "_configured_io_delay", lambda _patch: 0)
    monkeypatch.setattr(bench, "_freeze_manifest", freeze)
    monkeypatch.setattr(bench, "_make_writable", cleanup)
    monkeypatch.setattr(
        bench,
        "time",
        SimpleNamespace(perf_counter=lambda: clock[0]),
    )
    with pytest.raises(ValueError, match="migration failed"):
        await bench.test_10k_workspace_import_resume_verify_finalize(
            SimpleNamespace(config=SimpleNamespace(database="test_unused")),
            tmp_path,
            monkeypatch,
            1,
        )
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert report["error_type"] == "ValueError"
    assert report["cleanup_error_type"] == "OSError"
    assert report["total_seconds"] == 10
    assert report["last_stage_seconds"] == 10
    assert report["cleanup_seconds"] == 100
