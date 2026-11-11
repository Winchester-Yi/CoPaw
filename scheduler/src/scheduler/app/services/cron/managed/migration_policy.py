# -*- coding: utf-8 -*-
"""迁入策略只信共库登记，客户端 meta 不能关闭旧表投影。"""

import json

KEYS = ("tenant_id", "source_id", "agent_id", "job_id")
WHERE = " AND ".join(f"{key}=%s" for key in KEYS)


async def migration_fact(db, key):
    rows = await db.fetch_all(
        f"SELECT * FROM swe_cron_registration_journal WHERE {WHERE} AND action='migrate' LIMIT 2",
        tuple(key),
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("cron_migration_fact_conflict")
    return validated_fact(rows[0], key)


def validated_fact(row, key):
    """已从共库查询出的日志复用同一校验，列表无需逐行重复查询。"""
    try:
        fact = json.loads(row["payload_json"])["migration"]
        valid = (
            tuple(row[name] for name in KEYS) == tuple(key)
            and fact["version"] == 1
            and tuple(fact["identity"]) == tuple(key)
            and type(fact["imported"]) is bool
            and all(
                isinstance(fact[name], str) and fact[name]
                for name in ("chat_id", "session_id")
            )
            and type(fact["history_max_id"]) is int
            and fact["history_max_id"] >= 0
            and row["status"] in {"pending", "running", "failed", "completed"}
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("cron_migration_fact_invalid")
    return {
        **fact,
        "status": row["status"],
        "operation_id": row["operation_id"],
    }


async def imported_binding(db, key, state=None):
    fact = await migration_fact(db, key)
    if fact is None or not fact["imported"]:
        return None
    if state is not None and (
        state.get("task_chat_id") != fact["chat_id"]
        or state.get("task_session_id") != fact["session_id"]
    ):
        raise ValueError("cron_migration_binding_conflict")
    return fact
