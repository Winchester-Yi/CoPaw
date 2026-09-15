"""SQLite 验证真实聚合 SQL；不代表 TDSQL 方言与性能验证。"""

import sqlite3
from dataclasses import replace
from datetime import date, datetime

import pytest

from core_reference import (
    Scope,
    assemble,
    bind,
    build_queries,
    date_bounds,
    percentage,
)


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "FIND_IN_SET",
        2,
        lambda value, values: int(value in (values or "").split(",")),
    )
    connection.executescript("""
        CREATE TABLE jkh_user_inf(user_id TEXT, sync_date TEXT, first_bbk_id TEXT,
            org_id TEXT, first_bbk_nm TEXT, org_nm TEXT);
        CREATE TABLE swe_tenant_init_source(tenant_id TEXT, source_id TEXT);
        CREATE TABLE swe_cron_jobs(id TEXT, tenant_id TEXT, source_id TEXT,
            skill_ids TEXT, status TEXT, deleted_at TEXT);
        CREATE TABLE swe_cron_executions(id INTEGER, job_id TEXT, tenant_id TEXT,
            trace_id TEXT, actual_time TEXT, status TEXT, async_status TEXT, is_read INTEGER);
        CREATE TABLE swe_cron_subtasks(trace_id TEXT, custuid TEXT);
        CREATE TABLE swe_tracing_traces(trace_id TEXT, source_id TEXT, user_id TEXT,
            start_time TEXT, status TEXT);
        CREATE TABLE swe_tracing_spans(span_id TEXT, trace_id TEXT, source_id TEXT, skill_id TEXT,
            user_id TEXT DEFAULT 'alice', start_time TEXT DEFAULT '2026-09-13 10:00:00', has_error INTEGER DEFAULT 0);
        CREATE TABLE swe_marketplace_skills(source_id TEXT, skill_id TEXT, include_in_statistics INTEGER);
        CREATE TABLE swe_html_preview_click_events(source_id TEXT, user_id TEXT,
            cron_task_id TEXT, trace_id TEXT, customer_id TEXT, clicked_at TEXT,
            event_type TEXT, template_type TEXT, button_type TEXT);
        INSERT INTO jkh_user_inf VALUES
            ('alice','2026-09-13','001','01','甲分行','同名支行'),
            ('alice','2026-09-13','001','01','甲分行','同名支行'),
            ('bob','2026-09-13','002','01','乙分行','同名支行'),
            ('empty','2026-09-13','003','03','丙分行','空支行');
        INSERT INTO swe_tenant_init_source VALUES ('alice','S'),('alice','S'),('bob','OTHER');
        INSERT INTO swe_cron_jobs VALUES
            ('j1','alice','S','k1,k2','active',NULL),
            ('j2','alice','S','k1','active',NULL),
            ('j3','outsider','S','k1','active',NULL);
        INSERT INTO swe_cron_executions VALUES
            (1,'j1','alice','p1','2026-09-13 10:00:00','success','success',1),
            (2,'j1','alice','p2','2026-09-13 10:00:00','error','error',1),
            (3,'j2','alice','p3','2026-09-13 10:00:00','success','success',1),
            (4,'j2','alice','p4','2026-09-13 10:00:00','error','error',0),
            (5,'j3','outsider','p5','2026-09-13 10:00:00','success','success',1),
            (6,'j1','alice','stop','2026-09-14 00:00:00','success','success',1);
        INSERT INTO swe_cron_subtasks VALUES
            ('p1','C1'),('p1','C1'),('p2','C2'),('p5','C5'),('stop','STOP'),
            ('a1','A1'),('a1','A1'),('aerr','A2'),('old','OLD');
        INSERT INTO swe_tracing_traces VALUES
            ('a1','S','alice','2026-09-13 10:00:00','completed'),
            ('aerr','S','alice','2026-09-13 10:00:00','error'),
            ('old','S','alice','2026-09-12 10:00:00','completed'),
            ('p1','S','alice','2026-09-13 10:00:00','completed'),
            ('nost','S','alice','2026-09-13 10:00:00','completed');
        INSERT INTO swe_tracing_spans (span_id, trace_id, source_id, skill_id) VALUES
            ('s1','a1','S','k1'),('s2','a1','S','k2'),('s3','aerr','S','k1'),
            ('s4','old','S','k1'),('s5','p1','S','k1'),('s6','nost','S','k1');
        UPDATE swe_tracing_spans SET start_time = '2026-09-12 10:00:00' WHERE trace_id = 'old';
        UPDATE swe_tracing_spans SET has_error = 1 WHERE trace_id = 'aerr';
        INSERT INTO swe_marketplace_skills VALUES ('S','k1',1),('S','k1',1),('S','k2',1),('OTHER','k9',1);
        INSERT INTO swe_html_preview_click_events VALUES
            ('S','alice','j1','p1','C1','2026-09-13 11:00:00','preview_view','sub',NULL),
            ('S','alice','j1','p1','C1','2026-09-13 11:01:00','preview_view','sub',NULL),
            ('S','alice','j1','p1','C1','2026-09-13 11:00:00','button_click','sub','insight'),
            ('S','alice','j1','p1','C1','2026-09-13 11:00:01','button_click','sub','insight'),
            ('S','alice','j1','p1','C1','2026-09-13 11:00:00','button_click','sub','phone'),
            ('S','alice','j1','p1','C1','2026-09-13 11:00:01','button_click','sub','phone'),
            ('S','alice',NULL,'a1',NULL,'2026-09-13 11:00:00','preview_view','main',NULL),
            ('S','alice',NULL,'a1','A1','2026-09-13 11:01:00','preview_view','sub',NULL),
            ('S','alice',NULL,'a1','A1','2026-09-13 11:02:00','preview_view','sub',NULL),
            ('S','alice',NULL,'old','OLD','2026-09-13 11:00:00','preview_view','sub',NULL),
            ('S','outsider',NULL,'a1','LEAK','2026-09-13 11:00:00','preview_view','sub',NULL),
            ('OTHER','alice',NULL,'a1','LEAK','2026-09-13 11:00:00','preview_view','sub',NULL),
            ('S','alice',NULL,'a1','STOP','2026-09-14 00:00:00','preview_view','sub',NULL);
    """)
    yield connection
    connection.close()


@pytest.fixture
def scope():
    start, stop = date_bounds(date(2026, 9, 13), date(2026, 9, 13))
    return Scope("S", "2026-09-13", start, stop)


def execute(db, scope):
    results = {}
    for name, (sql, params) in build_queries(scope).items():
        params = tuple(
            value.isoformat(" ") if isinstance(value, datetime) else value
            for value in params
        )
        results[name] = [
            dict(row) for row in db.execute(sql.replace("%s", "?"), params)
        ]
    return results


def test_all_metrics_and_deduplication(db, scope):
    results = execute(db, scope)
    assert results["roster_conflicts"] == []
    rows = {row["task_type"]: row for row in assemble(results, scope.group_by)}
    push, ask, other = (
        rows[name] for name in ("push_plan", "ask_plan", "push_other")
    )
    assert (
        push["skill_count"],
        push["permission_manager_count"],
        push["active_manager_count"],
    ) == (2, 1, 1)
    assert (
        push["suc_execute_job"],
        push["read_tasks"],
        push["read_rate"],
    ) == (1, 2, 200.0)
    assert (push["recommended_customers"], push["read_customer_count"]) == (
        2,
        1,
    )
    assert (
        push["insight_customer_count"],
        push["phone_customer_count"],
        push["plan_read_rate"],
    ) == (1, 1, 50.0)
    assert (push["insight_count"], push["phone_count"]) == (2, 2)
    assert (ask["suc_execute_job"], ask["read_tasks"], ask["read_rate"]) == (
        2,
        2,
        100.0,
    )
    assert ask["recommended_customers"] == 2
    assert ask["read_customer_count"] == 2  # 包含当期点击、前期生成的 OLD。
    assert ask["active_manager_count"] is None
    assert (
        other["suc_execute_job"],
        other["skill_count"],
        other["read_tasks"],
    ) == (1, 1, 1)
    assert (
        other["recommended_customers"] is None
        and other["plan_read_rate"] is None
    )
    assert other["insight_count"] is None and other["phone_count"] is None


@pytest.mark.parametrize("group_by", ["overall", "branch", "org"])
def test_grouping_and_empty_dimensions(db, scope, group_by):
    scoped = replace(scope, group_by=group_by)
    rows = assemble(execute(db, scoped), group_by)
    assert len(rows) == (3 if group_by == "overall" else 9)
    if group_by == "org":
        assert {(row["first_bbk_id"], row["org_id"]) for row in rows} == {
            ("001", "01"),
            ("002", "01"),
            ("003", "03"),
        }
        assert all(
            row["skill_count"] == 0
            for row in rows
            if row["first_bbk_id"] == "003"
        )


def test_filter_values_bound_and_empty_filter_result(db, scope):
    scoped = replace(scope, first_bbk_id="001' OR 1=1 --")
    assert all(
        "001' OR 1=1 --" not in sql
        for sql, _ in build_queries(scoped).values()
    )
    assert assemble(execute(db, scoped), "overall") == []
    filtered = replace(scope, group_by="org", first_bbk_id="001", org_id="01")
    assert len(assemble(execute(db, filtered), "org")) == 3


def test_conflicting_roster_detected_before_filter(db, scope):
    db.execute(
        "INSERT INTO jkh_user_inf VALUES ('alice','2026-09-13','002','02','乙','乙支行')"
    )
    assert execute(db, replace(scope, first_bbk_id="001"))[
        "roster_conflicts"
    ] == [{"user_id": "alice"}]


def test_click_actor_and_active_owner_use_independent_rosters(db, scope):
    db.execute("UPDATE swe_cron_jobs SET tenant_id = 'bob' WHERE id = 'j1'")
    db.execute(
        "UPDATE swe_html_preview_click_events SET user_id = 'bob' WHERE cron_task_id = 'j1'"
    )
    rows = assemble(execute(db, replace(scope, group_by="branch")), "branch")
    push = {
        row["first_bbk_id"]: row
        for row in rows
        if row["task_type"] == "push_plan"
    }
    assert (
        push["001"]["suc_execute_job"] == 1
        and push["001"]["active_manager_count"] == 0
    )
    assert (
        push["002"]["active_manager_count"] == 1
        and push["002"]["read_customer_count"] == 1
    )
    assert push["002"]["plan_read_rate"] is None


def test_ask_not_read_by_other_user_or_exposure(db, scope):
    db.execute(
        "UPDATE swe_html_preview_click_events SET user_id = 'bob' WHERE trace_id = 'a1'"
    )
    assert execute(db, scope)["ask_tasks"][0]["read_tasks"] == 2
    db.execute(
        "UPDATE swe_html_preview_click_events SET user_id = 'alice', event_type = 'module_exposure' WHERE trace_id = 'a1'"
    )
    assert execute(db, scope)["ask_tasks"][0]["read_tasks"] == 2


def test_deleted_job_retains_execution_and_customer_history(db, scope):
    db.execute(
        "UPDATE swe_cron_jobs SET deleted_at = '2026-09-14', status = 'deleted' WHERE id = 'j1'"
    )
    push = assemble(execute(db, scope), "overall")[0]
    assert push["suc_execute_job"] == 1 and push["recommended_customers"] == 2
    assert push["skill_count"] == 0 and push["active_manager_count"] == 0
    assert push["read_customer_count"] == 0


def test_anti_join_uses_all_execution_history(db, scope):
    db.execute(
        "INSERT INTO swe_cron_executions VALUES (9,'j1','alice','a1','2026-09-12','success','success',1)"
    )
    ask = assemble(execute(db, scope), "overall")[1]
    assert ask["suc_execute_job"] == 1 and ask["recommended_customers"] == 1


def test_overall_distinct_not_sum_of_branches(db, scope):
    db.execute(
        "INSERT INTO swe_cron_executions VALUES (9,'j1','bob','p1','2026-09-13 12:00:00','success','success',1)"
    )
    overall = assemble(execute(db, scope), "overall")[0]
    branch = assemble(execute(db, replace(scope, group_by="branch")), "branch")
    assert overall["recommended_customers"] == 2
    assert (
        sum(
            row["recommended_customers"]
            for row in branch
            if row["task_type"] == "push_plan"
        )
        == 3
    )


@pytest.mark.parametrize(
    "numerator,denominator,expected",
    [(1, 3, 33.33), (0, 1, 0.0), (1, 0, None), (None, 3, None), (2, 1, 200.0)],
)
def test_percentages(numerator, denominator, expected):
    assert percentage(numerator, denominator) == expected


def test_validation_and_binding(scope):
    assert bind("x=:source_id OR y=:source_id", {"source_id": "S"}) == (
        "x=%s OR y=%s",
        ("S", "S"),
    )
    with pytest.raises(ValueError):
        replace(scope, group_by="org_id; DROP TABLE x")
    with pytest.raises(ValueError):
        replace(scope, stop=scope.start)
    with pytest.raises(ValueError):
        replace(scope, source_id="")
