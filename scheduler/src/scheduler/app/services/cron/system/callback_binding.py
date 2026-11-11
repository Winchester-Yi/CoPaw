# -*- coding: utf-8 -*-
"""系统触发只信任已登记的新绑定，不借管理员角色或旧绑定放行。"""

import json

from .configuration import normalize_config
from .identity import TASK_SCOPES
from .platform import PROTOCOL


def binding_config(caller, body, job):
    """校验来源、Agent身份和操作引用，返回该引用的安全配置与水位。"""
    if body.get("callback_protocol") != PROTOCOL:
        raise ValueError("invalid_system_callback_protocol")
    if job is None or caller.source_id != job["source_id"]:
        raise LookupError("cron_system_job_not_found")
    if TASK_SCOPES.get(job["task_type"]) != job["scope_kind"]:
        raise LookupError("cron_system_job_not_found")
    if job["scope_kind"] == "source" and (
        job["tenant_id"],
        job["agent_id"],
    ) != ("", ""):
        raise LookupError("cron_system_job_not_found")
    if job["scope_kind"] == "agent" and (
        caller.tenant_id,
        caller.agent_id,
    ) != (job["tenant_id"], job["agent_id"]):
        raise LookupError("cron_system_job_not_found")
    kind = body.get("task_type")
    kind = "task_session_cleanup" if kind == "cleanup" else kind
    if kind != job["task_type"]:
        raise ValueError("system_callback_type_mismatch")
    supplied = body.get("external_job_id", body.get("jobId"))
    if not job["external_job_id"] or (
        supplied is not None and str(supplied) != job["external_job_id"]
    ):
        raise ValueError("system_callback_binding_invalid")
    return _reference(job, body.get("operation_id"))


def _reference(job, operation_id):
    references = [
        (job["effective_operation_id"], job["effective_config_json"]),
        (job["previous_operation_id"], job["previous_config_json"]),
    ]
    if job["sync_stage"] in {"updating", "enabling", "pausing", "completed"}:
        references.insert(0, (job["operation_id"], job["desired_config_json"]))
    for key, raw in references:
        if operation_id and key == operation_id and raw:
            snapshot = json.loads(raw)
            generation = snapshot.get("_generation")
            if type(generation) is not int or generation < 1:
                raise RuntimeError("system_binding_data_unavailable")
            return normalize_config(snapshot), generation
    raise ValueError("system_callback_operation_invalid")
