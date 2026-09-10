# -*- coding: utf-8 -*-
import json
from pathlib import Path

import pytest

from swe.agents.tool_failure import ToolExecutionError
from swe.agents.tools.publish_annotated_html import (
    create_publish_annotated_html_tool,
)
from swe.app.agent_context import set_current_agent_id
from swe.config.context import (
    reset_current_user_id,
    reset_current_workspace_dir,
    set_current_user_id,
    set_current_workspace_dir,
)


def _payload(response) -> dict:
    return json.loads(response.content[0]["text"])


@pytest.fixture
def publication_context(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = workspace / "media" / "source.html"
    source.parent.mkdir(parents=True)
    source.write_text(
        "<!doctype html><link href='app.css' rel='stylesheet'>"
        "<script src='app.js'></script><main>before</main>",
        encoding="utf-8",
    )
    monkeypatch.setenv("FILE_URL", "https://files.example")
    set_current_agent_id("agent-a")
    user_token = set_current_user_id("alice")
    workspace_token = set_current_workspace_dir(workspace)
    try:
        yield workspace, source
    finally:
        reset_current_workspace_dir(workspace_token)
        reset_current_user_id(user_token)
        set_current_agent_id("default")


@pytest.mark.asyncio
async def test_publishes_distinct_valid_html_with_annotation_manifest(
    publication_context,
) -> None:
    workspace, source = publication_context
    output = workspace / "report-revised.html"
    output.write_text(
        "<!doctype html><link href='app.css' rel='stylesheet'>"
        "<script src='app.js'></script><main>after</main>",
        encoding="utf-8",
    )
    publish = create_publish_annotated_html_tool(
        source_path=source,
        expected_annotation_ids=("ann-1", "ann-2"),
    )

    response = await publish(
        str(output),
        applied_annotation_ids=["ann-1"],
        unresolved_annotation_ids=["ann-2"],
    )
    payload = _payload(response)

    assert payload["ok"] is True
    assert payload["applied_annotation_ids"] == ["ann-1"]
    assert payload["unresolved_annotation_ids"] == ["ann-2"]
    assert payload["url"].endswith("/report-revised.html")
    assert payload["validation"] == {
        "structure": "passed",
        "resource_manifest": "passed",
        "browser_smoke": "not_available",
    }
    assert (workspace / "static" / "report-revised.html").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_name", "contents", "message"),
    [
        ("empty.html", "", "empty"),
        ("report.txt", "<html></html>", "HTML"),
        (
            "marked.html",
            "<div data-copaw-annotation='1'></div>",
            "instrumentation",
        ),
        (
            "missing-resource.html",
            "<html><main>changed</main></html>",
            "resource",
        ),
    ],
)
async def test_rejects_invalid_revision_output(
    publication_context,
    output_name: str,
    contents: str,
    message: str,
) -> None:
    workspace, source = publication_context
    output = workspace / output_name
    output.write_text(contents, encoding="utf-8")
    publish = create_publish_annotated_html_tool(
        source_path=source,
        expected_annotation_ids=("ann-1",),
    )

    with pytest.raises(ToolExecutionError, match=message):
        await publish(
            str(output),
            applied_annotation_ids=["ann-1"],
            unresolved_annotation_ids=[],
        )


@pytest.mark.asyncio
async def test_allows_preexisting_copaw_data_attributes(
    publication_context,
) -> None:
    workspace, source = publication_context
    source.write_text(
        "<!doctype html><html><main data-copaw-state='ready' "
        "data-copaw-annotation='source-owned'>before</main></html>",
        encoding="utf-8",
    )
    output = workspace / "report-revised.html"
    output.write_text(
        "<!doctype html><html><main data-copaw-state='ready' "
        "data-copaw-annotation='source-owned'>after</main></html>",
        encoding="utf-8",
    )
    publish = create_publish_annotated_html_tool(
        source_path=source,
        expected_annotation_ids=("ann-1",),
    )

    response = await publish(
        str(output),
        applied_annotation_ids=["ann-1"],
        unresolved_annotation_ids=[],
    )

    assert _payload(response)["ok"] is True


@pytest.mark.asyncio
async def test_rejects_source_overwrite_and_workspace_escape(
    publication_context,
    tmp_path: Path,
) -> None:
    workspace, source = publication_context
    publish = create_publish_annotated_html_tool(
        source_path=source,
        expected_annotation_ids=("ann-1",),
    )

    with pytest.raises(ToolExecutionError, match="distinct"):
        await publish(
            str(source),
            applied_annotation_ids=["ann-1"],
            unresolved_annotation_ids=[],
        )

    outside = tmp_path / "outside.html"
    outside.write_text("<html></html>", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="workspace"):
        await publish(
            str(outside),
            applied_annotation_ids=["ann-1"],
            unresolved_annotation_ids=[],
        )


@pytest.mark.asyncio
async def test_rejects_source_content_changed_after_tool_creation(
    publication_context,
) -> None:
    workspace, source = publication_context
    output = workspace / "report-revised.html"
    output.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    publish = create_publish_annotated_html_tool(
        source_path=source,
        expected_annotation_ids=("ann-1",),
    )
    source.write_text(
        source.read_text(encoding="utf-8").replace("before", "overwritten"),
        encoding="utf-8",
    )

    with pytest.raises(ToolExecutionError, match="source.*changed"):
        await publish(
            str(output),
            applied_annotation_ids=["ann-1"],
            unresolved_annotation_ids=[],
        )


@pytest.mark.asyncio
async def test_rejects_unknown_or_unreported_annotation_ids(
    publication_context,
) -> None:
    workspace, source = publication_context
    output = workspace / "report-revised.html"
    output.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    publish = create_publish_annotated_html_tool(
        source_path=source,
        expected_annotation_ids=("ann-1", "ann-2"),
    )

    with pytest.raises(ToolExecutionError, match="exactly once"):
        await publish(
            str(output),
            applied_annotation_ids=["ann-1", "ann-unknown"],
            unresolved_annotation_ids=[],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "replacement", "preserved_script"),
    [
        (
            "static_interactive.html",
            ("Sales report", "Monthly sales report"),
            "toggleAttribute('hidden')",
        ),
        (
            "dynamic_interactive.html",
            ("Running", "Active"),
            "setInterval(render, 1000)",
        ),
    ],
)
async def test_fixture_revision_preserves_unrelated_interactions_and_rerendering(
    tmp_path: Path,
    monkeypatch,
    fixture_name: str,
    replacement: tuple[str, str],
    preserved_script: str,
) -> None:
    fixture = (
        Path(__file__).parents[3]
        / "fixtures"
        / "document_annotations"
        / fixture_name
    )
    workspace = tmp_path / "workspace"
    source = workspace / "media" / fixture_name
    source.parent.mkdir(parents=True)
    source.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    output = workspace / fixture_name.replace(".html", "-revised.html")
    output.write_text(
        source.read_text(encoding="utf-8").replace(*replacement),
        encoding="utf-8",
    )
    monkeypatch.setenv("FILE_URL", "https://files.example")
    set_current_agent_id("agent-a")
    user_token = set_current_user_id("alice")
    workspace_token = set_current_workspace_dir(workspace)
    try:
        publish = create_publish_annotated_html_tool(
            source_path=source,
            expected_annotation_ids=("ann-1",),
        )
        response = await publish(
            str(output),
            applied_annotation_ids=["ann-1"],
            unresolved_annotation_ids=[],
        )
    finally:
        reset_current_workspace_dir(workspace_token)
        reset_current_user_id(user_token)
        set_current_agent_id("default")

    assert _payload(response)["ok"] is True
    revised = output.read_text(encoding="utf-8")
    assert preserved_script in revised
    assert replacement[1] in revised


@pytest.mark.asyncio
async def test_unresolved_fixture_is_reported_without_flattening_or_overwrite(
    publication_context,
) -> None:
    workspace, source = publication_context
    fixture_dir = (
        Path(__file__).parents[3] / "fixtures" / "document_annotations"
    )
    source_html = (fixture_dir / "unresolved_target.html").read_text(
        encoding="utf-8",
    )
    report = json.loads(
        (fixture_dir / "unresolved_report.json").read_text(encoding="utf-8"),
    )
    source.write_text(source_html, encoding="utf-8")
    output = workspace / "unresolved-target-revised.html"
    output.write_text(source_html, encoding="utf-8")
    expected_ids = tuple(
        report["applied_annotation_ids"] + report["unresolved_annotation_ids"],
    )
    publish = create_publish_annotated_html_tool(
        source_path=source,
        expected_annotation_ids=expected_ids,
    )

    response = await publish(
        str(output),
        applied_annotation_ids=report["applied_annotation_ids"],
        unresolved_annotation_ids=report["unresolved_annotation_ids"],
    )
    payload = _payload(response)

    assert payload["ok"] is True
    assert (
        payload["applied_annotation_ids"] == report["applied_annotation_ids"]
    )
    assert (
        payload["unresolved_annotation_ids"]
        == report["unresolved_annotation_ids"]
    )
    assert source.read_text(encoding="utf-8") == source_html
    assert output != source
    assert output.read_text(encoding="utf-8") == source_html
    assert '<canvas id="opaque-chart"' in source_html
    assert "context.fillRect(10, 10, 100, 40)" in source_html
