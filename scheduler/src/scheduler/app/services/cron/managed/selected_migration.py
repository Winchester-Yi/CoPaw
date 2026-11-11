# -*- coding: utf-8 -*-
"""勾选迁移：冻结快照、确认范围、整组准备，最后发布接管事实。"""

import json
import copy
import logging
import re
import httpx

from .date_policy import platform_window_open
from .diagnostics import format_management_error
from .identity import CallerIdentity
from .operations import LeaseLost
from .selected_migration_store import (
    SelectedMigrationStore,
    key_of,
    lease_active,
)

logger = logging.getLogger(__name__)


def summary(row):
    progress = json.loads(row["progress_json"])
    return {
        "operation_id": row["operation_id"],
        "status": row["status"],
        "can_retry": row["status"] in {"previewing", "running"}
        and not lease_active(row),
        **progress,
    }


class SelectedMigrationService:
    def __init__(self, db, source, platform):
        self.store = SelectedMigrationStore(db)
        self.source = source
        self.platform = platform

    async def submit(self, caller, body):
        return summary(await self.store.submit(caller, body))

    async def get(self, caller, operation_id):
        return summary(await self.store.get(caller, operation_id, public=True))

    async def confirm(self, caller, operation_id):
        return summary(await self.store.confirm(caller, operation_id))

    async def run(self, caller, operation_id):
        batch = await self.store.claim(caller, operation_id)
        if not batch:
            return
        payload = json.loads(batch["payload_json"])
        try:
            if batch["status"] == "previewing":
                await self._preview(caller, batch, payload)
            else:
                await self._migrate(batch, payload)
        except LeaseLost:
            logger.warning("迁移租约已转交 operation_id=%s", operation_id)
        except Exception as exc:
            logger.error(
                "迁移后台中断 operation_id=%s\n%s",
                operation_id,
                format_management_error(exc),
            )
            await self.store.save(batch, payload, "failed")

    async def _preview(self, caller, batch, payload):
        known = {group["job_id"]: group for group in payload["groups"]}
        for job in payload["job_ids"]:
            group = known.get(job)
            if group and group.get("snapshot"):
                continue
            group = {
                "job_id": job,
                "status": "preview_failed",
                "error_code": None,
            }
            try:
                snapshot = await self.source.migration_snapshot(caller, job)
                _validate_snapshot(caller, job, snapshot)
                group.update(
                    snapshot=snapshot,
                    status="preview_ready",
                    member_count=len(snapshot["members"]),
                    warnings=sorted(
                        {
                            w
                            for m in snapshot["members"]
                            for w in m.get("warnings", [])
                        },
                    ),
                )
            except Exception as exc:
                group["error_code"] = _error(exc)
                logger.error(
                    "迁移快照失败 job_id=%s\n%s",
                    job,
                    format_management_error(exc),
                )
            known[job] = group
            payload["groups"] = list(known.values())
            await self.store.save(batch, payload)
        await self.store.save(
            batch,
            payload,
            (
                "awaiting_confirmation"
                if any(g.get("snapshot") for g in payload["groups"])
                else "failed"
            ),
        )

    async def _migrate(self, batch, payload):
        for group in payload["groups"]:
            if group["status"] == "completed":
                continue
            if not group.get("snapshot"):
                group["status"] = "failed"
                continue
            group.update(status="running", error_code=None)
            await self.store.save(batch, payload)
            try:
                operations = [
                    op
                    for op in await self.store.prepare_group(
                        batch,
                        group["snapshot"],
                    )
                    if op
                ]
                await self._group(batch, operations, group["snapshot"])
                group.update(
                    status="completed",
                    migrated_count=len(operations),
                    skipped_count=group["member_count"] - len(operations),
                )
            except LeaseLost:
                raise
            except Exception as exc:
                group.update(status="failed", error_code=_error(exc))
                logger.error(
                    "任务组迁移失败 job_id=%s\n%s",
                    group["job_id"],
                    format_management_error(exc),
                )
            await self.store.save(batch, payload)
        await self.store.save(
            batch,
            payload,
            (
                "completed"
                if all(g["status"] == "completed" for g in payload["groups"])
                else "failed"
            ),
        )

    async def _group(self, batch, operations, snapshot):
        root = tuple(snapshot["root"])
        for operation in operations:
            await self._prepare_timer(batch, operation, root)
        targets = _legacy_timer_targets(operations, snapshot)
        if operations and _root_dispatch_state(snapshot, root) != "ordinary":
            await self._check_old_batch_drained(root)
        for operation in operations:
            await self._stop_legacy_definition(batch, operation)
        await self._pause_legacy_timers(batch, targets)
        try:
            for operation in operations:
                await self._activate(batch, operation)
            await self.store.publish(batch, operations)
        except Exception:
            # 即使远程暂停失败，本地就绪门禁仍关闭，不投入业务执行。
            for operation in operations:
                timer = json.loads(operation["progress_json"]).get(
                    "external_id",
                )
                if timer:
                    try:
                        await self.platform.set_enabled(timer, False)
                    except Exception as exc:
                        logger.error(
                            "迁移失败后暂停新入口失败 operation_id=%s\n%s",
                            operation["operation_id"],
                            format_management_error(exc),
                        )
            raise

    async def _check_old_batch_drained(self, root):
        waiting = await self.store.db.fetch_one(
            "SELECT 1 AS pending FROM swe_cron_dispatch_batches b "
            "JOIN swe_cron_dispatch_intents i ON i.batch_id=b.batch_id "
            "WHERE b.tenant_id=%s AND b.source_id=%s AND b.agent_id=%s "
            "AND b.parent_job_id=%s "
            "AND i.status IN ('pending','claimed','acknowledged','dispatched') LIMIT 1",
            root,
        )
        if waiting:
            raise ValueError("cron_migration_legacy_batch_not_drained")

    async def _stop_legacy_definition(self, batch, operation):
        progress = json.loads(operation["progress_json"])
        await self.store.step(batch, operation, progress)
        caller = CallerIdentity(*key_of(operation)[:3], operation["actor_id"])
        result = await self.source.migration_stop_legacy(operation, caller)
        if result.get("stopped") is not True:
            raise ValueError("cron_migration_legacy_file_stop_unconfirmed")

    async def _pause_legacy_timers(self, batch, targets):
        for timer, operation in targets.items():
            progress = json.loads(operation["progress_json"])
            await self.store.step(batch, operation, progress)
            try:
                await self.platform.set_enabled(timer, False)
            except Exception as exc:
                logger.error(
                    "迁移暂停旧定时器失败 job_id=%s external_id=%s\n%s",
                    operation["job_id"],
                    timer,
                    format_management_error(exc),
                )
                raise ValueError(
                    "cron_migration_legacy_timer_pause_failed",
                ) from exc

    async def _prepare_timer(self, batch, operation, root):
        payload = json.loads(operation["payload_json"])
        progress = json.loads(operation["progress_json"])
        mode, spec = payload["dispatch_state"], payload["spec"]
        if mode != "ordinary" and key_of(operation) != root:
            return
        spec, advance = _timer_spec(spec, mode)
        if not progress.get("external_id"):
            if progress.get("registration_started"):
                logger.warning(
                    "迁移注册未取得ID，重试可能遗留重复新入口 operation_id=%s",
                    operation["operation_id"],
                )
            progress["registration_started"] = True
            await self.store.step(batch, operation, progress)
            timer = await self.platform.register(operation, spec, mode)
            progress["external_id"] = timer
            field = (
                "ordinary_external_job_id"
                if mode == "ordinary"
                else "batch_external_job_id"
            )
            progress["batch_advance_minutes"] = advance
            await self.store.step(batch, operation, progress, field)
        await self.platform.set_enabled(progress["external_id"], False)
        progress["prepared"] = {"spec": spec}
        await self.store.step(batch, operation, progress)

    async def _activate(self, batch, operation):
        progress = json.loads(operation["progress_json"])
        if not progress.get("external_id"):
            return
        payload = json.loads(operation["payload_json"])
        enabled = payload["dispatch_state"] == "batch_enabled" or (
            payload["dispatch_state"] == "ordinary"
            and payload["spec"]["enabled"]
        )
        await self.store.step(batch, operation, progress)
        await self.platform.set_enabled(
            progress["external_id"],
            enabled and platform_window_open(payload["spec"]),
        )


def _root_dispatch_state(snapshot, root):
    return next(
        member["state"]["dispatch_state"]
        for member in snapshot["members"]
        if key_of(member["definition"]) == root
    )


def _legacy_timer_targets(operations, snapshot):
    new_ids = {
        str(json.loads(op["progress_json"]).get("external_id") or "").lstrip(
            "0",
        )
        for op in operations
    }
    warnings = {
        key_of(member["definition"]): member.get("warnings", [])
        for member in snapshot["members"]
    }
    targets = {}
    for operation in operations:
        legacy = json.loads(operation["payload_json"]).get(
            "legacy_runtime",
            {},
        )
        for timer in _legacy_timer_ids(legacy, warnings[key_of(operation)]):
            if timer in new_ids:
                raise ValueError(
                    "cron_migration_legacy_timer_binding_conflict",
                )
            targets.setdefault(timer, operation)
    return targets


def _legacy_timer_ids(legacy, warnings):
    for field in ("ordinary_external_job_id", "batch_external_job_id"):
        if any(
            f"{kind}:{field}" in warnings
            for kind in ("binding_conflict", "invalid_state")
        ):
            raise ValueError("cron_migration_legacy_timer_binding_conflict")
        value = legacy.get(field)
        if not value:
            continue
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{1,128}",
            value,
        ):
            raise ValueError("cron_migration_legacy_timer_binding_conflict")
        timer = value.lstrip("0")
        if not timer:
            raise ValueError("cron_migration_legacy_timer_binding_conflict")
        yield timer


def _validate_snapshot(caller, job, snapshot):
    root = caller.job_key(job)
    members = snapshot.get("members", [])
    if tuple(snapshot.get("root", ())) != root or not members:
        raise ValueError("cron_migration_snapshot_invalid")
    seen = set()
    for member in members:
        definition = member["definition"]
        key = key_of(definition)
        if key in seen or key[1:3] != root[1:3]:
            raise ValueError("cron_migration_identity_conflict")
        seen.add(key)
        if (
            key != root
            and tuple(
                definition.get("parent_" + k)
                for k in ("tenant_id", "source_id", "agent_id", "job_id")
            )
            != root
        ):
            raise ValueError("cron_migration_parent_conflict")
        if member["state"].get("dispatch_state") not in {
            "ordinary",
            "batch_enabled",
            "batch_paused",
        }:
            raise ValueError("cron_migration_mode_unknown")
    if root not in seen:
        raise ValueError("cron_migration_parent_missing")


def _error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = exc.response.json().get("detail")
            if isinstance(detail, str) and re.fullmatch(
                r"cron_migration_[a-z_]{1,100}",
                detail,
            ):
                return detail
        except (ValueError, AttributeError):
            pass
    text = str(exc)
    return (
        text
        if isinstance(exc, ValueError) and text.startswith("cron_migration_")
        else "cron_migration_step_failed"
    )


def _timer_spec(original, mode):
    """旧批入口的提前量只作用于平台入口，定义仍保留原业务排程。"""
    from .scatter import shift_cron_expression

    spec = copy.deepcopy(original)
    if mode == "ordinary":
        return spec, 0
    meta = spec.setdefault("meta", {})
    advance = meta.get("batch_dispatch_offset_minutes", 0) or 0
    if type(advance) is not int or not 0 <= advance <= 1440:
        raise ValueError("cron_migration_batch_offset_invalid")
    shifted = shift_cron_expression(
        spec["schedule"]["cron"],
        spec["schedule"]["timezone"],
        offset_minutes=advance,
    )
    if shifted.error:
        raise ValueError("cron_migration_batch_cron_not_shiftable")
    spec["schedule"]["cron"] = shifted.cron
    meta["_scheduler_calendar_advance_minutes"] = advance
    return spec, advance
