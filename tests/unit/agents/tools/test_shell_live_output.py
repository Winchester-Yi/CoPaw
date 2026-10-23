# -*- coding: utf-8 -*-
import asyncio
import os
import signal
import sys
import threading
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_unix_shell_decodes_complete_output_in_worker(
    monkeypatch,
    tmp_path,
):
    from swe.agents.tools import shell

    observed_threads = []
    original_decode = shell.smart_decode

    def record_decode(data):
        observed_threads.append(threading.get_ident())
        return original_decode(data)

    monkeypatch.setattr(shell, "smart_decode", record_decode)
    returncode, stdout, stderr = await shell._execute_unix_subprocess(
        "printf hello",
        tmp_path,
        5,
        os.environ.copy(),
    )
    assert (returncode, stdout, stderr) == (0, "hello", "")
    assert observed_threads
    assert threading.get_ident() not in observed_threads


@pytest.mark.asyncio
async def test_shell_response_formatting_runs_in_worker(monkeypatch, tmp_path):
    from swe.agents.tools import shell

    observed_threads = []
    original_format = shell._format_shell_response

    def record_format(*args):
        observed_threads.append(threading.get_ident())
        return original_format(*args)

    async def fake_subprocess(*args, **kwargs):
        return 0, "hello", "warning"

    @asynccontextmanager
    async def no_execution_slot(_policy):
        yield

    monkeypatch.setattr(shell, "_format_shell_response", record_format)
    monkeypatch.setattr(shell, "_execute_platform_subprocess", fake_subprocess)
    monkeypatch.setattr(
        shell,
        "_tenant_shell_execution_slot",
        no_execution_slot,
    )
    monkeypatch.setattr(
        shell,
        "prepare_shell_command",
        lambda *a: SimpleNamespace(
            command="ignored",
            working_dir=tmp_path,
            env=os.environ.copy(),
            python_runtime_guard=nullcontext(),
        ),
    )
    monkeypatch.setattr(
        shell,
        "resolve_current_process_limit_policy",
        lambda *a: SimpleNamespace(build_preexec_fn=lambda: None),
    )
    monkeypatch.setattr(
        shell,
        "_format_process_limit_diagnostic",
        lambda text, p: text,
    )
    response = await shell.execute_shell_command("ignored")
    assert response.content[0]["text"] == "hello\n[stderr]\nwarning"
    assert observed_threads
    assert threading.get_ident() not in observed_threads


@pytest.mark.asyncio
async def test_shell_terminal_result_is_not_truncated_before_central_compaction(
    monkeypatch,
    tmp_path,
):
    from swe.agents.tools import shell

    raw_output = "head\n" + ("middle\n" * 20_000) + "tail\n"

    async def fake_subprocess(*_args, **_kwargs):
        return 0, raw_output, ""

    @asynccontextmanager
    async def no_execution_slot(_policy):
        yield

    monkeypatch.setattr(
        shell,
        "prepare_shell_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            command="echo ignored",
            working_dir=tmp_path,
            env=os.environ.copy(),
            python_runtime_guard=nullcontext(),
        ),
    )
    monkeypatch.setattr(
        shell,
        "resolve_current_process_limit_policy",
        lambda _tool_name: SimpleNamespace(build_preexec_fn=lambda: None),
    )
    monkeypatch.setattr(
        shell,
        "_tenant_shell_execution_slot",
        no_execution_slot,
    )
    monkeypatch.setattr(shell, "_execute_platform_subprocess", fake_subprocess)
    monkeypatch.setattr(
        shell,
        "_format_process_limit_diagnostic",
        lambda text, _policy: text,
    )

    result = await shell.execute_shell_command("echo ignored")

    assert result.content[0]["text"] == raw_output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_budget", "expected_budget"),
    [(80, 80), (None, 50 * 1024)],
)
async def test_live_frame_uses_current_recent_tool_result_budget(
    configured_budget,
    expected_budget,
):
    from swe.app.runner.tool_output_frames import (
        bind_tool_output_emitter,
        emit_tool_output_text,
        tool_output_invocation,
    )
    from swe.config.context import set_current_recent_max_bytes

    frames = []

    async def collect(frame):
        frames.append(frame)

    set_current_recent_max_bytes(configured_budget)
    try:
        with (
            bind_tool_output_emitter(collect),
            tool_output_invocation(
                tool_call_id="call-1",
                tool_name="execute_shell_command",
            ),
        ):
            await emit_tool_output_text(
                "stdout",
                "x" * (expected_budget + 1),
            )
    finally:
        set_current_recent_max_bytes(None)

    assert sum(len(frame["text"].encode("utf-8")) for frame in frames) <= (
        expected_budget
    )
    assert frames[-1]["budget_bytes"] == expected_budget
    assert frames[-1]["truncated"] is True
    assert "[早期实时输出已省略]" in frames[-1]["text"]


@pytest.mark.asyncio
async def test_unix_shell_emits_live_stdout_and_stderr_frames(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Unix subprocess live output is not used on Windows")

    from swe.agents.tools import shell
    from swe.app.runner.tool_output_frames import (
        bind_tool_output_emitter,
        tool_output_invocation,
    )

    frames = []

    async def collect(frame):
        frames.append(frame)

    with (
        bind_tool_output_emitter(collect),
        tool_output_invocation(
            tool_call_id="call-1",
            tool_name="execute_shell_command",
        ),
    ):
        returncode, stdout, stderr = await shell._execute_unix_subprocess(
            "printf 'out-line\\n'; printf 'err-line\\n' >&2",
            tmp_path,
            5,
            os.environ.copy(),
        )

    assert returncode == 0
    assert stdout == "out-line"
    assert stderr == "err-line"

    assert {frame["source"] for frame in frames} == {"stdout", "stderr"}
    assert {frame["text"] for frame in frames} == {"out-line\n", "err-line\n"}
    assert all(frame["object"] == "tool_output_frame" for frame in frames)
    assert all(frame["tool_call_id"] == "call-1" for frame in frames)
    assert all(
        frame["tool_name"] == "execute_shell_command" for frame in frames
    )
    assert [frame["sequence"] for frame in frames] == [1, 2]


@pytest.mark.asyncio
async def test_unix_shell_success_cleans_background_pipe_holders(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Unix subprocess live output is not used on Windows")

    from swe.agents.tools import shell

    returncode, stdout, stderr = await asyncio.wait_for(
        shell._execute_unix_subprocess(
            "sleep 2 & echo done",
            tmp_path,
            1,
            os.environ.copy(),
        ),
        timeout=1,
    )

    assert returncode == 0
    assert stdout == "done"
    assert stderr == ""


@pytest.mark.asyncio
async def test_unix_shell_success_kills_sigterm_ignoring_pipe_holders(
    tmp_path,
):
    if sys.platform == "win32":
        pytest.skip("Unix subprocess live output is not used on Windows")

    from swe.agents.tools import shell

    pid_file = tmp_path / "background.pid"
    background_pid: int | None = None
    try:
        returncode, stdout, stderr = await asyncio.wait_for(
            shell._execute_unix_subprocess(
                (
                    "trap '' TERM; "
                    "while :; do sleep 1; done & "
                    f"echo $! > {pid_file}; "
                    "echo done"
                ),
                tmp_path,
                0.1,
                os.environ.copy(),
            ),
            timeout=4,
        )

        background_pid = int(pid_file.read_text().strip())

        assert returncode == 0
        assert stdout == "done"
        assert stderr == ""
        with pytest.raises(ProcessLookupError):
            os.kill(background_pid, 0)
    finally:
        if background_pid is not None:
            try:
                os.killpg(os.getpgid(background_pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_unix_shell_success_cleans_background_process_group(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Unix subprocess live output is not used on Windows")

    from swe.agents.tools import shell

    pid_file = tmp_path / "background.pid"
    background_pid: int | None = None
    try:
        returncode, stdout, stderr = await asyncio.wait_for(
            shell._execute_unix_subprocess(
                (
                    "sleep 30 >/dev/null 2>&1 & "
                    f"echo $! > {pid_file}; "
                    "echo done"
                ),
                tmp_path,
                5,
                os.environ.copy(),
            ),
            timeout=4,
        )

        background_pid = int(pid_file.read_text().strip())

        assert returncode == 0
        assert stdout == "done"
        assert stderr == ""
        with pytest.raises(ProcessLookupError):
            os.kill(background_pid, 0)
    finally:
        if background_pid is not None:
            try:
                os.killpg(os.getpgid(background_pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
