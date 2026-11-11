# -*- coding: utf-8 -*-
"""在现有日志中保存选定组的冻结快照、注册进度和迁移事实。"""

import copy
import json
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from .contracts import ManagedCronNotFound
from .date_policy import reason
from .definition import canonical, request_hash, stable_id
from .migration_policy import KEYS, migration_fact
from .operations import (
    JOURNAL,
    WHERE_JOB,
    LeaseLost,
    ManagementConflict,
    ManagementStore,
    utc_now,
)
from .storage import insert, transaction, update

PUBLIC_COLUMNS = "operation_id,tenant_id,source_id,agent_id,job_id,actor_id,status,lease_owner,lease_until,progress_json,created_at"


def public_progress(payload):
    """轮询只读取轻量摘要，不反复拉取整组任务正文快照。"""
    groups = [
        {key: value for key, value in group.items() if key != "snapshot"}
        for group in payload["groups"]
    ]
    return {
        "groups": groups,
        "root_count": len(payload["job_ids"]),
        "member_count": sum(group.get("member_count", 0) for group in groups),
    }


def key_of(row):
    return tuple(row[name] for name in KEYS)


def journal_row(caller, operation_id, job_id, action, payload, status):
    now = utc_now()
    return {
        "operation_id": operation_id,
        **dict(zip(KEYS, caller.job_key(job_id))),
        "actor_id": caller.actor_id,
        "action": action,
        "request_id": operation_id,
        "request_hash": request_hash(payload),
        "expected_version": 1,
        "payload_json": canonical(payload),
        "progress_json": (
            canonical(public_progress(payload))
            if action == "migration_batch"
            else "{}"
        ),
        "status": status,
        "stage": "snapshot",
        "version": 1,
        "lease_owner": "",
        "lease_until": None,
        "active_key": None,
        "error_code": None,
        "created_at": now,
        "updated_at": now,
    }


class SelectedMigrationStore:
    def __init__(self, db):
        self.db = db

    async def submit(self, caller, body):
        jobs = sorted(set(body["job_ids"]))
        for job in jobs:
            caller.job_key(job)
        operation_id = stable_id(caller, body["request_id"], "migration_batch")
        payload = {"job_ids": jobs, "groups": []}
        async with transaction(self.db) as tx:
            existing = await tx.fetch_one(
                f"SELECT * FROM {JOURNAL} WHERE operation_id=%s FOR UPDATE",
                (operation_id,),
            )
            if existing:
                if json.loads(existing["payload_json"])["job_ids"] != jobs:
                    raise ManagementConflict("cron_migration_request_conflict")
                return existing
            row = journal_row(
                caller,
                operation_id,
                operation_id,
                "migration_batch",
                payload,
                "previewing",
            )
            await insert(tx, JOURNAL, row)
            return row

    async def get(self, caller, operation_id, *, public=False):
        columns = PUBLIC_COLUMNS if public else "*"
        row = await self.db.fetch_one(
            f"SELECT {columns} FROM {JOURNAL} WHERE operation_id=%s AND action='migration_batch'",
            (operation_id,),
        )
        if (
            not row
            or key_of(row)[:3] != caller.job_key(operation_id)[:3]
            or (row["actor_id"] != caller.actor_id and not caller.is_manager)
        ):
            raise ManagedCronNotFound("cron_migration_not_found")
        return row

    async def confirm(self, caller, operation_id):
        async with transaction(self.db) as tx:
            await tx.fetch_one(
                f"SELECT operation_id FROM {JOURNAL} WHERE operation_id=%s FOR UPDATE",
                (operation_id,),
            )
            row = await SelectedMigrationStore(tx).get(caller, operation_id)
            if row["status"] == "completed":
                return row
            if row.get("lease_owner") and _future(row.get("lease_until")):
                raise ManagementConflict("cron_migration_in_progress")
            if row["status"] not in {
                "awaiting_confirmation",
                "failed",
                "running",
                "previewing",
            }:
                raise ManagementConflict("cron_migration_preview_not_ready")
            groups = json.loads(row["payload_json"])["groups"]
            if row["status"] == "previewing" or (
                row["status"] != "awaiting_confirmation"
                and any(not group.get("snapshot") for group in groups)
            ):
                # 采集失败必须重新预览，再由用户确认实际范围。
                status = "previewing"
            else:
                status = "running"
            await update(
                tx,
                JOURNAL,
                {"status": status, "lease_owner": "", "lease_until": None},
                "operation_id=%s",
                (operation_id,),
            )
            row["status"] = status
            return row

    async def claim(self, caller, operation_id):
        async with transaction(self.db) as tx:
            await tx.fetch_one(
                f"SELECT operation_id FROM {JOURNAL} WHERE operation_id=%s FOR UPDATE",
                (operation_id,),
            )
            row = await SelectedMigrationStore(tx).get(caller, operation_id)
            if row["status"] not in {"previewing", "running"} or (
                row.get("lease_owner") and _future(row.get("lease_until"))
            ):
                return None
            row.update(
                lease_owner=str(uuid4()),
                lease_until=utc_now() + timedelta(seconds=120),
            )
            await update(
                tx,
                JOURNAL,
                {k: row[k] for k in ("lease_owner", "lease_until")},
                "operation_id=%s",
                (operation_id,),
            )
            return row

    async def fence(self, tx, batch):
        row = await tx.fetch_one(
            f"SELECT * FROM {JOURNAL} WHERE operation_id=%s FOR UPDATE",
            (batch["operation_id"],),
        )
        if (
            not row
            or row["lease_owner"] != batch["lease_owner"]
            or not _future(row["lease_until"])
        ):
            raise LeaseLost()
        await update(
            tx,
            JOURNAL,
            {"lease_until": utc_now() + timedelta(seconds=120)},
            "operation_id=%s",
            (batch["operation_id"],),
        )

    async def save(self, batch, payload, status=None):
        async with transaction(self.db) as tx:
            await self.fence(tx, batch)
            values = {
                "payload_json": canonical(payload),
                "progress_json": canonical(public_progress(payload)),
                "updated_at": utc_now(),
            }
            if status:
                values.update(status=status, lease_owner="", lease_until=None)
            await update(
                tx,
                JOURNAL,
                values,
                "operation_id=%s",
                (batch["operation_id"],),
            )

    async def prepare_group(self, batch, snapshot):
        async with transaction(self.db) as tx:
            await self.fence(tx, batch)
            result = []
            for member in sorted(
                snapshot["members"],
                key=lambda item: key_of(item["definition"]),
            ):
                result.append(await self._member(tx, batch, member))
            return result

    async def _member(self, tx, batch, member):
        definition, state = member["definition"], member["state"]
        key = key_of(definition)
        existing = await tx.fetch_one(
            f"SELECT * FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
            key,
        )
        binding = await tx.fetch_one(
            f"SELECT * FROM swe_cron_job_states WHERE {WHERE_JOB}",
            key,
        )
        fact = await migration_fact(tx, key)
        _check_existing(
            existing,
            binding,
            member,
            completed=bool(fact and fact["status"] == "completed"),
        )
        if fact:
            if fact["status"] == "completed":
                return None
            operation = await tx.fetch_one(
                f"SELECT * FROM {JOURNAL} WHERE operation_id=%s",
                (fact["operation_id"],),
            )
            if (
                json.loads(operation["payload_json"]).get("batch_id")
                != batch["operation_id"]
            ):
                raise ManagementConflict("cron_migration_other_operation")
            return operation
        imported = not existing or existing["management_owner"] == "legacy"
        if (
            existing
            and not imported
            and (
                existing.get("deleted_at")
                or not binding
                or binding["management_status"] != "ready"
            )
        ):
            raise ManagementConflict("cron_migration_existing_job_not_ready")
        operation = _member_operation(batch, member, imported)
        await insert(tx, JOURNAL, operation)
        if not imported:
            return None
        await _write_member(tx, member, existing, binding)
        return operation

    async def step(self, batch, operation, progress, field=None):
        async with transaction(self.db) as tx:
            await self.fence(tx, batch)
            await update(
                tx,
                JOURNAL,
                {
                    "progress_json": canonical(progress),
                    "updated_at": utc_now(),
                },
                "operation_id=%s",
                (operation["operation_id"],),
            )
            if field:
                await update(
                    tx,
                    "swe_cron_job_states",
                    {field: progress["external_id"]},
                    WHERE_JOB,
                    key_of(operation),
                )
        operation["progress_json"] = canonical(progress)

    async def publish(self, batch, operations):
        async with transaction(self.db) as tx:
            await self.fence(tx, batch)
            for operation in operations:
                await update(
                    tx,
                    "swe_cron_job_states",
                    {"management_status": "ready", "updated_at": utc_now()},
                    WHERE_JOB,
                    key_of(operation),
                )
                await update(
                    tx,
                    JOURNAL,
                    {
                        "status": "completed",
                        "stage": "completed",
                        "active_key": None,
                        "updated_at": utc_now(),
                    },
                    "operation_id=%s",
                    (operation["operation_id"],),
                )


def _future(value):
    from datetime import datetime

    return bool(
        value
        and (
            datetime.fromisoformat(value) if isinstance(value, str) else value
        )
        > utc_now(),
    )


def lease_active(row):
    return bool(row.get("lease_owner") and _future(row.get("lease_until")))


def _check_existing(existing, binding, member, *, completed=False):
    if not existing:
        if binding:
            raise ManagementConflict("cron_migration_orphan_state")
        return
    definition = member["definition"]
    if key_of(existing) != key_of(definition) or (
        existing.get("deleted_at") and not completed
    ):
        raise ManagementConflict("cron_migration_identity_conflict")
    if existing.get("creator_user_id") != definition["creator_user_id"]:
        raise ManagementConflict("cron_migration_owner_conflict")
    if tuple(existing.get("parent_" + k) for k in KEYS) != tuple(
        definition.get("parent_" + k) for k in KEYS
    ):
        raise ManagementConflict("cron_migration_parent_conflict")
    if not binding or any(
        binding.get(k) != member["state"].get(k)
        for k in ("task_chat_id", "task_session_id")
    ):
        raise ManagementConflict("cron_migration_binding_conflict")


def _member_operation(batch, member, imported):
    from .identity import CallerIdentity

    definition, state = member["definition"], member["state"]
    key = key_of(definition)
    operation_id = str(uuid5(NAMESPACE_URL, "selected-cron:" + canonical(key)))
    caller = CallerIdentity(*key[:3], batch["actor_id"])
    payload = {
        "batch_id": batch["operation_id"],
        "spec": definition["spec_json"],
        "dispatch_state": state["dispatch_state"],
        "legacy_runtime": member.get("legacy_runtime", {}),
        "migration": {
            "version": 1,
            "imported": imported,
            "identity": list(key),
            "chat_id": state["task_chat_id"],
            "session_id": state["task_session_id"],
            "history_max_id": member["history_max_id"],
            "history_read_ids": member.get("history_read_ids", []),
        },
    }
    row = journal_row(
        caller,
        operation_id,
        key[3],
        "migrate",
        payload,
        "running" if imported else "completed",
    )
    row["active_key"] = request_hash(key) if imported else None
    row["expected_version"] = definition.get("definition_version", 1)
    return row


async def _write_member(tx, member, existing, binding):
    source, state = member["definition"], copy.deepcopy(member["state"])
    spec, now = copy.deepcopy(source["spec_json"]), utc_now()
    for field in ("external_job_id", "batch_dispatch_external_job_id"):
        spec.get("meta", {}).pop(field, None)
    key = key_of(source)
    values = {
        **ManagementStore.definition_values(spec),
        "management_owner": "scheduler",
        "creator_user_id": source["creator_user_id"],
        "job_origin": source.get("job_origin", "manual"),
        "definition_version": 1,
        "updated_at": now,
    }
    for name in (
        *("parent_" + k for k in KEYS),
        "offset_minutes",
        "enable_offset",
        "offset_window_hours",
        "source_definition_version",
        "original_cron_expr",
    ):
        if name in source:
            values[name] = source[name]
    identity = dict(zip(KEYS, key))
    if existing:
        await update(tx, "swe_cron_jobs_v2", values, WHERE_JOB, key)
    else:
        await insert(
            tx,
            "swe_cron_jobs_v2",
            {**identity, **values, "created_at": now},
        )
    state.update(
        ordinary_external_job_id=None,
        batch_external_job_id=None,
        management_status="migrating",
        state_version=1,
        management_version=1,
        state_source="selected_migration",
        date_window_paused=bool(reason(spec)),
        date_window_sync_required=False,
        updated_at=now,
    )
    state.setdefault("unread_execution_count", 0)
    state.setdefault("has_scheduled_result", False)
    if isinstance(state.get("next_run_times_json"), list):
        state["next_run_times_json"] = canonical(state["next_run_times_json"])
    if binding:
        await update(tx, "swe_cron_job_states", state, WHERE_JOB, key)
    else:
        await insert(
            tx,
            "swe_cron_job_states",
            {**identity, **state, "created_at": now},
        )
