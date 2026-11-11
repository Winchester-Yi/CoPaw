# -*- coding: utf-8 -*-
"""Model-failure policy shared by managed batch retries and worker feedback."""

import json

from .dispatch_events import failure_type

MODEL_ERROR_TYPE = "Prompt"
MAIN_ERROR_EXCLUSIONS = (
    "cron auth user_info is expired",
    "workflow sapid does not match target user",
    "技能任务结果不可用",
)


def _result_metadata(run):
    try:
        value = json.loads(run.get("result_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _history_matches(run, history):
    if not isinstance(history, dict):
        return False
    try:
        metadata = json.loads(history.get("meta") or "{}")
    except (TypeError, ValueError):
        return False
    return (
        isinstance(metadata, dict)
        and history.get("job_id") == run.get("job_id")
        and history.get("tenant_id") == run.get("tenant_id")
        and metadata.get("management_owner") == "scheduler"
        and metadata.get("managed_execution_id") == run.get("execution_id")
        and metadata.get("source_id") == run.get("source_id")
        and metadata.get("agent_id") == run.get("agent_id")
    )


def is_model_failure(run, history=None):
    """A correlated Prompt subtask failure or an existing retryable main error."""
    child_failed_with_model = (
        _history_matches(run, history)
        and history.get("status") == "success"
        and history.get("async_status") == "error"
        and MODEL_ERROR_TYPE in (history.get("error_type") or "")
    )
    if child_failed_with_model:
        return True

    if run.get("status") != "error":
        return False
    metadata = _result_metadata(run)
    if (
        failure_type(run) == "auth_expired"
        or metadata.get("failure_type") == "auth_expired"
    ):
        return False
    details = [
        (
            history.get("error_message", "")
            if _history_matches(run, history)
            else ""
        ),
        metadata.get("error_detail", ""),
        run.get("error_code", ""),
        run.get("settled_error_code", ""),
    ]
    combined = " ".join(str(value or "") for value in details).casefold()
    return not any(error in combined for error in MAIN_ERROR_EXCLUSIONS)


def _sql_stop_condition(run_alias, history_alias):
    result_json = (
        f"(CASE WHEN JSON_VALID({run_alias}.result_json) "
        f"THEN {run_alias}.result_json ELSE '{{}}' END)"
    )
    error_texts = (
        f"{history_alias}.error_message",
        f"JSON_UNQUOTE(JSON_EXTRACT({result_json},'$.error_detail'))",
        f"{run_alias}.error_code",
        f"{run_alias}.settled_error_code",
    )
    return " OR ".join(
        f"INSTR(LOWER(COALESCE({value},'')), '{error}') > 0"
        for value in error_texts
        for error in MAIN_ERROR_EXCLUSIONS
    )


def worker_model_failure_sql(run_alias="r", history_alias="h"):
    """SQL equivalent of is_model_failure for a settled execution row."""
    result_json = (
        f"(CASE WHEN JSON_VALID({run_alias}.result_json) "
        f"THEN {run_alias}.result_json ELSE '{{}}' END)"
    )
    error_type = f"COALESCE(JSON_UNQUOTE(JSON_EXTRACT({result_json},'$.failure_type')),'')"
    main_error = (
        f"({run_alias}.status='error' "
        f"AND {error_type} COLLATE utf8mb4_bin <> 'auth_expired' "
        f"AND NOT ({_sql_stop_condition(run_alias, history_alias)}))"
    )
    child_error = (
        f"({history_alias}.status='success' "
        f"AND {history_alias}.async_status='error' "
        f"AND INSTR(COALESCE({history_alias}.error_type,'') COLLATE utf8mb4_bin,'Prompt')>0)"
    )
    return f"COALESCE(({main_error} OR {child_error}),0)"


def worker_history_join_sql(run_alias="r", history_alias="h"):
    """Join only the SWE history row belonging to the same managed execution."""
    metadata = (
        f"(CASE WHEN JSON_VALID({history_alias}.meta) "
        f"THEN {history_alias}.meta ELSE '{{}}' END)"
    )
    return (
        f"{history_alias}.id={run_alias}.history_execution_id "
        f"AND {history_alias}.job_id={run_alias}.job_id "
        f"AND {history_alias}.tenant_id={run_alias}.tenant_id "
        f"AND JSON_UNQUOTE(JSON_EXTRACT({metadata},'$.management_owner'))='scheduler' "
        f"AND JSON_UNQUOTE(JSON_EXTRACT({metadata},'$.managed_execution_id'))={run_alias}.execution_id "
        f"AND JSON_UNQUOTE(JSON_EXTRACT({metadata},'$.source_id'))={run_alias}.source_id "
        f"AND JSON_UNQUOTE(JSON_EXTRACT({metadata},'$.agent_id'))={run_alias}.agent_id"
    )
