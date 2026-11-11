# -*- coding: utf-8 -*-
"""Read selected legacy resources without starting or repairing a workspace."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from ...config.context import encode_scope_id, is_valid_identity_value
from ...runtime_workers import run_runtime_state_work
from . import migration_source as source
from .repo.json_repo import JsonJobRepository

MIGRATION_STOP_KEY = "task_migration_stop"


def _sources(root, key):
    tenant, origin, agent, _ = key
    if not all(is_valid_identity_value(value) for value in key):
        raise ValueError("invalid_cron_identity")
    scope = encode_scope_id(tenant, origin)
    home = root / scope
    files = source._workspace_sources(
        home,
        root,
    )  # pylint: disable=protected-access
    legacy = home / "jobs.json"
    if legacy.exists() and agent == "default":
        files.append(
            source._SourceFile(
                legacy,
                str(legacy.relative_to(root)),
                scope,
                agent,
            ),
        )  # pylint: disable=protected-access
    return [
        item
        for item in (
            source._resolved_profile(item, root)
            for item in files  # pylint: disable=protected-access
        )
        if item.agent_id == agent
    ]


def _chat(path, root, candidate):
    path = path.with_name("chats.json")
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("cron_migration_unsafe_chat_path")
    if not path.is_file():
        raise ValueError("cron_migration_chat_missing")
    with path.open("rb") as handle:
        raw = handle.read(source.MAX_FILE_BYTES + 1)
    if len(raw) > source.MAX_FILE_BYTES:
        raise ValueError("cron_migration_chat_file_too_large")
    state = candidate.state_patch
    if not state.get("task_chat_id") or not state.get("task_session_id"):
        raise ValueError("cron_migration_binding_conflict")
    chats = [
        chat
        for chat in json.loads(raw).get("chats", [])
        if chat.get("id") == state.get("task_chat_id")
    ]
    if len(chats) != 1:
        raise ValueError("cron_migration_chat_missing")
    chat = chats[0]
    _warn_chat_conflicts(chat, candidate)


def _warn_chat_conflicts(chat, candidate):
    definition = candidate.definition
    expected = {
        "session_kind": "task",
        "task_job_id": definition["job_id"],
        "task_tenant_id": definition["tenant_id"],
        "task_source_id": definition["source_id"],
        "task_agent_id": definition["agent_id"],
        "session_id": candidate.state_patch["task_session_id"],
        "user_id": definition["creator_user_id"],
    }
    actual = {
        "task_agent_id": definition["agent_id"],
        **(chat.get("meta") or {}),
        "session_id": chat.get("session_id"),
        "user_id": chat.get("user_id"),
    }
    for field, value in expected.items():
        if actual.get(field) != value:
            candidate.warnings.append(
                f"cron_migration_binding_conflict:{definition['job_id']}:chats.json:{field}",
            )


def read_member(root, key, loaded):
    """Only inspect the requested tenant and Agent; no global workspace walk."""
    root = Path(root).expanduser().resolve()
    found = []
    for item in _sources(root, key):
        data, fingerprint = source._read_file(
            item,
            root,
        )  # pylint: disable=protected-access
        for raw in data["jobs"]:
            if raw.get("id") != key[3]:
                continue
            candidate = source._candidate(
                raw,
                item,
                data,
                loaded,
                fingerprint,
                datetime.now(timezone.utc),
            )  # pylint: disable=protected-access
            if candidate.identity != tuple(key):
                raise ValueError("cron_migration_identity_conflict")
            _chat(item.path, root, candidate)
            found.append(candidate)
    if len(found) != 1:
        raise ValueError(
            (
                "cron_migration_job_missing"
                if not found
                else "cron_migration_duplicate_source"
            ),
        )
    candidate = found[0]
    if candidate.definition["task_type"] not in {"text", "agent", "workflow"}:
        raise ValueError("cron_migration_system_job_unsupported")
    state = copy.deepcopy(candidate.state_patch)
    legacy_runtime = copy.deepcopy(state)
    if state.get("last_status") == "running":
        state["last_status"] = "idle"
    for field in (
        "ordinary_external_job_id",
        "batch_external_job_id",
        "next_run_at",
        "next_run_times_json",
    ):
        state.pop(field, None)
    candidate.definition["spec_json"]["schedule"]["run_on_non_workday"] = False
    spec = candidate.definition["spec_json"]
    target = {
        "user_id": candidate.definition["creator_user_id"],
        "session_id": state["task_session_id"],
    }
    spec["dispatch"]["target"] = target
    if spec.get("request") is not None:
        spec["request"].update(target)
    return {
        "definition": candidate.definition,
        "state": state,
        "legacy_runtime": legacy_runtime,
        "fingerprint": candidate.fingerprint,
        "warnings": candidate.warnings,
        "memory_available": candidate.memory_available,
    }


def _legacy_file(root, key):
    files = []
    for item in _sources(root, key):
        data, _ = source._read_file(
            item,
            root,
        )  # pylint: disable=protected-access
        if any(job.get("id") == key[3] for job in data["jobs"]):
            files.append(item)
    if len(files) != 1:
        raise ValueError("cron_migration_legacy_file_changed")
    return files[0]


def _check_legacy_spec(job, item, key, snapshot):
    raw = job.model_dump(mode="json")
    tenant, origin, _ = source._identity(
        raw,
        item,
    )  # pylint: disable=protected-access
    fact, meta = snapshot["migration"], job.meta or {}
    if (
        (tenant, origin) != key[:2]
        or meta.get("task_chat_id") != fact["chat_id"]
        or meta.get("task_session_id") != fact["session_id"]
    ):
        raise ValueError("cron_migration_legacy_file_changed")
    legacy = snapshot.get("legacy_runtime", {})
    for field, expected in (
        ("external_job_id", "ordinary_external_job_id"),
        ("batch_dispatch_external_job_id", "batch_external_job_id"),
    ):
        if meta.get(field) and str(meta[field]) != legacy.get(expected):
            raise ValueError("cron_migration_legacy_timer_binding_conflict")


async def stop_legacy_member(root, key, operation_id, snapshot):
    """原子停用旧定义，不启动工作区，不同步旧表。"""
    root = Path(root).expanduser().resolve()
    item = await run_runtime_state_work(_legacy_file, root, key)

    def stop(jobs_file):
        matches = [
            index
            for index, job in enumerate(jobs_file.jobs)
            if job.id == key[3]
        ]
        if len(matches) != 1:
            raise ValueError("cron_migration_legacy_file_changed")
        index = matches[0]
        job = jobs_file.jobs[index]
        _check_legacy_spec(job, item, key, snapshot)
        meta = dict(job.meta or {})
        if meta.get(MIGRATION_STOP_KEY) == operation_id and not job.enabled:
            return False, None
        if meta.get(MIGRATION_STOP_KEY):
            raise ValueError("cron_migration_legacy_file_changed")
        meta.update(
            {MIGRATION_STOP_KEY: operation_id, "pause_reason": "manual"},
        )
        jobs_file.jobs[index] = job.model_copy(
            update={"enabled": False, "meta": meta},
        )
        return True, None

    await JsonJobRepository(item.path).mutate_jobs_file(stop)
