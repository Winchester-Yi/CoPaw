# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Optional

import click

from .http import client, print_json
from ..app.channels.schema import DEFAULT_CHANNEL
from ..config.context import get_current_source_id, get_current_tenant_id
from ..config.utils import load_config

MAX_NOTIFICATION_DELAY_MINUTES = 7 * 24 * 60


def _base_url(ctx: click.Context, base_url: Optional[str]) -> str:
    """Resolve base_url with priority:
    1) command --base-url
    2) global --host/--port
        (already resolved in main.py, may come from config.json)
    """
    if base_url:
        return base_url.rstrip("/")
    host = (ctx.obj or {}).get("host", "127.0.0.1")
    port = (ctx.obj or {}).get("port", 8088)
    return f"http://{host}:{port}"


@click.group("cron")
def cron_group() -> None:
    """Manage scheduled cron jobs via the HTTP API (/cron).

    Use list/get/state to inspect jobs; create/update/delete to add, replace,
    or remove; pause/resume to toggle execution; run to trigger a one-off run.
    """


@cron_group.command("list")
@click.option(
    "--base-url",
    default=None,
    help=(
        "Override the API base URL (e.g. http://127.0.0.1:8088). "
        "If omitted, uses global --host and --port from config."
    ),
)
@click.option(
    "--agent-id",
    default="default",
    help="Agent ID (defaults to 'default')",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Tenant ID forwarded as X-Tenant-Id header.",
)
@click.option(
    "--source-id",
    default=None,
    help="Source ID forwarded as X-Source-Id header.",
)
@click.pass_context
def list_jobs(
    ctx: click.Context,
    base_url: Optional[str],
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> None:
    """List all cron jobs. Output is JSON from GET /cron/jobs."""
    base_url = _base_url(ctx, base_url)
    with client(base_url) as c:
        headers = _build_headers(agent_id, tenant_id, source_id=source_id)
        r = c.get("/cron/jobs", headers=headers)
        r.raise_for_status()
        print_json(r.json())


@cron_group.command("get")
@click.argument("job_id", metavar="JOB_ID")
@click.option(
    "--base-url",
    default=None,
    help="Override the API base URL. Defaults to global --host/--port.",
)
@click.option(
    "--agent-id",
    default="default",
    help="Agent ID (defaults to 'default')",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Tenant ID forwarded as X-Tenant-Id header.",
)
@click.option(
    "--source-id",
    default=None,
    help="Source ID forwarded as X-Source-Id header.",
)
@click.pass_context
def get_job(
    ctx: click.Context,
    job_id: str,
    base_url: Optional[str],
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> None:
    """Fetch a cron job by ID. Returns JSON from GET /cron/jobs/<id>."""
    base_url = _base_url(ctx, base_url)
    with client(base_url) as c:
        headers = _build_headers(agent_id, tenant_id, source_id=source_id)
        r = c.get(f"/cron/jobs/{job_id}", headers=headers)
        if r.status_code == 404:
            raise click.ClickException("Job not found.")
        r.raise_for_status()
        print_json(r.json())


@cron_group.command("state")
@click.argument("job_id", metavar="JOB_ID")
@click.option(
    "--base-url",
    default=None,
    help="Override the API base URL. Defaults to global --host/--port.",
)
@click.option(
    "--agent-id",
    default="default",
    help="Agent ID (defaults to 'default')",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Tenant ID forwarded as X-Tenant-Id header.",
)
@click.option(
    "--source-id",
    default=None,
    help="Source ID forwarded as X-Source-Id header.",
)
@click.pass_context
def job_state(
    ctx: click.Context,
    job_id: str,
    base_url: Optional[str],
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> None:
    """Get the runtime state of a cron job (e.g. next run time, paused)."""
    base_url = _base_url(ctx, base_url)
    with client(base_url) as c:
        headers = _build_headers(agent_id, tenant_id, source_id=source_id)
        r = c.get(f"/cron/jobs/{job_id}/state", headers=headers)
        if r.status_code == 404:
            raise click.ClickException("Job not found.")
        r.raise_for_status()
        print_json(r.json())


def _build_headers(
    agent_id: str,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    source_id: Optional[str] = None,
) -> dict[str, str]:
    resolved_source_id = source_id or get_current_source_id()
    headers = {
        "X-Agent-Id": agent_id,
        "X-Tenant-Id": tenant_id or get_current_tenant_id() or "default",
    }
    if user_id:
        headers["X-User-Id"] = user_id
    if resolved_source_id:
        headers["X-Source-Id"] = resolved_source_id
    return headers


def _build_spec_from_cli(
    task_type: str,
    name: str,
    cron: str,
    channel: str,
    target_user: str,
    target_session: str,
    creator_user: Optional[str],
    text: Optional[str],
    timezone: str,
    enabled: bool,
    mode: str,
    model_provider: Optional[str],
    model: Optional[str],
    tenant_id: Optional[str] = None,
    job_id: str = "",
    notification_delay_minutes: int = 0,
) -> dict:
    """Build CronJobSpec JSON payload from CLI args."""
    schedule = {"type": "cron", "cron": cron, "timezone": timezone}
    dispatch = {
        "type": "channel",
        "channel": channel,
        "target": {"user_id": target_user, "session_id": target_session},
        "mode": mode,
        "meta": {},
    }
    meta = {"notification_delay_minutes": notification_delay_minutes}
    if creator_user:
        meta["creator_user_id"] = creator_user
    runtime = {
        "max_concurrency": 1,
        "timeout_seconds": 7200,
        "misfire_grace_seconds": 300,
    }
    model_slot = _resolve_model_slot(model_provider, model)
    if task_type == "text":
        if not (text and text.strip()):
            raise click.UsageError(
                "--text is required when task type is 'text'",
            )
        return {
            "id": job_id,
            "name": name,
            "enabled": enabled,
            "tenant_id": tenant_id,
            "schedule": schedule,
            "task_type": "text",
            "text": text.strip(),
            "dispatch": dispatch,
            "runtime": runtime,
            "meta": meta,
        }
    if task_type == "agent":
        if not (text and text.strip()):
            raise click.UsageError(
                "--text is required when task type is 'agent' "
                "(the question/prompt sent to the agent)",
            )
        payload = {
            "id": job_id,
            "name": name,
            "enabled": enabled,
            "tenant_id": tenant_id,
            "schedule": schedule,
            "task_type": "agent",
            "request": {
                "input": [
                    {
                        "role": "user",
                        "type": "message",
                        "content": [{"type": "text", "text": text.strip()}],
                    },
                ],
                "session_id": target_session,
                "user_id": "cron",
            },
            "dispatch": dispatch,
            "runtime": runtime,
            "meta": meta,
        }
        if model_slot is not None:
            payload["model_slot"] = model_slot
        return payload
    raise click.UsageError(f"Unsupported task type: {task_type}")


def _resolve_model_slot(
    model_provider: Optional[str],
    model: Optional[str],
) -> Optional[dict[str, str]]:
    if (model_provider is None) != (model is None):
        raise click.UsageError(
            "--model-provider and --model must be provided together",
        )
    if model_provider is None or model is None:
        return None
    normalized_provider = model_provider.strip()
    normalized_model = model.strip()
    if not normalized_provider or not normalized_model:
        raise click.UsageError(
            "--model-provider and --model must be non-empty",
        )
    return {
        "provider_id": normalized_provider,
        "model": normalized_model,
    }


def _build_payload_from_args(
    *,
    file_: Optional[Path],
    task_type: Optional[str],
    name: Optional[str],
    cron: Optional[str],
    channel: Optional[str],
    target_user: Optional[str],
    target_session: Optional[str],
    creator_user: Optional[str],
    text: Optional[str],
    timezone: str,
    enabled: bool,
    mode: str,
    model_provider: Optional[str],
    model: Optional[str],
    tenant_id: Optional[str],
    job_id: str = "",
    notification_delay_minutes: int = 0,
) -> dict:
    if file_ is not None:
        payload = json.loads(file_.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise click.UsageError(
                "Cron job spec file must contain a JSON object",
            )
        payload["id"] = job_id
        return payload

    for value, label in [
        (task_type, "--type"),
        (name, "--name"),
        (cron, "--cron"),
        (channel, "--channel"),
        (target_user, "--target-user"),
        (target_session, "--target-session"),
    ]:
        if not value or (isinstance(value, str) and not value.strip()):
            raise click.UsageError(
                f"When creating without -f/--file, {label} is required",
            )
    return _build_spec_from_cli(
        task_type=task_type or "agent",
        name=name or "",
        cron=cron or "",
        channel=channel or DEFAULT_CHANNEL,
        target_user=target_user or "",
        target_session=target_session or "",
        creator_user=creator_user,
        text=text,
        timezone=timezone,
        enabled=enabled,
        mode=mode,
        model_provider=model_provider,
        model=model,
        tenant_id=tenant_id,
        job_id=job_id,
        notification_delay_minutes=notification_delay_minutes,
    )


def _infer_effective_user_id(
    payload: object,
    creator_user: Optional[str],
) -> Optional[str]:
    effective_user_id = creator_user
    if not effective_user_id and isinstance(payload, dict):
        meta = payload.get("meta") or {}
        if isinstance(meta, dict):
            effective_user_id = meta.get("creator_user_id")
    if (
        not effective_user_id
        and isinstance(payload, dict)
        and payload.get("task_type") in {"agent", "workflow"}
    ):
        dispatch = payload.get("dispatch") or {}
        target = dispatch.get("target") if isinstance(dispatch, dict) else {}
        if isinstance(target, dict):
            effective_user_id = target.get("user_id")
    return effective_user_id


def _load_existing_job_spec(
    http_client,
    job_id: str,
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> dict:
    headers = _build_headers(agent_id, tenant_id, source_id=source_id)
    response = http_client.get(f"/cron/jobs/{job_id}", headers=headers)
    if response.status_code == 404:
        raise click.ClickException("Job not found.")
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and isinstance(data.get("spec"), dict):
        return copy.deepcopy(data["spec"])
    if isinstance(data, dict):
        return copy.deepcopy(data)
    raise click.ClickException("Invalid response when loading existing job.")


def _normalize_extracted_text(text: str) -> Optional[str]:
    normalized = text.strip()
    return normalized or None


def _extract_text_from_content_parts(parts: list[object]) -> Optional[str]:
    text_parts = [
        str(part["text"])
        for part in parts
        if isinstance(part, dict)
        and part.get("type") == "text"
        and part.get("text")
    ]
    if not text_parts:
        return None
    return _normalize_extracted_text("".join(text_parts))


def _extract_text_from_content(content: object) -> Optional[str]:
    if isinstance(content, str):
        return _normalize_extracted_text(content)
    if isinstance(content, list):
        return _extract_text_from_content_parts(content)
    return None


def _extract_existing_text(payload: dict) -> Optional[str]:
    if payload.get("task_type") == "text":
        return _extract_text_from_content(payload.get("text"))

    request = payload.get("request")
    if not isinstance(request, dict):
        return None
    request_input = request.get("input")
    if not isinstance(request_input, list):
        return None

    for item in request_input:
        if not isinstance(item, dict):
            continue
        extracted = _extract_text_from_content(item.get("content"))
        if extracted:
            return extracted
    return None


def _apply_optional_updates(target: dict, updates: dict[str, object]) -> None:
    for key, value in updates.items():
        if value is not None:
            target[key] = value


def _merge_meta(
    existing_meta: object,
    creator_user: Optional[str],
) -> dict:
    meta = existing_meta if isinstance(existing_meta, dict) else {}
    merged = dict(meta)
    if creator_user is not None:
        merged["creator_user_id"] = creator_user
    return merged


def _resolve_update_task_type(
    payload: dict,
    task_type: Optional[str],
) -> str:
    effective_task_type = task_type or payload.get("task_type")
    if effective_task_type in {"text", "agent"}:
        return effective_task_type
    raise click.UsageError(
        "Existing job is missing a supported task type; use -f/--file "
        "or specify --type explicitly",
    )


def _resolve_update_text(
    payload: dict,
    text: Optional[str],
    effective_task_type: str,
) -> str:
    effective_text = (
        text.strip()
        if isinstance(text, str) and text.strip()
        else _extract_existing_text(payload)
    )
    if effective_text:
        return effective_text
    raise click.UsageError(
        f"--text is required when task type is '{effective_task_type}'",
    )


def _apply_task_payload(
    payload: dict,
    effective_task_type: str,
    effective_text: str,
    target: dict,
) -> dict:
    payload["task_type"] = effective_task_type
    if effective_task_type == "text":
        payload["text"] = effective_text
        payload.pop("request", None)
        payload.pop("model_slot", None)
        return payload

    existing_request = payload.get("request")
    if not isinstance(existing_request, dict):
        existing_request = {}
    request_session_id = existing_request.get("session_id") or target.get(
        "session_id",
    )
    request_user_id = existing_request.get("user_id") or "cron"
    payload["request"] = {
        **existing_request,
        "input": [
            {
                "role": "user",
                "type": "message",
                "content": [{"type": "text", "text": effective_text}],
            },
        ],
        "session_id": request_session_id,
        "user_id": request_user_id,
    }
    payload.pop("text", None)
    return payload


def _merge_update_payload(
    existing_spec: dict,
    *,
    job_id: str,
    task_type: Optional[str],
    name: Optional[str],
    cron: Optional[str],
    channel: Optional[str],
    target_user: Optional[str],
    target_session: Optional[str],
    creator_user: Optional[str],
    text: Optional[str],
    timezone: Optional[str],
    enabled: Optional[bool],
    mode: Optional[str],
    model_provider: Optional[str],
    model: Optional[str],
    tenant_id: Optional[str],
) -> dict:
    payload = copy.deepcopy(existing_spec)
    payload["id"] = job_id

    _apply_optional_updates(
        payload,
        {
            "tenant_id": tenant_id,
            "name": name,
            "enabled": enabled,
        },
    )

    schedule = payload.setdefault("schedule", {})
    _apply_optional_updates(
        schedule,
        {
            "cron": cron,
            "timezone": timezone,
        },
    )

    dispatch = payload.setdefault("dispatch", {})
    target = dispatch.setdefault("target", {})
    _apply_optional_updates(
        dispatch,
        {
            "channel": channel,
            "mode": mode,
        },
    )
    _apply_optional_updates(
        target,
        {
            "user_id": target_user,
            "session_id": target_session,
        },
    )

    payload["meta"] = _merge_meta(payload.get("meta"), creator_user)

    effective_task_type = _resolve_update_task_type(payload, task_type)
    effective_text = _resolve_update_text(
        payload,
        text,
        effective_task_type,
    )
    payload = _apply_task_payload(
        payload,
        effective_task_type,
        effective_text,
        target,
    )
    model_slot = _resolve_model_slot(model_provider, model)
    if model_slot is not None and effective_task_type == "agent":
        payload["model_slot"] = model_slot
    return payload


@cron_group.command("create")
@click.option(
    "-f",
    "--file",
    "file_",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to a JSON file containing the full cron job spec. "
        "Mutually exclusive with inline options (--type, --name, etc.)."
    ),
)
@click.option(
    "--type",
    "task_type",
    type=click.Choice(["text", "agent"], case_sensitive=False),
    default=None,
    help=(
        "Task type: 'text' sends fixed content to the channel; "
        "'agent' sends a question to the agent and delivers the reply to the "
        "channel. Required when not using -f/--file."
    ),
)
@click.option(
    "--name",
    default=None,
    help="Display name for the job. Required when not using -f/--file.",
)
@click.option(
    "--cron",
    default=None,
    help=(
        "Cron expression (5 fields: minute hour day month weekday). "
        "Example: '0 9 * * *' for daily at 09:00. Required without -f/--file."
    ),
)
@click.option(
    "--channel",
    default=None,
    help=(
        "Delivery channel: e.g. imessage, dingtalk, discord, qq, console. "
        "Required when not using -f/--file."
    ),
)
@click.option(
    "--target-user",
    default=None,
    help=(
        "Target user_id for the channel (recipient identifier). "
        "Required when not using -f/--file."
    ),
)
@click.option(
    "--target-session",
    default=None,
    help=(
        "Target session_id for the channel. "
        "Required when not using -f/--file."
    ),
)
@click.option(
    "--creator-user",
    default=None,
    hidden=True,
    help="Creator user_id for task ownership metadata.",
)
@click.option(
    "--text",
    default=None,
    help=(
        "Content: for 'text' tasks this is the message sent to the channel; "
        "for 'agent' tasks this is the prompt/question sent to the agent. "
        "Required for both task types."
    ),
)
@click.option(
    "--timezone",
    default=None,
    help=(
        "Timezone for the cron schedule (e.g. UTC, America/New_York). "
        "Defaults to the user timezone from config."
    ),
)
@click.option(
    "--enabled/--no-enabled",
    default=True,
    help="Create the job as enabled (--enabled) or disabled (--no-enabled).",
)
@click.option(
    "--mode",
    type=click.Choice(["stream", "final"], case_sensitive=False),
    default="final",
    help=(
        "Delivery mode: 'stream' sends incremental updates; "
        "'final' sends only the final result."
    ),
)
@click.option(
    "--model-provider",
    default=None,
    help="Model provider ID to store in model_slot for agent jobs.",
)
@click.option(
    "--model",
    default=None,
    help="Model name to store in model_slot for agent jobs.",
)
@click.option(
    "--notification-delay-minutes",
    type=click.IntRange(0, MAX_NOTIFICATION_DELAY_MINUTES),
    default=0,
    show_default=True,
    help=(
        "Delay successful automatic-run completion notifications by this "
        "many minutes."
    ),
)
@click.option(
    "--base-url",
    default=None,
    help="Override the API base URL. Defaults to global --host/--port.",
)
@click.option(
    "--agent-id",
    default="default",
    help="Agent ID (defaults to 'default')",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Tenant ID forwarded as X-Tenant-Id header.",
)
@click.option(
    "--source-id",
    default=None,
    help="Source ID forwarded as X-Source-Id header.",
)
@click.pass_context
def create_job(
    ctx: click.Context,
    file_: Optional[Path],
    task_type: Optional[str],
    name: Optional[str],
    cron: Optional[str],
    channel: Optional[str],
    target_user: Optional[str],
    target_session: Optional[str],
    creator_user: Optional[str],
    text: Optional[str],
    timezone: Optional[str],
    enabled: bool,
    mode: str,
    model_provider: Optional[str],
    model: Optional[str],
    notification_delay_minutes: int,
    base_url: Optional[str],
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> None:
    """Create a cron job.

    Either pass -f/--file with a JSON spec, or use --type, --name, --cron,
    --channel, --target-user, --target-session and --text to define the job
    inline.
    """
    if timezone is None:
        timezone = load_config().user_timezone or "UTC"
    base_url = _base_url(ctx, base_url)
    payload = _build_payload_from_args(
        file_=file_,
        task_type=task_type,
        name=name,
        cron=cron,
        channel=channel,
        target_user=target_user,
        target_session=target_session,
        creator_user=creator_user,
        text=text,
        timezone=timezone,
        enabled=enabled,
        mode=mode,
        model_provider=model_provider,
        model=model,
        tenant_id=tenant_id,
        notification_delay_minutes=notification_delay_minutes,
    )
    with client(base_url) as c:
        effective_user_id = _infer_effective_user_id(payload, creator_user)
        headers = _build_headers(
            agent_id,
            tenant_id,
            effective_user_id,
            source_id,
        )
        r = c.post("/cron/jobs", json=payload, headers=headers)
        r.raise_for_status()
        print_json(r.json())


def _update_job_impl(
    ctx: click.Context,
    job_id: str,
    file_: Optional[Path],
    task_type: Optional[str],
    name: Optional[str],
    cron: Optional[str],
    channel: Optional[str],
    target_user: Optional[str],
    target_session: Optional[str],
    creator_user: Optional[str],
    text: Optional[str],
    timezone: Optional[str],
    enabled: Optional[bool],
    mode: Optional[str],
    model_provider: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> None:
    base_url = _base_url(ctx, base_url)
    with client(base_url) as c:
        if file_ is not None:
            payload = _build_payload_from_args(
                file_=file_,
                task_type=task_type,
                name=name,
                cron=cron,
                channel=channel,
                target_user=target_user,
                target_session=target_session,
                creator_user=creator_user,
                text=text,
                timezone=timezone or "",
                enabled=enabled if enabled is not None else True,
                mode=mode or "final",
                model_provider=model_provider,
                model=model,
                tenant_id=tenant_id,
                job_id=job_id,
            )
        else:
            existing_spec = _load_existing_job_spec(
                c,
                job_id,
                agent_id,
                tenant_id,
                source_id,
            )
            payload = _merge_update_payload(
                existing_spec,
                job_id=job_id,
                task_type=task_type,
                name=name,
                cron=cron,
                channel=channel,
                target_user=target_user,
                target_session=target_session,
                creator_user=creator_user,
                text=text,
                timezone=timezone,
                enabled=enabled,
                mode=mode,
                model_provider=model_provider,
                model=model,
                tenant_id=tenant_id,
            )
        effective_user_id = _infer_effective_user_id(payload, creator_user)
        headers = _build_headers(
            agent_id,
            tenant_id,
            effective_user_id,
            source_id,
        )
        r = c.put(f"/cron/jobs/{job_id}", json=payload, headers=headers)
        if r.status_code == 404:
            raise click.ClickException("Job not found.")
        r.raise_for_status()
        print_json(r.json())


@cron_group.command("update")
@click.argument("job_id", metavar="JOB_ID")
@click.option(
    "-f",
    "--file",
    "file_",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Path to a JSON file containing the full cron job spec. "
        "Mutually exclusive with inline options (--type, --name, etc.)."
    ),
)
@click.option(
    "--type",
    "task_type",
    type=click.Choice(["text", "agent"], case_sensitive=False),
    default=None,
    help="Task type for the replacement job when not using -f/--file.",
)
@click.option(
    "--name",
    default=None,
    help="Display name for the replacement job when not using -f/--file.",
)
@click.option(
    "--cron",
    default=None,
    help="Cron expression for the replacement job when not using -f/--file.",
)
@click.option(
    "--channel",
    default=None,
    help="Delivery channel for the replacement job when not using -f/--file.",
)
@click.option(
    "--target-user",
    default=None,
    help="Target user_id for the replacement job when not using -f/--file.",
)
@click.option(
    "--target-session",
    default=None,
    help="Target session_id for the replacement job when not using -f/--file.",
)
@click.option(
    "--creator-user",
    default=None,
    hidden=True,
    help="Creator user_id override for task ownership metadata.",
)
@click.option(
    "--text",
    default=None,
    help="Content for the replacement job when not using -f/--file.",
)
@click.option(
    "--timezone",
    default=None,
    help="Timezone for the replacement job. Defaults to user timezone.",
)
@click.option(
    "--enabled/--no-enabled",
    default=None,
    help="Override enabled state only when this flag is provided.",
)
@click.option(
    "--mode",
    type=click.Choice(["stream", "final"], case_sensitive=False),
    default=None,
    help="Override delivery mode only when this option is provided.",
)
@click.option(
    "--model-provider",
    default=None,
    help="Model provider ID to store in model_slot for agent jobs.",
)
@click.option(
    "--model",
    default=None,
    help="Model name to store in model_slot for agent jobs.",
)
@click.option(
    "--base-url",
    default=None,
    help="Override the API base URL. Defaults to global --host/--port.",
)
@click.option(
    "--agent-id",
    default="default",
    help="Agent ID (defaults to 'default')",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Tenant ID forwarded as X-Tenant-Id header.",
)
@click.option(
    "--source-id",
    default=None,
    help="Source ID forwarded as X-Source-Id header.",
)
@click.pass_context
def update_job(
    ctx: click.Context,
    job_id: str,
    file_: Optional[Path],
    task_type: Optional[str],
    name: Optional[str],
    cron: Optional[str],
    channel: Optional[str],
    target_user: Optional[str],
    target_session: Optional[str],
    creator_user: Optional[str],
    text: Optional[str],
    timezone: Optional[str],
    enabled: Optional[bool],
    mode: Optional[str],
    model_provider: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> None:
    """Replace an existing cron job in place while keeping the same job ID."""
    _update_job_impl(
        ctx,
        job_id,
        file_,
        task_type,
        name,
        cron,
        channel,
        target_user,
        target_session,
        creator_user,
        text,
        timezone,
        enabled,
        mode,
        model_provider,
        model,
        base_url,
        agent_id,
        tenant_id,
        source_id,
    )


@cron_group.command("delete")
@click.argument("job_id", metavar="JOB_ID")
@click.option(
    "--base-url",
    default=None,
    help="Override the API base URL. Defaults to global --host/--port.",
)
@click.option(
    "--agent-id",
    default="default",
    help="Agent ID (defaults to 'default')",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Tenant ID forwarded as X-Tenant-Id header.",
)
@click.option(
    "--source-id",
    default=None,
    help="Source ID forwarded as X-Source-Id header.",
)
@click.pass_context
def delete_job(
    ctx: click.Context,
    job_id: str,
    base_url: Optional[str],
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> None:
    """Permanently delete a cron job. The job is removed from the server."""
    base_url = _base_url(ctx, base_url)
    with client(base_url) as c:
        headers = _build_headers(agent_id, tenant_id, source_id=source_id)
        r = c.delete(f"/cron/jobs/{job_id}", headers=headers)
        if r.status_code == 404:
            raise click.ClickException("Job not found.")
        r.raise_for_status()
        print_json(r.json())


@cron_group.command("pause")
@click.argument("job_id", metavar="JOB_ID")
@click.option(
    "--base-url",
    default=None,
    help="Override the API base URL. Defaults to global --host/--port.",
)
@click.option(
    "--agent-id",
    default="default",
    help="Agent ID (defaults to 'default')",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Tenant ID forwarded as X-Tenant-Id header.",
)
@click.option(
    "--source-id",
    default=None,
    help="Source ID forwarded as X-Source-Id header.",
)
@click.pass_context
def pause_job(
    ctx: click.Context,
    job_id: str,
    base_url: Optional[str],
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> None:
    """Pause a cron job so it no longer runs on schedule.
    Use 'resume' to re-enable.
    """
    base_url = _base_url(ctx, base_url)
    with client(base_url) as c:
        headers = _build_headers(agent_id, tenant_id, source_id=source_id)
        r = c.post(f"/cron/jobs/{job_id}/pause", headers=headers)
        if r.status_code == 404:
            raise click.ClickException("Job not found.")
        r.raise_for_status()
        print_json(r.json())


@cron_group.command("resume")
@click.argument("job_id", metavar="JOB_ID")
@click.option(
    "--base-url",
    default=None,
    help="Override the API base URL. Defaults to global --host/--port.",
)
@click.option(
    "--agent-id",
    default="default",
    help="Agent ID (defaults to 'default')",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Tenant ID forwarded as X-Tenant-Id header.",
)
@click.option(
    "--source-id",
    default=None,
    help="Source ID forwarded as X-Source-Id header.",
)
@click.pass_context
def resume_job(
    ctx: click.Context,
    job_id: str,
    base_url: Optional[str],
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> None:
    """Resume a paused cron job so it runs again on its schedule."""
    base_url = _base_url(ctx, base_url)
    with client(base_url) as c:
        headers = _build_headers(agent_id, tenant_id, source_id=source_id)
        r = c.post(f"/cron/jobs/{job_id}/resume", headers=headers)
        if r.status_code == 404:
            raise click.ClickException("Job not found.")
        r.raise_for_status()
        print_json(r.json())


@cron_group.command("run")
@click.argument("job_id", metavar="JOB_ID")
@click.option(
    "--base-url",
    default=None,
    help="Override the API base URL. Defaults to global --host/--port.",
)
@click.option(
    "--agent-id",
    default="default",
    help="Agent ID (defaults to 'default')",
)
@click.option(
    "--tenant-id",
    default=None,
    help="Tenant ID forwarded as X-Tenant-Id header.",
)
@click.option(
    "--source-id",
    default=None,
    help="Source ID forwarded as X-Source-Id header.",
)
@click.pass_context
def run_job(
    ctx: click.Context,
    job_id: str,
    base_url: Optional[str],
    agent_id: str,
    tenant_id: Optional[str],
    source_id: Optional[str],
) -> None:
    """Trigger a one-off run of a cron job immediately (ignores schedule)."""
    base_url = _base_url(ctx, base_url)
    with client(base_url) as c:
        headers = _build_headers(agent_id, tenant_id, source_id=source_id)
        r = c.post(f"/cron/jobs/{job_id}/run", headers=headers)
        if r.status_code == 404:
            raise click.ClickException("Job not found.")
        r.raise_for_status()
        print_json(r.json())
