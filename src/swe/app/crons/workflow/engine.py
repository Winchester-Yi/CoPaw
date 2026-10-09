"""One synchronous JSON HTTP call for one workflow Scheduled Run attempt."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .models import WorkflowConfig, WorkflowValueMapping

Renderer = Callable[[dict[str, Any]], str]
ClientFactory = Callable[[float], httpx.AsyncClient]
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RENDERED_CHARS = 100_000


class WorkflowCallError(RuntimeError):
    """Public-safe failure with optional diagnostic HTTP status."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str = "call_failed",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


def allowed_workflow_hosts(configured: set[str] | None = None) -> set[str]:
    hosts = (
        configured
        if configured is not None
        else set(os.environ.get("SWE_WORKFLOW_ALLOWED_HOSTS", "").split(","))
    )
    return {host.strip().casefold() for host in hosts if host.strip()}


def ensure_workflow_host_approved(url: str, hosts: set[str]) -> None:
    hostname = (urlsplit(url).hostname or "").casefold()
    if not hosts or hostname not in hosts:
        raise ValueError("workflow endpoint host is not approved")


@dataclass(frozen=True)
class WorkflowOutcome:
    display_text: str
    selected_result: dict[str, Any]
    input_snapshot: dict[str, Any]
    http_status: int


def _read_path(document: Any, path: list[str | int]) -> Any:
    current = document
    for segment in path:
        if isinstance(current, dict) and isinstance(segment, str):
            if segment not in current:
                raise RuntimeError(
                    f"workflow response field missing: {segment}"
                )
            current = current[segment]
        elif isinstance(current, list) and isinstance(segment, int):
            if segment < 0 or segment >= len(current):
                raise RuntimeError(
                    f"workflow response index missing: {segment}"
                )
            current = current[segment]
        else:
            raise RuntimeError(f"workflow response field invalid: {segment}")
    return current


def _resolve_value(
    mapping: WorkflowValueMapping,
    env: Mapping[str, str],
    runtime: Mapping[str, Any],
    secrets: Mapping[str, str],
) -> Any:
    if mapping.source == "literal":
        value = mapping.value
    else:
        source = {
            "env": env,
            "runtime": runtime,
            "secret": secrets,
        }[mapping.source]
        value = source.get(mapping.key)
        if value is None or value == "":
            raise ValueError(
                f"workflow {mapping.source} value missing: {mapping.key}",
            )
    return f"{mapping.prefix}{value}" if mapping.prefix else value


def _resolve_template(
    value: Any,
    env: Mapping[str, str],
    runtime: Mapping[str, Any],
    secrets: Mapping[str, str],
) -> Any:
    if isinstance(value, dict):
        if "source" in value and set(value) <= {
            "source",
            "key",
            "value",
            "prefix",
        }:
            return _resolve_value(
                WorkflowValueMapping.model_validate(value),
                env,
                runtime,
                secrets,
            )
        return {
            key: _resolve_template(item, env, runtime, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_template(item, env, runtime, secrets) for item in value
        ]
    return value


def _redact_template(value: Any) -> Any:
    if isinstance(value, dict):
        if "source" in value and set(value) <= {
            "source",
            "key",
            "value",
            "prefix",
        }:
            if value["source"] == "literal":
                return value.get("value")
            return f"<{value['source']}:{value.get('key', '')}>"
        return {key: _redact_template(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_template(item) for item in value]
    return value


def _direct_render(fields: dict[str, Any]) -> str:
    value = next(iter(fields.values())) if len(fields) == 1 else fields
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


_REGISTERED_RENDERERS: dict[str, Renderer] = {"direct": _direct_render}


def register_workflow_renderer(name: str, renderer: Renderer) -> None:
    """Register a reviewed, deterministic renderer referenced by DB config."""
    if not name or name == "direct" or name in _REGISTERED_RENDERERS:
        raise ValueError("workflow renderer name is invalid or already used")
    _REGISTERED_RENDERERS[name] = renderer


def is_registered_workflow_renderer(name: str) -> bool:
    return name in _REGISTERED_RENDERERS


class WorkflowEngine:
    """Execute configured HTTP calls without constructing an Agent."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        renderers: Mapping[str, Renderer] | None = None,
        allowed_hosts: set[str] | None = None,
    ) -> None:
        self._client_factory = client_factory or (
            lambda timeout: httpx.AsyncClient(timeout=timeout)
        )
        self._renderers: dict[str, Renderer] = dict(_REGISTERED_RENDERERS)
        self._renderers.update(renderers or {})
        self._allowed_hosts = allowed_workflow_hosts(allowed_hosts)

    async def execute(
        self,
        config: WorkflowConfig,
        *,
        env: Mapping[str, str],
        target_user_id: str,
        runtime: Mapping[str, Any] | None = None,
        secrets: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> WorkflowOutcome:
        """Make one HTTP request and turn its JSON into display text."""
        if not target_user_id or env.get("sapId") != target_user_id:
            raise ValueError("workflow sapId does not match target user")
        ensure_workflow_host_approved(config.url, self._allowed_hosts)
        renderer = self._renderers.get(config.renderer_key) or (
            _REGISTERED_RENDERERS.get(config.renderer_key)
        )
        if renderer is None:
            raise ValueError(
                f"workflow renderer unavailable: {config.renderer_key}"
            )

        runtime_values = runtime or {}
        secret_values = secrets or {}
        headers = {
            name: str(
                _resolve_value(value, env, runtime_values, secret_values)
            )
            for name, value in config.headers.items()
        }
        query = {
            name: _resolve_value(value, env, runtime_values, secret_values)
            for name, value in config.query.items()
        }
        body = (
            _resolve_template(config.body, env, runtime_values, secret_values)
            if config.body is not None
            else None
        )
        parsed_url = urlsplit(config.url)
        request_path = re.sub(
            r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda match: quote(
                str(
                    _resolve_value(
                        config.path_params[match.group(1)],
                        env,
                        runtime_values,
                        secret_values,
                    ),
                ),
                safe="",
            ),
            parsed_url.path,
        )
        request_url = urlunsplit(
            (parsed_url.scheme, parsed_url.netloc, request_path, "", ""),
        )
        timeout = min(float(config.timeout_seconds), timeout_seconds or 7200.0)
        try:
            async with self._client_factory(timeout) as client:
                async with client.stream(
                    config.method,
                    request_url,
                    headers=headers,
                    params=query,
                    json=body,
                ) as response:
                    if not response.is_success:
                        raise WorkflowCallError(
                            "技能任务执行失败",
                            status_code=response.status_code,
                            error_code="http_status",
                        )
                    response_bytes = bytearray()
                    async for chunk in response.aiter_bytes():
                        if (
                            len(response_bytes) + len(chunk)
                            > MAX_RESPONSE_BYTES
                        ):
                            raise RuntimeError(
                                "workflow response exceeds size limit",
                            )
                        response_bytes.extend(chunk)
                    http_status = response.status_code
        except httpx.TimeoutException as exc:
            raise WorkflowCallError(
                "技能任务执行超时",
                error_code="timeout",
            ) from exc
        except httpx.RequestError as exc:
            raise WorkflowCallError("技能任务执行失败") from exc
        try:
            document = json.loads(response_bytes)
        except ValueError as exc:
            raise WorkflowCallError(
                "技能任务结果不可用",
                error_code="invalid_json",
            ) from exc
        if config.success_rule is not None:
            try:
                actual = _read_path(document, config.success_rule.path)
            except RuntimeError as exc:
                raise WorkflowCallError(
                    "技能任务执行失败",
                    error_code="business_failure",
                ) from exc
            if actual != config.success_rule.equals:
                raise WorkflowCallError(
                    "技能任务执行失败",
                    error_code="business_failure",
                )

        try:
            selected = {
                name: _read_path(document, path)
                for name, path in config.result_fields.items()
            }
        except RuntimeError as exc:
            raise WorkflowCallError(
                "技能任务结果不可用",
                error_code="missing_result",
            ) from exc
        try:
            display_text = renderer(selected)
        except Exception as exc:
            raise WorkflowCallError(
                "技能任务结果展示失败",
                error_code="render_failure",
            ) from exc
        if not isinstance(display_text, str) or not display_text.strip():
            raise RuntimeError("workflow renderer returned empty text")
        if len(display_text) > MAX_RENDERED_CHARS:
            raise RuntimeError("workflow rendered result exceeds size limit")
        return WorkflowOutcome(
            display_text=display_text,
            selected_result=selected,
            input_snapshot={
                "workflow_binding_id": config.binding_id,
                "workflow_version": config.version,
                "query": {
                    name: _redact_template(value.model_dump())
                    for name, value in config.query.items()
                },
                "path_params": {
                    name: _redact_template(value.model_dump())
                    for name, value in config.path_params.items()
                },
                "body": _redact_template(config.body),
            },
            http_status=http_status,
        )
