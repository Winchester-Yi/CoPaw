# -*- coding: utf-8 -*-
"""Internal, read-only selected legacy snapshot export for Scheduler."""

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from ...config import utils as config_utils
from ...runtime_workers import run_runtime_state_work
from .managed_api import ResourceRequest, _identity, _locked_operation
from .managed_diagnostics import format_storage_error
from .migration_source import capture_loaded_states
from .selected_migration_source import read_member, stop_legacy_member

router = APIRouter(
    prefix="/internal/cron/managed",
    tags=["selected-cron-migration"],
)
logger = logging.getLogger(__name__)


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=128)


@router.post("/migration-stop-legacy")
async def migration_stop_legacy(request: Request, body: ResourceRequest):
    actor, tenant, source, agent = _identity(request)
    async with _locked_operation(request, body) as operation:
        if (
            operation["action"] != "migrate"
            or operation["status"] != "running"
            or operation["actor_id"] != actor
            or not operation["_migration"]
        ):
            raise HTTPException(404, "cron_operation_not_found")
        try:
            await stop_legacy_member(
                config_utils.WORKING_DIR,
                (tenant, source, agent, body.job_id),
                body.operation_id,
                json.loads(operation["payload_json"]),
            )
        except ValueError as exc:
            code = str(exc)
            if not code.startswith("cron_migration_"):
                code = "cron_migration_legacy_file_changed"
            raise HTTPException(409, code) from exc
    return {"stopped": True}


def _row_identity(row, key):
    if (
        not row
        or row.get("deleted_at")
        or (row["tenant_id"], row["source_id"], row["id"])
        != (key[0], key[1], key[3])
    ):
        raise ValueError("cron_migration_legacy_record_missing")


async def _member(db, key, loaded, row):
    _row_identity(row, key)
    histories = await db.fetch_all(
        "SELECT id FROM swe_cron_executions WHERE job_id=%s AND tenant_id=%s "
        "AND job_id COLLATE utf8mb4_bin=%s AND tenant_id COLLATE utf8mb4_bin=%s "
        "AND is_read=0 AND status IN ('success','error','timeout','cancelled','skipped') "
        "AND (async_status IS NULL OR async_status IN ('success','failed','error')) ORDER BY id",
        (key[3], key[0], key[3], key[0]),
    )
    result = await run_runtime_state_work(
        read_member,
        config_utils.WORKING_DIR,
        key,
        loaded,
    )
    definition, state = result["definition"], result["state"]
    if row.get("creator_user_id") != definition["creator_user_id"]:
        raise ValueError("cron_migration_owner_conflict")
    for field in ("task_chat_id", "task_session_id"):
        if row.get(field) != state.get(field):
            result["warnings"].append(
                f"cron_migration_binding_conflict:{key[3]}:swe_cron_jobs:{field}",
            )
    watermark = await db.fetch_one(
        "SELECT MAX(id) AS max_id FROM swe_cron_executions WHERE job_id=%s AND tenant_id=%s",
        (key[3], key[0]),
    )
    result["history_max_id"] = int((watermark or {}).get("max_id") or 0)
    result["history_read_ids"] = [item["id"] for item in histories]
    await _restore_completed_result(db, key, result)
    return result


async def _restore_completed_result(db, key, snapshot):
    """未加载的子工作区可从已有历史恢复终态，不接管在途执行。"""
    row = await db.fetch_one(
        "SELECT status,async_status,error_message,actual_time,output_preview,is_manual FROM swe_cron_executions "
        "WHERE job_id=%s AND tenant_id=%s AND job_id COLLATE utf8mb4_bin=%s AND tenant_id COLLATE utf8mb4_bin=%s "
        "AND id<=%s AND status IN ('success','error','timeout','cancelled','skipped') "
        "AND (async_status IS NULL OR async_status IN ('success','failed','error')) ORDER BY id DESC LIMIT 1",
        (key[3], key[0], key[3], key[0], snapshot["history_max_id"]),
    )
    if not row:
        return
    state = snapshot["state"]
    failed = row["async_status"] in {"failed", "error"} or row["status"] in {
        "error",
        "timeout",
    }
    state.setdefault("last_status", "error" if failed else row["status"])
    state.setdefault("last_error", row["error_message"] if failed else None)
    actual = row["actual_time"]
    if actual:
        value = (
            datetime.fromisoformat(actual)
            if isinstance(actual, str)
            else actual
        )
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        actual = value.astimezone(timezone.utc).replace(tzinfo=None)
        state.setdefault("last_run_at", actual)
    if not row["is_manual"]:
        state.setdefault("has_scheduled_result", bool(row["output_preview"]))
        state.setdefault(
            "latest_scheduled_preview",
            row["output_preview"] or "",
        )
        if actual:
            state.setdefault("last_scheduled_run_at", actual)


async def snapshot_group(db, key, actor, manager, loaded):
    row = await db.fetch_one(
        "SELECT * FROM swe_cron_jobs WHERE id=%s",
        (key[3],),
    )
    _row_identity(row, key)
    if not manager and row.get("creator_user_id") != actor:
        raise HTTPException(404, "cron_migration_job_not_found")
    parent = await _member(db, key, loaded, row)
    if parent["definition"].get("parent_job_id"):
        raise ValueError("cron_migration_select_parent")
    mode = await _mode(db, key, parent)
    parent["state"]["dispatch_state"] = mode
    children = await db.fetch_all(
        "SELECT * FROM swe_cron_jobs WHERE source_id=%s "
        "AND source_id COLLATE utf8mb4_bin=%s AND deleted_at IS NULL "
        "AND JSON_UNQUOTE(JSON_EXTRACT(CASE WHEN JSON_VALID(meta) THEN meta ELSE '{}' END, "
        "'$.broadcast_source_job_id'))=%s ORDER BY id",
        (key[1], key[1], key[3]),
    )
    members = [parent]
    for child in children:
        meta = json.loads(child.get("meta") or "{}")
        child_key = (
            child["tenant_id"],
            child["source_id"],
            key[2],
            child["id"],
        )
        member = await _member(db, child_key, loaded, child)
        _set_parent(member, key, meta)
        member["state"]["dispatch_state"] = mode
        members.append(member)
    return {
        "root": list(key),
        "members": members,
        "memory_scope": "loaded_on_current_instance",
    }


def _set_parent(member, key, old_meta):
    definition = member["definition"]
    spec_meta = definition["spec_json"]["meta"]
    for name, value in zip(
        ("tenant_id", "source_id", "agent_id", "job_id"),
        key,
    ):
        field = f"broadcast_source_{name}"
        default = value if name in {"source_id", "agent_id"} else None
        for origin, meta in (
            ("swe_cron_jobs", old_meta),
            ("jobs.json", spec_meta),
        ):
            if meta.get(field, default) != value:
                member["warnings"].append(
                    f"cron_migration_parent_conflict:{definition['job_id']}:{origin}:{field}",
                )
        spec_meta[field] = value
        definition[f"parent_{name}"] = value


async def _mode(db, key, parent):
    meta = parent["definition"]["spec_json"]["meta"]
    if not meta.get("broadcast_dispatch_intents_enabled"):
        return "ordinary"
    row = await db.fetch_one(
        "SELECT paused FROM swe_cron_dispatch_controls WHERE source_id=%s AND tenant_id=%s AND parent_job_id=%s",
        (key[1], key[0], key[3]),
    )
    # Absence is the legacy default (active); storage errors must not become it.
    return "batch_paused" if row and row["paused"] else "batch_enabled"


@router.post("/migration-snapshot")
async def migration_snapshot(request: Request, body: SnapshotRequest):
    actor, tenant, source, agent = _identity(request)
    db = getattr(request.app.state, "db_connection", None)
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(503, "cron_database_unavailable")
    loaded = capture_loaded_states(
        getattr(request.app.state, "multi_agent_manager", None),
    )
    manager = request.headers.get("X-User-Role", "").lower() in {
        "manager",
        "admin",
    }
    try:
        return jsonable_encoder(
            await snapshot_group(
                db,
                (tenant, source, agent, body.job_id),
                actor,
                manager,
                loaded,
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "旧任务迁移快照读取失败 job_id=%s\n%s",
            body.job_id,
            format_storage_error(exc),
        )
        code = (
            str(exc)
            if isinstance(exc, ValueError)
            and str(exc).startswith("cron_migration_")
            else "cron_migration_snapshot_unavailable"
        )
        raise HTTPException(
            409 if isinstance(exc, ValueError) else 503,
            code,
        ) from exc
