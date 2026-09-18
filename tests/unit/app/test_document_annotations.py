# -*- coding: utf-8 -*-
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from swe.app.routers.console import _append_document_annotation_context
from swe.app.document_annotations import (
    DOCUMENT_ANNOTATION_MAX_BUNDLE_BYTES,
    DocumentAnnotationValidationError,
    build_document_annotation_directive,
    validate_document_annotation_request,
)

FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "document_annotations"
    / "source_resolvable.json"
)


def _bundle(source: Path) -> dict:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["source"][
        "attachment_url"
    ] = f"/files/preview/{str(source).lstrip('/')}"
    payload["source"]["sha256"] = hashlib.sha256(
        source.read_bytes(),
    ).hexdigest()
    return payload


def _parts(bundle: dict) -> list[dict]:
    return [
        {"type": "text", "text": "请修改"},
        {
            "type": "file",
            "file_url": bundle["source"]["attachment_url"],
            "file_name": bundle["source"]["file_name"],
        },
    ]


def test_validates_same_turn_html_attachment_and_builds_escaped_directive(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "media" / "report.annotation-source.html"
    source.parent.mkdir(parents=True)
    source.write_text(
        "<!doctype html><script>window.ok=true</script>",
        encoding="utf-8",
    )
    bundle = _bundle(source)
    bundle["annotations"][0]["comment"] = "改成 <两列> & 保持交互"

    context = validate_document_annotation_request(
        bundle,
        content_parts=_parts(bundle),
        workspace_dir=workspace,
    )
    directive = build_document_annotation_directive(context)

    assert context.source_path == source.resolve()
    assert context.expected_annotation_ids == ("ann-001", "ann-002")
    assert "改成 &lt;两列&gt; &amp; 保持交互" in directive
    assert str(source.resolve()) in directive
    assert "untrusted document data" in directive
    assert "publish_annotated_html" in directive
    assert "distinct new HTML file" in directive
    assert "serialized live DOM" in directive


def test_runtime_generated_fixture_reaches_the_agent_directive(
    tmp_path: Path,
) -> None:
    fixture_dir = (
        Path(__file__).parents[2] / "fixtures" / "document_annotations"
    )
    runtime_annotation = json.loads(
        (fixture_dir / "runtime_generated.json").read_text(encoding="utf-8"),
    )
    workspace = tmp_path / "workspace"
    source = workspace / "media" / "runtime-report.html"
    source.parent.mkdir(parents=True)
    source.write_text("<html><main id='app'></main></html>", encoding="utf-8")
    bundle = _bundle(source)
    bundle["annotations"] = [runtime_annotation]

    context = validate_document_annotation_request(
        bundle,
        content_parts=_parts(bundle),
        workspace_dir=workspace,
    )
    directive = build_document_annotation_directive(context)

    assert context.expected_annotation_ids == ("ann-runtime",)
    assert context.bundle.annotations[0].target.runtime_generated is True
    assert "ann-runtime" in directive
    assert "runtime_generated" in directive
    assert "data-status" in directive
    assert "serialized live DOM" in directive


def test_accepts_absent_optional_target_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "media" / "simple.html"
    source.parent.mkdir(parents=True)
    source.write_text("<p id='x'>x</p>", encoding="utf-8")
    bundle = _bundle(source)
    bundle["annotations"] = [
        {
            "id": "ann-1",
            "comment": "改为 y",
            "target": {
                "runtime_generated": False,
                "stable_attributes": {"id": "x"},
            },
        },
    ]

    context = validate_document_annotation_request(
        bundle,
        content_parts=_parts(bundle),
        workspace_dir=workspace,
    )

    assert context.expected_annotation_ids == ("ann-1",)


def test_matches_preview_url_to_normalized_same_turn_file_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "media" / "report.html"
    source.parent.mkdir(parents=True)
    source.write_text("<p>ok</p>", encoding="utf-8")
    bundle = _bundle(source)
    parts = _parts(bundle)
    parts[1]["file_url"] = str(source)

    context = validate_document_annotation_request(
        bundle,
        content_parts=parts,
        workspace_dir=workspace,
    )

    assert context.source_path == source.resolve()


def test_rejects_absolute_path_as_declared_attachment_url(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "media" / "report.html"
    source.parent.mkdir(parents=True)
    source.write_text("<p>ok</p>", encoding="utf-8")
    bundle = _bundle(source)
    bundle["source"]["attachment_url"] = str(source)

    with pytest.raises(
        DocumentAnnotationValidationError,
        match="invalid attachment URL",
    ):
        validate_document_annotation_request(
            bundle,
            content_parts=_parts(bundle),
            workspace_dir=workspace,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(schema_version=2), "schema_version"),
        (
            lambda value: value["annotations"][0].update(comment="x" * 2049),
            "comment",
        ),
        (
            lambda value: value["annotations"][0].update(
                target={"runtime_generated": False},
            ),
            "target evidence",
        ),
    ],
)
def test_rejects_invalid_contract(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "media" / "report.html"
    source.parent.mkdir(parents=True)
    source.write_text("<p>ok</p>", encoding="utf-8")
    bundle = _bundle(source)
    mutate(bundle)

    with pytest.raises(DocumentAnnotationValidationError, match=message):
        validate_document_annotation_request(
            bundle,
            content_parts=_parts(bundle),
            workspace_dir=workspace,
        )


def test_rejects_oversize_bundle(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "media" / "report.html"
    source.parent.mkdir(parents=True)
    source.write_text("<p>ok</p>", encoding="utf-8")
    bundle = _bundle(source)
    bundle["padding"] = "x" * DOCUMENT_ANNOTATION_MAX_BUNDLE_BYTES

    with pytest.raises(DocumentAnnotationValidationError, match="too large"):
        validate_document_annotation_request(
            bundle,
            content_parts=_parts(bundle),
            workspace_dir=workspace,
        )


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        (lambda source, bundle, parts: parts.clear(), "same-turn"),
        (
            lambda source, bundle, parts: bundle["source"].update(
                file_name="report.txt",
            ),
            "HTML",
        ),
        (
            lambda source, bundle, parts: bundle["source"].update(
                sha256="0" * 64,
            ),
            "digest",
        ),
        (lambda source, bundle, parts: source.unlink(), "not found"),
        (
            lambda source, bundle, parts: parts[1].update(
                file_name="different.html",
            ),
            "file name",
        ),
    ],
)
def test_rejects_invalid_source_attachment(
    tmp_path: Path,
    prepare,
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "media" / "report.html"
    source.parent.mkdir(parents=True)
    source.write_text("<p>ok</p>", encoding="utf-8")
    bundle = _bundle(source)
    parts = _parts(bundle)
    prepare(source, bundle, parts)

    with pytest.raises(DocumentAnnotationValidationError, match=message):
        validate_document_annotation_request(
            bundle,
            content_parts=parts,
            workspace_dir=workspace,
        )


def test_rejects_path_outside_current_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "other-tenant" / "media" / "report.html"
    source.parent.mkdir(parents=True)
    source.write_text("<p>secret</p>", encoding="utf-8")
    bundle = _bundle(source)

    with pytest.raises(DocumentAnnotationValidationError, match="workspace"):
        validate_document_annotation_request(
            bundle,
            content_parts=_parts(bundle),
            workspace_dir=workspace,
        )


@pytest.mark.asyncio
async def test_console_request_appends_server_owned_hidden_context(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "media" / "report.html"
    source.parent.mkdir(parents=True)
    source.write_text("<html><p id='x'>ok</p></html>", encoding="utf-8")
    bundle = _bundle(source)
    native_payload: dict[str, Any] = {
        "content_parts": _parts(bundle),
        "meta": {"system_prompt_injections": ["existing"]},
    }

    await _append_document_annotation_context(
        {"document_annotations": bundle},
        native_payload,
        type("Workspace", (), {"workspace_dir": workspace})(),
    )

    assert native_payload["meta"]["system_prompt_injections"][0] == "existing"
    assert (
        "document-annotation-task"
        in native_payload["meta"]["system_prompt_injections"][1]
    )
    assert native_payload["meta"]["_document_annotation_context"] == {
        "source_path": str(source.resolve()),
        "expected_annotation_ids": ["ann-001", "ann-002"],
    }
    assert all(
        "document-annotation-task" not in str(part)
        for part in native_payload["content_parts"]
    )


@pytest.mark.asyncio
async def test_console_request_without_annotations_is_unchanged() -> None:
    native_payload = {"content_parts": [], "meta": {"session_id": "chat-1"}}

    await _append_document_annotation_context(
        {},
        native_payload,
        type("Workspace", (), {"workspace_dir": "/unused"})(),
    )

    assert native_payload == {
        "content_parts": [],
        "meta": {"session_id": "chat-1"},
    }
