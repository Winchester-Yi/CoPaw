# -*- coding: utf-8 -*-
"""Scheduler-only CLI transport; no old task or filesystem fallback."""

import copy
import os
from urllib.parse import quote, urlsplit
from uuid import uuid4

import click
import httpx
from click.core import ParameterSource

from ..config.context import (
    get_current_passthrough_headers,
    get_current_source_id,
    get_current_tenant_id,
    get_current_user_id,
    is_valid_identity_value,
    resolve_runtime_identity,
)
from .http import print_json

JOBS = "/scheduler/cron/jobs"


def identity(value, label):
    if (
        not isinstance(value, str)
        or len(value) > 128
        or not is_valid_identity_value(value)
    ):
        raise click.UsageError(f"A valid {label} is required for Scheduler.")
    return value


def scheduler_base_url(ctx, override):
    root = ctx.find_root()
    explicit = {
        name
        for name in ("host", "port")
        if root.get_parameter_source(name) == ParameterSource.COMMANDLINE
    }
    if override:
        result = override
    elif explicit:
        options = root.obj or {}
        host = options.get("host", "127.0.0.1")
        port = options.get("port") if "port" in explicit else 9100
        result = f"http://{host}:{port}"
    else:
        result = (
            os.environ.get("SWE_SCHEDULER_API_URL")
            or "http://127.0.0.1:9100/api"
        )
    parsed = urlsplit(result)
    has_credentials_or_suffix = any(
        (parsed.username, parsed.password, parsed.query, parsed.fragment),
    )
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or has_credentials_or_suffix
    ):
        raise click.UsageError("Invalid Scheduler base URL.")
    return result.rstrip("/")


def scheduler_headers(agent, tenant=None, user=None, source=None):
    ctx = click.get_current_context(silent=True)
    options = (ctx.obj or {}) if ctx is not None else {}
    incoming = {
        key.lower(): value
        for key, value in (get_current_passthrough_headers() or {}).items()
    }
    source = (
        source or get_current_source_id() or os.environ.get("SWE_SOURCE_ID")
    )
    tenant = (
        tenant or get_current_tenant_id() or os.environ.get("SWE_TENANT_ID")
    )
    tenant, source, _ = resolve_runtime_identity(tenant, source)
    actor = (
        options.get("cron_user_id")
        or get_current_user_id()
        or incoming.get("x-user-id")
        or user
        or tenant
    )
    actor = identity(actor, "user-id")
    result = {
        key: value
        for key, value in incoming.items()
        if key
        in {
            "authorization",
            "x-header-cookie",
            "b3",
            "x-b3-traceid",
            "x-b3-spanid",
            "x-bbk-id",
            "x-user-name",
        }
    }
    result.update(
        {
            "X-User-Id": actor,
            "X-Tenant-Id": identity(tenant or actor, "tenant-id"),
            "X-Source-Id": identity(source, "source-id"),
            "X-Agent-Id": identity(agent, "agent-id"),
        },
    )
    return result


class SchedulerCronClient:
    def __init__(self, http, headers, request_id=None):
        self.http = http
        self.headers = headers
        self.request_id = identity(request_id or str(uuid4()), "request-id")

    @staticmethod
    def path(job_id):
        return JOBS + "/" + quote(identity(job_id, "job-id"), safe="")

    def _request(self, method, path, *, body=None, params=None):
        correlation = (
            {"request_id": body["request_id"]} if body is not None else {}
        )
        suffix = (
            f" request_id={correlation['request_id']}" if correlation else ""
        )
        kwargs = {"headers": self.headers}
        if body is not None:
            kwargs["json"] = body
        if params is not None:
            kwargs["params"] = params
        try:
            if method == "DELETE":
                response = self.http.request(method, path, **kwargs)
            else:
                response = getattr(self.http, method.lower())(path, **kwargs)
        except httpx.HTTPError as error:
            raise click.ClickException(
                f"Scheduler transport failed ({type(error).__name__}).{suffix} No automatic retry; inspect the original operation.",
            ) from error
        try:
            result = response.json()
        except ValueError as error:
            raise click.ClickException(
                f"Invalid Scheduler JSON response (HTTP {response.status_code}).{suffix}",
            ) from error
        if not isinstance(result, dict):
            raise click.ClickException("Invalid Scheduler response object.")
        if response.status_code >= 300:
            if isinstance(result.get("operation"), dict):
                print_json({**result, **correlation})
            detail = result.get("detail")
            code = detail if isinstance(detail, str) else "request_rejected"
            raise click.ClickException(
                f"Scheduler HTTP {response.status_code}: {code}.{suffix}",
            )
        return result

    def _check_spec(self, spec, agent, owner, job_id=None):
        expected = (
            self.headers["X-Tenant-Id"],
            self.headers["X-Source-Id"],
            self.headers["X-Agent-Id"],
        )
        if (
            not isinstance(spec, dict)
            or owner != "scheduler"
            or (spec.get("tenant_id"), spec.get("source_id"), agent)
            != expected
        ):
            raise click.ClickException("Scheduler task identity mismatch.")
        identifier = identity(spec.get("id"), "job-id")
        if job_id is not None and identifier != job_id:
            raise click.ClickException("Scheduler job ID mismatch.")

    def list_jobs(self):
        jobs, cursors, identifiers = [], set(), set()
        params = {"limit": 200, "include_deleted": "false"}
        while True:
            page = self._request("GET", JOBS, params=params)
            if not isinstance(page.get("items"), list):
                raise click.ClickException("Invalid Scheduler job page.")
            for item in page["items"]:
                if not isinstance(item, dict):
                    raise click.ClickException("Invalid Scheduler task.")
                self._check_spec(
                    item,
                    item.get("agent_id"),
                    item.get("management_owner"),
                )
                if item["id"] in identifiers:
                    raise click.ClickException("Duplicate Scheduler job ID.")
                identifiers.add(item["id"])
                jobs.append(item)
            cursor = page.get("next_cursor")
            if cursor is None:
                return jobs
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise click.ClickException(
                    "Invalid or repeated Scheduler cursor.",
                )
            cursors.add(cursor)
            params["cursor"] = cursor

    def detail(self, job_id, *, deleted=False):
        data = self._request(
            "GET",
            self.path(job_id),
            params={"include_deleted": str(deleted).lower()},
        )
        self._check_spec(
            data.get("spec"),
            data.get("agent_id"),
            data.get("management_owner"),
            job_id,
        )
        for field in ("definition_version", "management_version"):
            self._version(data, field)
        return data

    @staticmethod
    def _version(detail, field):
        value = detail.get(field)
        if type(value) is not int or value < 1:
            raise click.ClickException(f"Missing Scheduler {field}.")
        return value

    def state(self, job_id):
        data = self.detail(job_id)
        return {
            key: data.get(key)
            for key in (
                "state",
                "task",
                "management_owner",
                "definition_version",
                "management_version",
                "management_status",
                "dispatch_state",
            )
        }

    @staticmethod
    def _check_operation(operation, job_id=None, operation_id=None):
        if (
            not isinstance(operation, dict)
            or not operation.get("operation_id")
            or not operation.get("job_id")
            or operation.get("status")
            not in {"pending", "running", "completed", "failed", "superseded"}
        ):
            raise click.ClickException("Invalid Scheduler operation receipt.")
        if job_id is not None and operation["job_id"] != job_id:
            raise click.ClickException(
                "Scheduler operation receipt belongs to another job.",
            )
        if (
            operation_id is not None
            and operation["operation_id"] != operation_id
        ):
            raise click.ClickException("Scheduler operation ID mismatch.")

    def _operation(
        self,
        method,
        path,
        payload=None,
        *,
        job_id=None,
        operation_id=None,
    ):
        body = (
            {"request_id": self.request_id, **payload}
            if payload is not None
            else None
        )
        data = self._request(method, path, body=body)
        operation = data.get("operation")
        self._check_operation(operation, job_id, operation_id)
        if body is not None:
            data["request_id"] = self.request_id
        if operation["status"] in {"failed", "superseded"}:
            print_json(data)
            raise click.ClickException(
                "Scheduler operation failed; inspect the original operation.",
            )
        return data

    def create(self, spec):
        spec = copy.deepcopy(spec)
        spec.pop("id", None)
        return self._operation(
            "POST",
            JOBS,
            {"spec": spec, "dispatch_state": "ordinary"},
        )

    def update(self, job_id, spec, current):
        if spec.get(
            "enabled",
            current["spec"].get("enabled", True),
        ) != current["spec"].get("enabled", True):
            raise click.UsageError(
                "Use pause/resume to change enabled state; update performs no partial writes.",
            )
        self._check_binding(current["spec"], spec)
        return self._operation(
            "PUT",
            self.path(job_id),
            {
                "spec": spec,
                "expected_definition_version": self._version(
                    current,
                    "definition_version",
                ),
            },
            job_id=job_id,
        )

    @staticmethod
    def _check_binding(old, new):
        pairs = [
            (
                old.get("meta") or {},
                new.get("meta") or {},
                ("task_session_id", "task_chat_id", "creator_user_id"),
            ),
            (
                old.get("request") or {},
                new.get("request") or {},
                ("session_id", "user_id"),
            ),
            (
                (old.get("dispatch") or {}).get("target") or {},
                (new.get("dispatch") or {}).get("target") or {},
                ("session_id", "user_id"),
            ),
        ]
        for previous, updated, keys in pairs:
            for key in keys:
                if updated.get(key, previous.get(key)) != previous.get(key):
                    label = (
                        "target user"
                        if key in {"user_id", "creator_user_id"}
                        else "session/chat binding"
                    )
                    raise click.UsageError(
                        f"update cannot replace the original {label}.",
                    )

    def delete(self, job_id):
        current = self.detail(job_id, deleted=True)
        return self._operation(
            "DELETE",
            self.path(job_id),
            {
                "expected_definition_version": self._version(
                    current,
                    "definition_version",
                ),
            },
            job_id=job_id,
        )

    def set_enabled(self, job_id, enabled):
        current = self.detail(job_id)
        return self._operation(
            "PUT",
            self.path(job_id) + "/enabled",
            {
                "enabled": enabled,
                "expected_management_version": self._version(
                    current,
                    "management_version",
                ),
            },
            job_id=job_id,
        )

    def run(self, job_id):
        current = self.detail(job_id)
        data = self._request(
            "POST",
            self.path(job_id) + "/run",
            body={
                "request_id": self.request_id,
                "expected_definition_version": self._version(
                    current,
                    "definition_version",
                ),
            },
        )
        execution = data.get("execution")
        if (
            not isinstance(execution, dict)
            or execution.get("job_id") != job_id
            or not execution.get("execution_id")
        ):
            raise click.ClickException("Invalid Scheduler execution receipt.")
        return {**data, "request_id": self.request_id}

    def operation(self, job_id, operation_id=None):
        suffix = (
            quote(identity(operation_id, "operation-id"), safe="")
            if operation_id
            else "pending"
        )
        data = self._request(
            "GET",
            self.path(job_id) + "/operations/" + suffix,
        )
        row = data if operation_id else data.get("operation")
        if row is not None:
            self._check_operation(row, job_id, operation_id)
        return data

    def retry(self, job_id, operation_id):
        suffix = quote(identity(operation_id, "operation-id"), safe="")
        return self._operation(
            "POST",
            self.path(job_id) + "/operations/" + suffix + "/retry",
            job_id=job_id,
            operation_id=operation_id,
        )

    def execution(self, job_id, execution_id):
        suffix = quote(identity(execution_id, "execution-id"), safe="")
        data = self._request(
            "GET",
            self.path(job_id) + "/executions/" + suffix,
        )
        if (
            data.get("job_id") != job_id
            or data.get("execution_id") != execution_id
        ):
            raise click.ClickException("Scheduler execution ID mismatch.")
        return data
