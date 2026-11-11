# -*- coding: utf-8 -*-
"""按批次成员回溯业务尝试，按执行主键读取，不扫描同任务的其他批次。"""

import json

from . import managed_board_sql as sql
from .managed_board import ManagedBoardUnavailable, member_error_message


async def member_attempts(db, source, batch_id, member_id):
    member = await db.fetch_one(
        f"SELECT m.*,c.logical_fire_at,c.members_json FROM {sql.MEMBERS} m "
        f"JOIN {sql.RECEIPTS} c ON c.batch_id=m.batch_id AND c.source_id COLLATE utf8mb4_bin=m.source_id "
        "WHERE m.batch_id=%s AND m.member_id=%s AND m.source_id=%s "
        "AND m.source_id COLLATE utf8mb4_bin=%s",
        (batch_id, member_id, source, source),
    )
    if member is None:
        raise LookupError("cron_batch_member_not_found")
    identity = tuple(member[key] for key in sql.KEYS)
    _validate_manifest(member, identity)
    current = member["current_execution_id"]
    attempts = []
    for number in range(member["business_attempt"], 0, -1):
        run = await _read_attempt(db, current, identity)
        current = _previous_attempt(member, run, number)
        attempts.append(
            {
                "execution_id": run["execution_id"],
                "business_attempt": number,
                "current": not attempts,
                "status": run["status"],
                "settled_status": run["settled_status"],
                "started_at": sql.time_text(run["started_at"]),
                "finished_at": sql.time_text(
                    run["settled_at"] or run["finished_at"],
                ),
                "error_type": run["history_error_type"],
                "error_message": member_error_message(run),
            },
        )
    return {"items": list(reversed(attempts))}


def _validate_manifest(member, identity):
    try:
        item = json.loads(member["members_json"])[member["dispatch_order"]]
        if (
            tuple(item["identity"]) == identity
            and item["execution_id"] == member["initial_execution_id"]
            and member["member_id"] == member["initial_execution_id"]
            and 1 <= member["business_attempt"] <= 1000
        ):
            return
    except (ValueError, TypeError, IndexError, KeyError):
        pass
    raise ManagedBoardUnavailable("cron_batch_relation_incomplete")


async def _read_attempt(db, execution_id, identity):
    if not isinstance(execution_id, str) or not execution_id:
        raise ManagedBoardUnavailable("cron_batch_attempt_history_incomplete")
    terms = " AND ".join(f"r.{key} COLLATE utf8mb4_bin=%s" for key in sql.KEYS)
    row = await db.fetch_one(
        "SELECT r.*, h.error_type AS history_error_type, "
        "h.error_message AS history_error_message "
        f"FROM {sql.RUNS} r LEFT JOIN swe_cron_executions h "
        "ON h.id=r.history_execution_id "
        "AND h.tenant_id COLLATE utf8mb4_bin=r.tenant_id "
        "AND h.job_id COLLATE utf8mb4_bin=r.job_id "
        f"WHERE r.execution_id=%s AND {terms}",
        (execution_id, *identity),
    )
    if not row or row["execution_id"] != execution_id:
        raise ManagedBoardUnavailable("cron_batch_attempt_history_incomplete")
    return row


def _previous_attempt(member, run, number):
    meta = json.loads(run["dispatch_meta_json"])
    if (
        meta.get("protocol") != "scheduler_managed_v1"
        or run["scheduled_fire_at"] != member["logical_fire_at"]
    ):
        raise ManagedBoardUnavailable("cron_batch_attempt_history_incomplete")
    initial = member["initial_execution_id"]
    if number == 1 and run["execution_id"] == initial:
        return None
    if (
        number > 1
        and run["execution_id"] != initial
        and meta.get("batch_id") == member["batch_id"]
        and meta.get("batch_member_id") == member["member_id"]
        and meta.get("business_attempt") == number
    ):
        return meta.get("previous_execution_id")
    raise ManagedBoardUnavailable("cron_batch_attempt_history_incomplete")
