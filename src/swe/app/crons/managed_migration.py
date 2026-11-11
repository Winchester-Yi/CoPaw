# -*- coding: utf-8 -*-
"""SWE 独立核验共库迁入事实，不信请求或 spec 中的资源保留标记。"""

import json

KEYS = ("tenant_id", "source_id", "agent_id", "job_id")


async def uses_original_resources(db, operation):
    key = tuple(operation[name] for name in KEYS)
    where = " AND ".join(f"{name}=%s" for name in KEYS)
    rows = await db.fetch_all(
        f"SELECT * FROM swe_cron_registration_journal WHERE {where} AND action='migrate' LIMIT 2",
        key,
    )
    if not rows:
        return False
    if len(rows) != 1:
        raise ValueError("cron_migration_fact_conflict")
    record = rows[0]
    fact = json.loads(record["payload_json"])["migration"]
    if (
        tuple(record[name] for name in KEYS) != key
        or tuple(fact["identity"]) != key
        or fact["version"] != 1
        or type(fact["imported"]) is not bool
    ):
        raise ValueError("cron_migration_fact_conflict")
    if not fact["imported"]:
        return False
    if (
        not fact["chat_id"]
        or not fact["session_id"]
        or fact["chat_id"] != operation["task_chat_id"]
        or fact["session_id"] != operation["task_session_id"]
    ):
        raise ValueError("cron_migration_binding_conflict")
    return True
