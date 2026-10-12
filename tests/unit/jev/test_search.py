import asyncio
import json

import httpx
import pytest

from swe.jev.config import JevConfig
from swe.jev.context import jev_context, current_jev_context
from swe.jev.search import grep_snippets, select_ids, filter_search_results
from swe.config.context import tenant_context


@pytest.mark.parametrize(
    "scores,k,threshold,want",
    [
        ({"a": 0.95, "b": 0.9, "c": 0.85, "d": 0.2}, 2, 0.8, ["a", "b", "c"]),
        ({"a": 0.4, "b": 0.3, "c": 0.2}, 2, 0.8, ["a", "b"]),
        ({"a": 0.2}, 5, 0.8, ["a"]),
        ({"a": 0.8, "b": 0.8, "c": 0.8}, 1, 0.8, ["a", "b", "c"]),
        ({}, 2, 0.8, []),
    ],
)
def test_union_and_stable_order(scores, k, threshold, want):
    assert select_ids(scores, k, threshold) == want


def test_overlapping_grep_context_is_one_evidence_block():
    lines = [
        "auth.py:1:  start",
        "auth.py:2:> hit",
        "auth.py:3:  context",
        "---",
        "auth.py:2:  hit",
        "auth.py:3:> context",
        "auth.py:4:  end",
        "---",
        "other.py:1:> separate",
        "---",
    ]
    assert grep_snippets(lines) == [
        "auth.py:1:  start\nauth.py:2:> hit\nauth.py:3:> context\nauth.py:4:  end",
        "other.py:1:> separate",
    ]


def config():
    return JevConfig(
        endpoint="https://jev.test/decide",
        timeout_seconds=1,
        search_filtering={"enabled": True, "top_k": 1, "threshold": 0.8},
    )


async def test_context_is_task_local_and_reset_after_cancellation():
    async def run(task):
        with jev_context(config(), task):
            await asyncio.sleep(0)
            assert current_jev_context().task == task

    await asyncio.gather(run("alice"), run("bob"))
    assert current_jev_context() is None
    with pytest.raises(asyncio.CancelledError):
        with jev_context(config(), "cancel"):
            raise asyncio.CancelledError()
    assert current_jev_context() is None


async def test_filter_preserves_partial_status_and_recoverable_original(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "swe.security.tenant_path_boundary.WORKING_DIR", tmp_path
    )
    tmp_path = tmp_path / "alice"
    tmp_path.mkdir()
    original_client = httpx.AsyncClient

    def respond(request):
        body = json.loads(request.content)
        assert "release connections" in body["state"]
        assert "login" in body["state"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    "snippet_0": {"type": "noul", "noul": 0.1},
                    "snippet_1": {"type": "noul", "noul": 0.95},
                    "snippet_2": {"type": "noul", "noul": 0.85},
                }
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original_client(
            transport=httpx.MockTransport(respond),
            **kw,
        ),
    )
    lines = ["noise.py", "db.py", "auth.py"]
    original = "\n".join(lines) + "\n\n(Results truncated at 3 entries.)"
    with tenant_context(tenant_id="alice", workspace_dir=tmp_path):
        with jev_context(config(), "fix login"):
            result = await filter_search_results(
                lines,
                original,
                query="*.py",
                kind="glob",
                intent="release connections",
            )
    assert result.startswith("db.py\nauth.py")
    assert "Results truncated at 3 entries." in result
    assert "retained=2 candidates=3" in result
    artifacts = list((tmp_path / "tool_result").glob("jev-*.txt"))
    assert len(artifacts) == 1
    assert artifacts[0].read_text() == original
    assert str(artifacts[0]) in result


async def test_disabled_filter_keeps_exact_original(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("HTTP"))
    assert (
        await filter_search_results(
            ["a"],
            "a\nstatus",
            query="a",
            kind="glob",
        )
        == "a\nstatus"
    )


async def test_public_glob_filters_and_can_be_bypassed(monkeypatch, tmp_path):
    from swe.agents.tools.file_search import glob_search

    monkeypatch.setattr(
        "swe.security.tenant_path_boundary.WORKING_DIR", tmp_path
    )
    workspace = tmp_path / "alice"
    workspace.mkdir()
    for name in ["a.py", "b.py", "c.py"]:
        (workspace / name).write_text("hit")
    original_client = httpx.AsyncClient

    def respond(request):
        return httpx.Response(
            200,
            json={
                "answers": {
                    "snippet_0": {"type": "noul", "noul": 0.1},
                    "snippet_1": {"type": "noul", "noul": 0.9},
                    "snippet_2": {"type": "noul", "noul": 0.2},
                }
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original_client(
            transport=httpx.MockTransport(respond),
            **kw,
        ),
    )
    with tenant_context(tenant_id="alice", workspace_dir=workspace):
        with jev_context(config(), "find relevant code"):
            result = await glob_search("*.py")
            assert result.content[0]["text"].startswith("b.py\n\n[Jev:")
            unfiltered = await glob_search("*.py", rerank=False)
            assert unfiltered.content[0]["text"] == "a.py\nb.py\nc.py"


def test_truncated_context_window_stays_whole():
    lines = ["a.py:1:  before", "a.py:2:> hit", "a.py:3:  after"]
    assert grep_snippets(lines) == ["\n".join(lines)]


@pytest.mark.parametrize("failure", ["service", "artifact"])
async def test_filter_failure_returns_exact_original(monkeypatch, failure):
    from swe.jev.client import JevClient

    async def relevance(self, state, candidates):
        return None if failure == "service" else {"snippet_0": 0.9}

    def cannot_save(original):
        raise OSError("disk full")

    monkeypatch.setattr(JevClient, "relevance", relevance)
    monkeypatch.setattr("swe.jev.search._save_original", cannot_save)
    original = "a.py\n\n(scan timed out)"
    with jev_context(config(), "find code"):
        assert (
            await filter_search_results(
                ["a.py"],
                original,
                query="*.py",
                kind="glob",
            )
            == original
        )
