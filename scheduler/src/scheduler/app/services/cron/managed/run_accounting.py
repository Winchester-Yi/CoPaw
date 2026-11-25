# -*- coding: utf-8 -*-
"""共用运行占位定义，不将主执行完成误当作异步链路已结束。"""

import json

DIRECT_DISPATCH_KINDS = ("ordinary", "manual")


def occupied_run_sql(alias=""):
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}status IN ('dispatching','accepted','running') OR "
        f"({prefix}status='success' AND {prefix}settled_status IS NULL))"
    )


def standalone_direct_sql(alias):
    """独立普通和手动执行直接派发，已关联批次的执行按批任务统计。"""
    return (
        f"({alias}.kind IN {DIRECT_DISPATCH_KINDS} AND NOT EXISTS ("
        "SELECT 1 FROM swe_cron_managed_batch_members m "
        f"WHERE m.source_id={alias}.source_id "
        f"AND m.current_execution_id={alias}.execution_id "
        "UNION ALL SELECT 1 FROM swe_cron_managed_batch_members m "
        f"WHERE m.source_id={alias}.source_id "
        f"AND m.initial_execution_id={alias}.execution_id))"
    )


def is_text_run(row):
    return json.loads(row.get("spec_json") or "{}").get("task_type") == "text"


def direct_dispatch_sql(alias):
    """纯文本使用冻结执行类型，独立执行沿用原有直接派发条件。"""
    task_type = f"JSON_UNQUOTE(JSON_EXTRACT({alias}.spec_json,'$.task_type'))"
    return (
        f"({standalone_direct_sql(alias)} OR COALESCE({task_type},'')='text')"
    )
