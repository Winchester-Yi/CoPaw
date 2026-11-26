# -*- coding: utf-8 -*-
"""受理新版平台触发和手动批触发，原子保存执行快照及触发回执。"""

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from .callback_contract import (
    PROTOCOL,
    binding_context,
    trigger_id,
    trigger_times,
)
from .contracts import ManagedCronNotFound
from .definition import canonical
from .dispatch_state_store import CONTROLS
from .distribution import _children
from .executions import ExecutionConflict, ExecutionRequest, ExecutionStore
from .identity import CallerIdentity, identity_value
from .operations import IDENTITY_FIELDS, WHERE_JOB
from .batch_order import ordered_members
from .repository import ManagedCronRepository
from .storage import insert, transaction
from .batch_members import active_batch_id, record_batch_members
from .date_policy import reason as date_reason
from .calendar_policy import TriggerObservation, admission
from ..workday_calendar import CalendarService, CalendarStore

RECEIPTS = "swe_cron_managed_trigger_receipts"
logger = logging.getLogger(__name__)


def _allows_empty_batch(targets, kind):
    """仍有批模式对象但暂时无人可执行时，保留既有零成员看板回执。"""
    return kind == "batch" and any(
        row["state_dispatch_state"] == "batch_enabled" for row in targets
    )


def _member_skip_reason(row, kind, now):
    if row["deleted_at"]:
        return "job_deleted"
    pause = row["state_pause_reason"]
    if pause:
        return (
            pause
            if pause in {"manual", "auto_unread_threshold"}
            else "job_paused"
        )
    if not row["enabled"]:
        return "job_disabled"
    if reason := date_reason(row, now):
        return reason
    if row["state_management_status"] != "ready":
        return "job_not_ready"
    state = row["state_dispatch_state"]
    if state == "batch_paused":
        return "batch_paused"
    wanted = "ordinary" if kind == "ordinary" else "batch_enabled"
    return "job_not_in_batch_mode" if state != wanted else None


def _partition_targets(targets, kind, now):
    selected, skipped = [], []
    for index, row in enumerate(targets):
        reason = _member_skip_reason(row, kind, now)
        if reason is None:
            selected.append(row)
        elif kind == "batch":
            skipped.append(
                {
                    "tenant_id": row["tenant_id"],
                    "agent_id": row["agent_id"],
                    "job_id": row["job_id"],
                    "job_name": row["name"],
                    "member_role": "parent" if index == 0 else "child",
                    "reason": reason,
                },
            )
    return selected, skipped


class BatchAlreadyActive(ExecutionConflict):
    """同一父任务仍有未结束的批次。"""

    def __init__(self, batch_id):
        super().__init__("cron_batch_already_active")
        self.batch_id = batch_id


class TriggerGrant:
    """已授权触发只可为当前父任务及其关联成员登记执行。"""

    def __init__(self, source_key, kind):
        self.source_key = source_key
        self.kind = kind

    async def authorize(self, db, caller, job_id):
        key = caller.job_key(job_id)
        row = await ManagedCronRepository(db)._get_row(
            key,
            include_deleted=True,
        )
        if (
            key != self.source_key
            and tuple(row.get(f"parent_{name}") for name in IDENTITY_FIELDS)
            != self.source_key
        ):
            raise ManagedCronNotFound("cron_trigger_job_not_related")
        expected = "ordinary" if self.kind == "ordinary" else "batch_enabled"
        if row["state_dispatch_state"] != expected:
            raise ExecutionConflict("cron_entry_mode_changed")
        return row


class ManagedSchedulingService:
    def __init__(self, db, *, clock=None, calendar=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.calendar = calendar or CalendarService(store=CalendarStore(db))

    async def accept(self, caller, body, kind):
        if kind not in {"ordinary", "batch"}:
            raise ValueError("invalid_cron_callback_kind")
        job_id = identity_value(
            str(body.get("job_id") or body.get("parent_job_id") or ""),
        )
        key = caller.job_key(job_id)
        root = await ManagedCronRepository(self.db)._get_row(
            key,
            include_deleted=True,
        )
        binding = await binding_context(self.db, root, body, kind)
        # 平台没有每次触发的标识，且约定每次请求都是新触发。
        receipt_id = str(uuid4())
        identifier = trigger_id(body) or receipt_id
        now = self.clock().astimezone(timezone.utc)
        logical, physical, time_basis = trigger_times(body, binding, now)
        # 上游 HTTP 和日期缓存写入不能占用用户任务事务或批次行锁。
        decision = await admission(
            json.loads(root["spec_json"]),
            logical,
            self.calendar,
            root["definition_version"],
        )
        decision["time_basis"] = time_basis
        observation = TriggerObservation(
            binding,
            kind,
            logical,
            physical,
            receipt_id,
            now,
            decision,
        )
        return await self._record_trigger(caller, key, observation, identifier)

    async def run_batch(self, caller, job_id):
        root = await ManagedCronRepository(self.db)._authorized_row(
            caller,
            job_id,
            False,
        )
        now = self.clock().astimezone(timezone.utc)
        receipt_id = str(uuid4())
        observation = TriggerObservation(
            {
                "definition_version": root["definition_version"],
                # 手动受理没有平台绑定或注册操作，回执兼容现有非空列。
                "external_id": "",
                "operation_id": "",
            },
            "batch",
            now,
            now,
            receipt_id,
            now,
            {"allowed": True, "time_basis": "manual"},
        )
        return await self._record_trigger(
            caller,
            caller.job_key(job_id),
            observation,
            receipt_id,
        )

    async def _record_trigger(self, caller, key, observation, identifier):
        job_id = key[-1]
        binding = observation.binding
        kind = observation.kind
        now = observation.now
        receipt_id = observation.receipt_id
        time_basis = observation.calendar["time_basis"]
        async with transaction(self.db) as tx:
            await tx.fetch_one(
                f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            repository = ManagedCronRepository(tx)
            if time_basis == "manual":
                root = await repository._authorized_row(caller, job_id, False)
                if root.get("parent_job_id"):
                    raise ExecutionConflict("cron_batch_run_requires_parent")
                existing_batch = await active_batch_id(tx, key)
                if existing_batch is not None:
                    raise BatchAlreadyActive(existing_batch)
            else:
                root = await repository._get_row(key, include_deleted=True)
            response, members, skipped = await self._accept_locked(
                tx,
                caller,
                root,
                observation,
            )
            response["trigger_time_basis"] = time_basis
            receipt_response = (
                {**response, "skipped_members": skipped}
                if response.get("batch_id")
                else response
            )
            await insert(
                tx,
                RECEIPTS,
                {
                    "receipt_id": receipt_id,
                    "callback_key": None,
                    **dict(zip(IDENTITY_FIELDS, key)),
                    "external_job_id": binding["external_id"],
                    "operation_id": binding["operation_id"],
                    "kind": kind,
                    "trigger_id": identifier,
                    "logical_fire_at": observation.logical.replace(
                        tzinfo=None,
                    ),
                    "physical_fire_at": observation.physical.replace(
                        tzinfo=None,
                    ),
                    "batch_id": response.get("batch_id"),
                    "status": (
                        "accepted" if response["accepted"] else "skipped"
                    ),
                    "response_json": canonical(receipt_response),
                    "members_json": canonical(members),
                    "created_at": now.replace(tzinfo=None),
                },
            )
            await record_batch_members(tx, response.get("batch_id"), now=now)
        logger.info(
            "%s触发已持久处理 receipt_id=%s job_id=%s kind=%s accepted=%s",
            "手动批调度" if time_basis == "manual" else "平台",
            receipt_id,
            job_id,
            kind,
            response["accepted"],
        )
        return response

    async def _accept_locked(
        self,
        tx,
        caller,
        root,
        observation,
    ):
        kind = observation.kind
        receipt_id = observation.receipt_id
        now = observation.now
        manual = observation.calendar["time_basis"] == "manual"
        reason = await self._trigger_gate(tx, root, observation)
        if reason:
            return (
                {
                    "job_id": root["job_id"],
                    "accepted": False,
                    "reused": False,
                    "status": "skipped",
                    "reason": reason,
                },
                [],
                [],
            )
        targets = [root]
        if kind == "batch":
            children = await _children(tx, caller.job_key(root["job_id"]), [])
            targets += [
                await ManagedCronRepository(tx)._get_row(
                    tuple(row[name] for name in IDENTITY_FIELDS),
                    include_deleted=True,
                )
                for row in children
            ]
        selected, skipped = _partition_targets(targets, kind, now)
        if not selected and (manual or not _allows_empty_batch(targets, kind)):
            return (
                {
                    "job_id": root["job_id"],
                    "accepted": False,
                    "reused": False,
                    "status": "skipped",
                    "reason": "job_not_eligible",
                },
                [],
                [],
            )
        batch_id = (
            str(uuid5(NAMESPACE_URL, "managed-batch:" + receipt_id))
            if kind == "batch"
            else None
        )
        members = await self._enqueue_members(
            tx,
            caller,
            root,
            selected,
            batch_id,
            observation,
        )
        if kind == "ordinary":
            return (
                {
                    "job_id": root["job_id"],
                    "execution_id": members[0]["execution_id"],
                    "definition_version": members[0]["definition_version"],
                    "accepted": True,
                    "reused": members[0]["reused"],
                    "status": members[0]["status"],
                },
                members,
                [],
            )
        return (
            {
                "batch_id": batch_id,
                "parent_job_id": root["job_id"],
                "accepted": True,
                "reused": False,
                "enqueued_intents": len(members),
                "status": "queued",
            },
            members,
            skipped,
        )

    async def _trigger_gate(self, tx, root, observation):
        reason = self._gate(
            root,
            observation.binding,
            observation.physical,
            observation.now,
        )
        if not reason and not observation.calendar["allowed"]:
            reason = observation.calendar["skipped"]
        if observation.kind != "batch":
            return reason
        control = await tx.fetch_one(
            f"SELECT state FROM {CONTROLS} WHERE {WHERE_JOB}",
            tuple(root[name] for name in IDENTITY_FIELDS),
        )
        state = control["state"] if control else root["state_dispatch_state"]
        if state == "batch_paused":
            return "batch_paused"
        if (
            observation.calendar["time_basis"] == "manual"
            and state != "batch_enabled"
        ):
            return reason or "job_not_in_batch_mode"
        return reason

    @staticmethod
    def _gate(root, binding, physical, now):
        if root["deleted_at"]:
            return "job_deleted"
        if binding.get("skip_reason"):
            return binding["skip_reason"]
        if root["definition_version"] != binding["definition_version"]:
            return "binding_definition_changed"
        if root["state_management_status"] != "ready":
            return "job_not_ready"
        if date_reason(root, now):
            return "job_outside_date_range"
        grace = json.loads(root["spec_json"])["runtime"][
            "misfire_grace_seconds"
        ]
        if physical > now + timedelta(seconds=5) or now - physical > timedelta(
            seconds=grace,
        ):
            return "trigger_outside_window"
        return None

    async def _enqueue_members(
        self,
        tx,
        caller,
        root,
        selected,
        batch_id,
        observation,
    ):
        logical = observation.logical
        receipt_id = observation.receipt_id
        kind = observation.kind

        key = tuple(root[name] for name in IDENTITY_FIELDS)
        ranked = await ordered_members(
            tx,
            root,
            selected,
            logical,
            self.clock(),
            kind,
        )
        store = ExecutionStore(tx, grant=TriggerGrant(key, kind))
        result = []
        first_order = None
        follow_level = 0
        if kind == "batch":
            state = await tx.fetch_one(
                "SELECT batch_follow_level FROM swe_cron_job_states "
                f"WHERE {WHERE_JOB}",
                key,
            )
            follow_level = (
                int(state["batch_follow_level"] or 0) if state else 0
            )
        for entry in ranked:
            row = entry["definition"]
            rank = entry["payload"]["dispatch_priority"]
            actor = CallerIdentity(
                row["tenant_id"],
                row["source_id"],
                row["agent_id"],
                caller.actor_id,
            )
            due = entry["due_at"]
            metadata = {
                "protocol": PROTOCOL,
                "trigger_receipt_id": receipt_id,
                "batch_id": batch_id,
                "parent_scheduled_fire_at": logical.isoformat(),
                "dispatch_priority": rank,
                "dispatch_order": entry["dispatch_order"],
                "viewer_heat_score": entry["viewer_heat_score"],
                "calendar_admission": observation.calendar,
            }
            if kind == "batch":
                metadata["batch_created_at"] = (
                    observation.now.astimezone(timezone.utc)
                    .replace(tzinfo=None)
                    .isoformat(" ")
                )
                metadata["batch_follow_level"] = follow_level
                if first_order is not None:
                    metadata["batch_queue_order"] = first_order
            command = ExecutionRequest(
                kind,
                receipt_id,
                logical,
                dispatch_meta=metadata,
                not_before_at=due,
            )
            run, reused = await store.enqueue_in_transaction(
                tx,
                actor,
                row["job_id"],
                command,
            )
            if first_order is None and kind == "batch":
                initial = await tx.fetch_one(
                    "SELECT queue_order FROM swe_cron_execution_runs WHERE execution_id=%s",
                    (run["execution_id"],),
                )
                first_order = initial["queue_order"]
            result.append(
                {
                    "execution_id": run["execution_id"],
                    "definition_version": run["definition_version"],
                    "status": run["status"],
                    "reused": reused,
                    "identity": list(actor.job_key(row["job_id"])),
                    "priority": rank,
                },
            )
        return result
