# -*- coding: utf-8 -*-
"""Request-scoped validation and publication for annotated HTML revisions."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Awaitable, Callable

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...config.context import get_current_workspace_dir
from ..tool_failure import ToolExecutionError
from .copy_file_to_static import copy_file_to_static

_ANNOTATION_INSTRUMENTATION = re.compile(
    r"data-copaw-annotation(?:-[\w-]+)?|"
    r"copaw-annotation-(?:overlay|editor|marker)",
    re.IGNORECASE,
)


def _fail(message: str) -> None:
    raise ToolExecutionError(error_type="invalid_arguments", detail=message)


class _ResourceManifestParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.resources: set[tuple[str, str]] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.resources.add(("script", values["src"] or ""))
        if tag == "link" and values.get("href"):
            self.resources.add(("link", values["href"] or ""))


def _resource_manifest(html: str) -> set[tuple[str, str]]:
    parser = _ResourceManifestParser()
    parser.feed(html)
    return parser.resources


def _workspace_file(path_value: str, workspace: Path) -> Path:
    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace)
    except FileNotFoundError as exc:
        _fail(f"HTML output not found: {candidate}")
        raise AssertionError from exc
    except (OSError, ValueError) as exc:
        _fail("HTML output must remain inside the current workspace")
        raise AssertionError from exc
    return resolved


def _validate_reported_ids(
    expected: tuple[str, ...],
    applied: list[str],
    unresolved: list[str],
) -> None:
    reported = [*applied, *unresolved]
    if len(reported) != len(set(reported)) or set(reported) != set(expected):
        _fail("every expected annotation ID must be reported exactly once")


def create_publish_annotated_html_tool(
    *,
    source_path: Path,
    expected_annotation_ids: tuple[str, ...],
) -> Callable[..., Awaitable[ToolResponse]]:
    """Bind publication authority to one server-validated annotation source."""
    trusted_source = Path(source_path).expanduser().resolve(strict=True)
    trusted_source_digest = hashlib.sha256(
        trusted_source.read_bytes(),
    ).digest()

    async def publish_annotated_html(
        output_path: str,
        applied_annotation_ids: list[str],
        unresolved_annotation_ids: list[str],
    ) -> ToolResponse:
        """Validate and publish a new HTML revision for this annotation turn.

        Args:
            output_path: Distinct revised HTML path inside the current workspace.
            applied_annotation_ids: Annotation IDs successfully applied.
            unresolved_annotation_ids: Annotation IDs not safely resolved.
        """
        workspace_value = get_current_workspace_dir()
        if workspace_value is None:
            _fail("workspace directory is not configured")
        workspace = Path(workspace_value).expanduser().resolve()
        try:
            trusted_source.relative_to(workspace)
        except ValueError:
            _fail(
                "validated annotation source is outside the current workspace",
            )

        output = _workspace_file(output_path, workspace)
        if output == trusted_source:
            _fail("output must be a distinct new HTML file")
        if output.suffix.lower() not in {".html", ".htm"}:
            _fail("output must be an HTML file")
        if output.stat().st_size == 0:
            _fail("HTML output is empty")

        _validate_reported_ids(
            expected_annotation_ids,
            applied_annotation_ids,
            unresolved_annotation_ids,
        )
        source_bytes = trusted_source.read_bytes()
        if hashlib.sha256(source_bytes).digest() != trusted_source_digest:
            _fail("annotation source changed after request validation")
        source_html = source_bytes.decode("utf-8")
        output_html = output.read_text(encoding="utf-8")
        source_marker_count = len(
            _ANNOTATION_INSTRUMENTATION.findall(source_html),
        )
        output_marker_count = len(
            _ANNOTATION_INSTRUMENTATION.findall(output_html),
        )
        if output_marker_count > source_marker_count:
            _fail(
                "temporary CoPaw annotation instrumentation remains in output",
            )
        if (
            "<html" not in output_html.lower()
            and "<!doctype html" not in output_html.lower()
        ):
            _fail("output is not a complete HTML document")
        missing_resources = _resource_manifest(
            source_html,
        ) - _resource_manifest(
            output_html,
        )
        if missing_resources:
            _fail("output removed source script or resource declarations")

        response = await copy_file_to_static(str(output))
        payload = json.loads(response.content[0]["text"])
        payload.update(
            {
                "source_path": str(trusted_source),
                "output_path": str(output),
                "applied_annotation_ids": applied_annotation_ids,
                "unresolved_annotation_ids": unresolved_annotation_ids,
                "validation": {
                    "structure": "passed",
                    "resource_manifest": "passed",
                    "browser_smoke": "not_available",
                },
            },
        )
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                ),
            ],
        )

    return publish_annotated_html


__all__ = ["create_publish_annotated_html_tool"]
