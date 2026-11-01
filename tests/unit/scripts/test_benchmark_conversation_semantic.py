# -*- coding: utf-8 -*-
"""Stable aggregation and parameter coverage for the local T8.5 baseline."""

import importlib.util
from pathlib import Path

import pytest

from swe.database.config import DatabaseConfig

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts/benchmark_conversation_semantic.py"
)


def load_script():
    spec = importlib.util.spec_from_file_location(
        "conversation_semantic_benchmark",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_percentile_summary_uses_nearest_rank_and_empty_nulls():
    module = load_script()
    assert module.latency_summary([1, 2, 3, 4, 5]) == {
        "sample_count": 5,
        "p50_ms": 3,
        "p95_ms": 5,
        "p99_ms": 5,
        "max_ms": 5,
    }
    assert module.latency_summary([]) == {
        "sample_count": 0,
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "max_ms": None,
    }


def test_parameter_groups_cover_small_default_and_large_bounds():
    module = load_script()
    assert len(module.GROUPS) == 3
    assert module.GROUPS[0]["max_bytes"] < module.DEFAULT_GROUP["max_bytes"]
    assert module.GROUPS[0]["max_events"] < module.DEFAULT_GROUP["max_events"]
    assert (
        module.GROUPS[0]["max_seconds"] < module.DEFAULT_GROUP["max_seconds"]
    )
    assert module.DEFAULT_GROUP == {
        "name": "default",
        "max_bytes": 64 * 1024,
        "max_events": 64,
        "max_seconds": 1.0,
    }
    assert all(
        group["max_bytes"] > module.DEFAULT_GROUP["max_bytes"]
        and group["max_events"] > module.DEFAULT_GROUP["max_events"]
        and group["max_seconds"] > module.DEFAULT_GROUP["max_seconds"]
        for group in module.GROUPS[2:]
    )


@pytest.mark.parametrize("failure", [None, "install", "close"])
async def test_generated_schema_cleanup_even_when_setup_fails(
    monkeypatch,
    failure,
):
    module = load_script()
    calls = []

    class FakeDatabase:
        def __init__(self, config):
            self.config = config

        async def connect(self):
            calls.append(("connect", self.config.database))

        async def close(self):
            calls.append(("close", self.config.database))
            if failure == "close" and self.config.database != "business":
                raise RuntimeError("close failed")

        async def execute(self, statement):
            calls.append((statement, self.config.database))

        async def fetch_all(self, statement):
            assert statement == "SHOW FULL TABLES"
            calls.append(("empty-check", self.config.database))
            return []

    async def install(database):
        calls.append(("install", database.config.database))
        if failure == "install":
            raise RuntimeError("install failed")

    monkeypatch.setattr(module, "DatabaseConnection", FakeDatabase)
    monkeypatch.setattr(module, "install_schema", install)
    config = DatabaseConfig(database="business")
    if failure:
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            async with module.isolated_database(config):
                if failure == "install":
                    pytest.fail("setup failure must prevent the workload")
    else:
        async with module.isolated_database(config) as (database, schema):
            assert database.config.database == schema
    created = [sql for sql, _ in calls if sql.startswith("CREATE DATABASE")]
    dropped = [sql for sql, _ in calls if sql.startswith("DROP DATABASE")]
    assert len(created) == len(dropped) == 1
    schema = created[0].split("`")[1]
    assert schema.startswith("test_semantic_bench_")
    assert dropped == [f"DROP DATABASE `{schema}`"]
    assert ("install", schema) in calls
    assert ("empty-check", schema) in calls
    assert ("close", schema) in calls
    assert ("close", "business") in calls


def test_event_durability_only_settles_events_in_committed_root():
    module = load_script()
    metrics = module.RunMetrics()
    metrics.pending_events = [(1, 10.0), (2, 11.0), (3, 12.0)]
    root = {
        "agent": {
            "memory": {
                "content": [[{"metadata": {"benchmark_sequence": 2}}, []]],
            },
        },
    }
    metrics.record_commit(root, started=13.0, finished=14.0)
    assert metrics.commit_latencies == [1000.0]
    assert metrics.durable_latencies == [4000.0, 3000.0]
    assert metrics.pending_events == [(3, 12.0)]
