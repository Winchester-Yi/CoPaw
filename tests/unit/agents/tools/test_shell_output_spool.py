# -*- coding: utf-8 -*-
import os
import asyncio
import threading
import signal
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from swe.agents.tool_failure import ToolExecutionError


@pytest.mark.asyncio
async def test_spool_initialization_failure_does_not_start_command(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import bind_shell_output_spool

    (tmp_path / "tool_result").write_text("blocked directory")
    marker = tmp_path / "executed"
    with bind_shell_output_spool(tmp_path, 512):
        with pytest.raises(ToolExecutionError) as caught:
            await shell.execute_shell_command(f"touch {marker}")
    assert caught.value.error_type == "shell_output_storage_failed"
    assert "not started" in str(caught.value)
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("tail_failure", [False, True])
async def test_midstream_storage_failure_stops_process_without_full_read(
    monkeypatch,
    tmp_path,
    tail_failure,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools import shell_output_spool as module

    def fail_file(*_args, **_kwargs):
        raise OSError("disk full")

    def forbidden_read(_stream):
        pytest.fail("storage failure must not read complete output")

    original_create = shell.asyncio.create_subprocess_shell
    processes = []

    async def create(*args, **kwargs):
        proc = await original_create(*args, **kwargs)
        processes.append(proc)
        # Only execution-time spooling fails; initialization has completed.
        monkeypatch.setattr(module.tempfile, "TemporaryFile", fail_file)
        return proc

    monkeypatch.setattr(shell.asyncio, "create_subprocess_shell", create)
    monkeypatch.setattr(module._StreamBuffer, "read_all", forbidden_read)
    marker = tmp_path / "completed"
    output_bytes = 4096 if tail_failure else 262144
    command = (
        f"python -c \"import sys; sys.stdout.write('x' * {output_bytes}); sys.stdout.flush()\"; "
        f"sleep 20; touch {marker}"
    )
    with module.bind_shell_output_spool(tmp_path, 512):
        with pytest.raises(ToolExecutionError) as caught:
            await asyncio.wait_for(
                shell.execute_shell_command(
                    command,
                    timeout=1 if tail_failure else 30,
                ),
                7,
            )
    assert caught.value.error_type == "shell_output_storage_failed"
    assert processes[0].returncode is not None
    assert processes[0].stdout.at_eof()
    assert processes[0].stderr.at_eof()
    with pytest.raises(ProcessLookupError):
        os.killpg(processes[0].pid, 0)
    assert not marker.exists()
    assert len(str(caught.value).encode()) < 2048


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_storage_failure_cleanup(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools import shell_output_spool as module

    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    original_terminate = shell._terminate_unix_process_group
    original_create = shell.asyncio.create_subprocess_shell
    processes = []

    async def terminate(*args, **kwargs):
        cleanup_started.set()
        await allow_cleanup.wait()
        await original_terminate(*args, **kwargs)

    def fail_file(*_args, **_kwargs):
        raise OSError("disk full")

    async def create(*args, **kwargs):
        proc = await original_create(*args, **kwargs)
        processes.append(proc)
        monkeypatch.setattr(module.tempfile, "TemporaryFile", fail_file)
        return proc

    monkeypatch.setattr(shell, "_terminate_unix_process_group", terminate)
    monkeypatch.setattr(shell.asyncio, "create_subprocess_shell", create)
    with module.bind_shell_output_spool(tmp_path, 512):
        task = asyncio.create_task(
            shell.execute_shell_command(
                "python -c \"import sys; sys.stdout.write('x' * 262144); sys.stdout.flush()\"; sleep 20",
            ),
        )
        try:
            await asyncio.wait_for(cleanup_started.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            allow_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            with pytest.raises(ProcessLookupError):
                os.killpg(processes[0].pid, 0)
            assert processes[0].stdout.at_eof()
            assert processes[0].stderr.at_eof()
        finally:
            allow_cleanup.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 3])
async def test_command_artifact_failure_preserves_side_effects_and_exit(
    monkeypatch,
    tmp_path,
    returncode,
):
    from pathlib import Path
    from swe.agents.tools import shell_output_spool as module

    shell = _prepare(monkeypatch, tmp_path)
    original_open = Path.open

    def fail(path, mode="r", *args, **kwargs):
        if mode == "xb":
            raise OSError("disk full")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    with module.bind_shell_output_spool(tmp_path, 512):
        with pytest.raises(ToolExecutionError) as caught:
            await shell.execute_shell_command(
                f"echo once >> executed; python -c \"print('x' * 131072)\"; exit {returncode}",
            )
    assert caught.value.error_type == "shell_output_storage_failed"
    assert caught.value.returncode == returncode
    assert (tmp_path / "executed").read_text() == "once\n"
    assert len(str(caught.value).encode()) < 2048
    assert not list((tmp_path / "tool_result").glob("*.txt"))
    assert not list((tmp_path / "tool_result").glob("*.tmp"))


def test_failed_stream_refuses_more_bytes_without_recovering_disk(
    monkeypatch,
    tmp_path,
):
    from swe.agents.tools import shell_output_spool as module

    stream = module._StreamBuffer(tmp_path, 512)
    stream.append(b"prefix")

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module.tempfile, "TemporaryFile", fail)
    try:
        with pytest.raises(ToolExecutionError):
            stream.append(b"x" * 65536)
        size = stream.size
        for _ in range(3):
            with pytest.raises(ToolExecutionError):
                stream.append(b"y" * 65536)
        assert stream.size == size
        assert len(stream.buffer) <= 512
    finally:
        stream.close()


@pytest.mark.parametrize("returncode", [0, 3])
def test_final_artifact_failure_reports_exit_and_bounded_preview(
    monkeypatch,
    tmp_path,
    returncode,
):
    from pathlib import Path
    from swe.agents.tools import shell_output_spool as module
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    spool = module.ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    spool.streams["stdout"].append(b"preview-" + b"x" * 200000)
    original_open = Path.open

    def fail(path, mode="r", *args, **kwargs):
        if mode == "xb":
            raise OSError("disk full")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    monkeypatch.setattr(
        module._StreamBuffer,
        "read_all",
        lambda _: pytest.fail("full read"),
    )
    try:
        with pytest.raises(ToolExecutionError) as caught:
            spool.prepare_response(returncode, "", "", "")
        assert caught.value.error_type == "shell_output_storage_failed"
        text = str(caught.value)
        assert f"exit code {returncode}" in text
        assert "preview-" in text
        assert "Complete output is unavailable" in text
        assert "tool_result/" not in text
        assert len(text.encode()) < 2048
        assert not list(spool.directory.glob("*.txt"))
        assert not list(spool.directory.glob("*.tmp"))
    finally:
        for stream in spool.streams.values():
            stream.close()


def _prepare(monkeypatch, tmp_path):
    from swe.agents.tools import shell

    monkeypatch.setattr(
        shell,
        "prepare_shell_command",
        lambda command, cwd: SimpleNamespace(
            command=command,
            working_dir=tmp_path,
            env=os.environ.copy(),
            python_runtime_guard=nullcontext(),
        ),
    )
    monkeypatch.setattr(
        shell,
        "resolve_current_process_limit_policy",
        lambda _: SimpleNamespace(
            build_preexec_fn=lambda: None,
            enabled=False,
            shell_max_concurrent=None,
            diagnostic=None,
            should_enforce=False,
            should_enforce_memory_limit=False,
        ),
    )
    return shell


@pytest.mark.asyncio
async def test_pipe_reader_batches_spool_writes_and_flushes_tail(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import (
        ShellOutputSpool,
        bind_shell_output_spool,
    )

    batches = []
    original = ShellOutputSpool.append

    async def append(self, source, chunk):
        batches.append((source, chunk))
        await original(self, source, chunk)

    monkeypatch.setattr(ShellOutputSpool, "append", append)
    size = 256 * 1024 + 123
    with bind_shell_output_spool(tmp_path, 512):
        await shell.execute_shell_command(
            f"python -c \"import sys; sys.stdout.write('a' * {size})\"",
        )
    assert (
        b"".join(chunk for source, chunk in batches if source == "stdout")
        == b"a" * size
    )
    assert all(0 < len(chunk) <= 64 * 1024 for _, chunk in batches)
    assert len(batches) == 5
    assert (
        next((tmp_path / "tool_result").glob("*.txt")).read_text()
        == "a" * size
    )


@pytest.mark.asyncio
async def test_cancelled_reader_flushes_pending_live_output(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import (
        ShellOutputSpool,
        bind_shell_output_spool,
    )

    live = asyncio.Event()
    batches = []
    original = ShellOutputSpool.append

    async def append(self, source, chunk):
        batches.append((source, chunk))
        await original(self, source, chunk)

    async def emit(source, text):
        if source == "stdout" and text == "pending tail":
            live.set()

    monkeypatch.setattr(ShellOutputSpool, "append", append)
    monkeypatch.setattr(shell, "emit_tool_output_text", emit)
    with bind_shell_output_spool(tmp_path, 512):
        task = asyncio.create_task(
            shell.execute_shell_command("printf 'pending tail'; sleep 20"),
        )
        try:
            await asyncio.wait_for(live.wait(), 5)
            assert batches == []
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
    assert batches == [("stdout", b"pending tail")]


@pytest.mark.asyncio
async def test_cancelled_batch_preserves_chunk_remainder(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import ShellOutputSpool

    class Pipe:
        def __init__(self, chunks):
            self.chunks = iter(chunks)

        async def read(self, _size):
            return next(self.chunks, b"")

    async def wait():
        return 0

    async def create(*_args, **_kwargs):
        return SimpleNamespace(
            stdout=Pipe([b"a" * 3] + [b"b" * 4096] * 16),
            stderr=Pipe([]),
            returncode=0,
            pid=99999999,
            wait=wait,
        )

    async def terminate(*_args):
        pass

    monkeypatch.setattr(shell.asyncio, "create_subprocess_shell", create)
    monkeypatch.setattr(shell, "_terminate_unix_process_group", terminate)
    spool = ShellOutputSpool(
        tmp_path,
        512,
        shell.smart_decode,
        shell._format_shell_response,
    )
    started, release = threading.Event(), threading.Event()
    written = []
    original = spool.streams["stdout"].append

    def append(chunk):
        if not written:
            started.set()
            assert release.wait(5)
        written.append(chunk)
        original(chunk)

    monkeypatch.setattr(spool.streams["stdout"], "append", append)
    with spool.activate():
        task = asyncio.create_task(
            shell._execute_unix_subprocess("ignored", tmp_path, 5, {}),
        )
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await spool.aclose()
    assert b"".join(written) == b"a" * 3 + b"b" * (16 * 4096)


@pytest.mark.asyncio
async def test_large_shell_output_recovers_exact_formatted_artifact(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import bind_shell_output_spool

    with bind_shell_output_spool(tmp_path, 512):
        result = await shell.execute_shell_command(
            "printf 'head\\n'; python -c \"print('middle' * 50000)\"; "
            "printf 'tail\\n'; printf 'warning\\n' >&2",
        )
    text = result.content[0]["text"]
    assert len(text.encode()) <= 512
    assert "<<<TRUNCATED>>>" in text
    artifact = next((tmp_path / "tool_result").glob("*.txt"))
    assert artifact.read_text() == (
        "head\n" + "middle" * 50000 + "\ntail\n[stderr]\nwarning"
    )


@pytest.mark.asyncio
async def test_spool_buffers_are_bounded_and_failure_retains_original(
    tmp_path,
):
    from swe.agents.tools.shell_output_spool import ShellOutputSpool
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    spool = ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    try:
        for _ in range(128):
            await spool.append("stdout", b"a" * 4096)
        assert spool.buffered_bytes <= 512
        assert spool.total_bytes == 128 * 4096
        stdout, stderr = spool.decode_streams()
        text, _ = spool.prepare_response(0, stdout, stderr, "")
        assert len(text.encode()) <= 512
        artifact = next((tmp_path / "tool_result").glob("*.txt"))
        assert artifact.stat().st_size == 128 * 4096
    finally:
        await spool.aclose()


@pytest.mark.asyncio
async def test_small_output_and_unbound_output_stay_exact(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import bind_shell_output_spool

    with bind_shell_output_spool(tmp_path, 512):
        small = await shell.execute_shell_command("printf '\\nhello\\n'")
    assert small.content[0]["text"] == "hello"
    full = await shell.execute_shell_command("python -c \"print('a' * 4000)\"")
    assert full.content[0]["text"] == "a" * 4000
    assert not list((tmp_path / "tool_result").glob("*.txt"))


@pytest.mark.asyncio
async def test_spooled_failure_preserves_type_and_full_detail(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import bind_shell_output_spool

    with bind_shell_output_spool(tmp_path, 512):
        with pytest.raises(ToolExecutionError) as caught:
            await shell.execute_shell_command(
                "python -c \"import sys; sys.stderr.write('x' * 4094 + '"
                "Read timed out' + 'x' * 4096); sys.exit(1)\"",
            )
    assert caught.value.error_type == "tool_timeout"
    assert len(caught.value.detail.encode()) <= 512
    artifact = next((tmp_path / "tool_result").glob("*.txt"))
    assert artifact.read_text() == (
        "Command failed with exit code 1.\n[stderr]\n"
        + "x" * 4094
        + "Read timed out"
        + "x" * 4096
    )


@pytest.mark.asyncio
async def test_spooled_timeout_retains_output_and_timeout_suffix(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import bind_shell_output_spool

    with bind_shell_output_spool(tmp_path, 512):
        with pytest.raises(ToolExecutionError) as caught:
            await shell.execute_shell_command(
                "python -c \"print('a' * 4000)\"; sleep 20",
                timeout=0.2,
            )
    assert caught.value.error_type == "tool_timeout"
    artifact = next((tmp_path / "tool_result").glob("*.txt"))
    text = artifact.read_text()
    assert "a" * 4000 in text
    assert "TimeoutError" in text


@pytest.mark.asyncio
async def test_spool_handles_unicode_boundaries_and_newline_stripping(
    tmp_path,
):
    from swe.agents.tools.shell_output_spool import ShellOutputSpool
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    spool = ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    raw = ("\n" * 70000 + "中🙂" * 70000 + "\n" * 70000).encode()
    try:
        for start in range(0, len(raw), 4096):
            await spool.append("stdout", raw[start : start + 4096])
        stdout, stderr = spool.decode_streams()
        text, _ = spool.prepare_response(0, stdout, stderr, "")
        assert len(text.encode()) <= 512
        assert (
            next((tmp_path / "tool_result").glob("*.txt")).read_text()
            == "中🙂" * 70000
        )
    finally:
        await spool.aclose()


@pytest.mark.asyncio
async def test_artifact_failure_falls_back_to_full_output(
    monkeypatch,
    tmp_path,
):
    from swe.agents.tools.shell_output_spool import ShellOutputSpool
    from swe.agents.tools.shell import smart_decode, _format_shell_response
    from pathlib import Path

    spool = ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    try:
        await spool.append("stdout", b"a" * 10000)
        original_open = Path.open

        def fail_artifact(path, mode="r", *args, **kwargs):
            if mode == "xb":
                raise OSError("disk full")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_artifact)
        stdout, stderr = spool.decode_streams()
        with pytest.raises(ToolExecutionError) as caught:
            spool.prepare_response(0, stdout, stderr, "")
        assert caught.value.error_type == "shell_output_storage_failed"
        assert caught.value.returncode == 0
        assert len(str(caught.value).encode()) < 2048
    finally:
        await spool.aclose()


@pytest.mark.asyncio
async def test_cancelled_shell_kills_process_group(monkeypatch, tmp_path):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import bind_shell_output_spool

    pid_path = tmp_path / "shell.pid"
    with bind_shell_output_spool(tmp_path, 512):
        task = asyncio.create_task(
            shell.execute_shell_command(
                f"echo $$ > {pid_path}; python -c \"print('a' * 4000)\"; sleep 20",
            ),
        )
        while not pid_path.exists():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)
    assert not list((tmp_path / "tool_result").glob("*.tmp"))


@pytest.mark.asyncio
async def test_replaced_spool_directory_cannot_escape_workspace(tmp_path):
    from swe.agents.tools.shell_output_spool import ShellOutputSpool
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    spool = ShellOutputSpool(
        workspace,
        512,
        smart_decode,
        _format_shell_response,
    )
    (workspace / "tool_result").symlink_to(external, target_is_directory=True)
    try:
        with pytest.raises(ToolExecutionError):
            await spool.append("stdout", b"a" * 10000)
        assert spool.buffered_bytes <= 512
        with pytest.raises(ToolExecutionError):
            spool.decode_streams()
        assert not list(external.iterdir())
    finally:
        await spool.aclose()


@pytest.mark.asyncio
async def test_cancelled_append_finishes_before_cleanup(monkeypatch, tmp_path):
    from swe.agents.tools.shell_output_spool import ShellOutputSpool
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    spool = ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = spool.streams["stdout"].append

    def delayed(chunk):
        started.set()
        release.wait(5)
        original(chunk)
        finished.set()

    monkeypatch.setattr(spool.streams["stdout"], "append", delayed)
    task = asyncio.create_task(spool.append("stdout", b"a" * 4096))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.02)
    try:
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await spool.aclose()
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancelled_response_waits_for_artifact_worker(
    monkeypatch,
    tmp_path,
    repeat_cancel,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import (
        ShellOutputSpool,
        bind_shell_output_spool,
    )

    started = threading.Event()
    release = threading.Event()
    original = ShellOutputSpool._write_response

    def delayed(self, *args):
        started.set()
        release.wait(5)
        return original(self, *args)

    monkeypatch.setattr(ShellOutputSpool, "_write_response", delayed)
    with bind_shell_output_spool(tmp_path, 512):
        task = asyncio.create_task(
            shell.execute_shell_command(
                "python -c \"print('a' * 4000)\"",
            ),
        )
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.02)
        if repeat_cancel:
            task.cancel()
            await asyncio.sleep(0.02)
        try:
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert (
        next((tmp_path / "tool_result").glob("*.txt")).read_text()
        == "a" * 4000
    )


@pytest.mark.asyncio
async def test_permission_failure_marker_remains_case_sensitive(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import bind_shell_output_spool

    with bind_shell_output_spool(tmp_path, 512):
        with pytest.raises(ToolExecutionError) as caught:
            await shell.execute_shell_command(
                "python -c \"import sys; sys.stderr.write('x' * 4000 + '"
                "Outside the allowed workspace'); sys.exit(1)\"",
            )
    assert caught.value.error_type == "shell_command_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_mode", ["short", "raise"])
async def test_initial_prefix_write_failure_keeps_full_output(
    monkeypatch,
    tmp_path,
    fail_mode,
):
    from swe.agents.tools import shell_output_spool
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    original_file = shell_output_spool.tempfile.TemporaryFile

    class ShortFile:
        def __init__(self, *args, **kwargs):
            self.file = original_file(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, data):
            self.file.write(data[:2])
            if fail_mode == "raise":
                raise OSError("disk full")
            return 2

    monkeypatch.setattr(
        shell_output_spool.tempfile,
        "TemporaryFile",
        ShortFile,
    )
    spool = shell_output_spool.ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    try:
        await spool.append("stdout", b"prefix")
        with pytest.raises(ToolExecutionError):
            await spool.append("stdout", b"a" * 4096)
        with pytest.raises(ToolExecutionError):
            await spool.append("stdout", b"suffix")
        assert spool.buffered_bytes <= 512
        error = spool.storage_failure("write", -15)
        assert "prefix" in str(error)
        assert "suffix" not in str(error)
    finally:
        await spool.aclose()


@pytest.mark.asyncio
async def test_repeated_cancel_kills_sigterm_ignoring_process_group(
    monkeypatch,
    tmp_path,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import bind_shell_output_spool

    pid_path = tmp_path / "ignoring-shell.pid"
    pid = None
    with bind_shell_output_spool(tmp_path, 512):
        task = asyncio.create_task(
            shell.execute_shell_command(
                f"trap '' TERM; echo $$ > {pid_path}; exec sleep 20",
            ),
        )
        while not pid_path.exists():
            await asyncio.sleep(0.01)
        pid = int(pid_path.read_text())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            with pytest.raises(ProcessLookupError):
                os.killpg(pid, 0)
        finally:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_cancelled_close_closes_both_spools(monkeypatch, tmp_path):
    from swe.agents.tools.shell_output_spool import ShellOutputSpool
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    spool = ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    await spool.append("stdout", b"a" * 4096)
    await spool.append("stderr", b"b" * 4096)
    started = threading.Event()
    release = threading.Event()
    original = spool.streams["stdout"].close

    def delayed():
        started.set()
        release.wait(5)
        original()

    monkeypatch.setattr(spool.streams["stdout"], "close", delayed)
    task = asyncio.create_task(spool.aclose())
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        assert all(stream.file is None for stream in spool.streams.values())
    finally:
        await spool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("command_timeout", [False, True])
async def test_shell_drains_slow_spool_writes_before_formatting(
    monkeypatch,
    tmp_path,
    command_timeout,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools import shell_output_spool

    original_append = shell_output_spool._StreamBuffer.append
    original_wait = shell._wait_for_unix_process_exit
    started = threading.Event()
    release = threading.Event()
    draining = asyncio.Event()

    def delayed_append(self, chunk):
        if self.size == 0:
            started.set()
            assert release.wait(5)
        original_append(self, chunk)

    async def wait_for_exit(proc, timeout):
        await original_wait(proc, timeout)
        draining.set()

    monkeypatch.setattr(
        shell_output_spool._StreamBuffer,
        "append",
        delayed_append,
    )
    monkeypatch.setattr(shell, "_wait_for_unix_process_exit", wait_for_exit)
    command = "python -c \"import sys; sys.stdout.write('a' * 32768)\""
    if command_timeout:
        command += "; sleep 20"
    with shell_output_spool.bind_shell_output_spool(tmp_path, 512):
        task = asyncio.create_task(
            shell.execute_shell_command(command, timeout=1),
        )
        try:
            deadline = time.monotonic() + 5
            while not started.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.001)
            if not command_timeout:
                await asyncio.wait_for(draining.wait(), 5)
            # Exceed the old one-second pipe-drain limit while a write is live.
            await asyncio.sleep(2.1 if command_timeout else 1.1)
        finally:
            release.set()
        if command_timeout:
            with pytest.raises(ToolExecutionError) as caught:
                await task
            assert "TimeoutError" in str(caught.value)
        else:
            await task
    artifact = next((tmp_path / "tool_result").glob("*.txt"))
    text = artifact.read_text()
    assert text.count("a") >= 32768
    assert "a" * 32768 in text


@pytest.mark.asyncio
async def test_replaced_spool_directory_returns_full_output(tmp_path):
    from swe.agents.tools.shell_output_spool import ShellOutputSpool
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    spool = ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    try:
        await spool.append("stdout", b"a" * 32768)
        await spool.append("stderr", b"warning")
        stdout, stderr = spool.decode_streams("timeout diagnostic")
        spool.directory.rename(tmp_path / "old_tool_result")
        spool.directory.write_text("replacement")
        with pytest.raises(ToolExecutionError) as caught:
            spool.prepare_response(0, stdout, stderr, "\nprocess diagnostic")
        assert caught.value.returncode == 0
        assert "warning" in str(caught.value)
        assert len(str(caught.value).encode()) < 2048
        assert spool.directory.read_text() == "replacement"
    finally:
        await spool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("command_timeout", [False, True])
async def test_spool_drain_bounds_wait_for_inherited_pipe(
    monkeypatch,
    tmp_path,
    command_timeout,
):
    shell = _prepare(monkeypatch, tmp_path)
    from swe.agents.tools.shell_output_spool import bind_shell_output_spool

    stdout = asyncio.StreamReader()
    stdout.feed_data(b"received output")
    stderr = asyncio.StreamReader()
    stderr.feed_eof()

    async def process_wait():
        return 0

    async def create_process(*_args, **_kwargs):
        return SimpleNamespace(
            stdout=stdout,
            stderr=stderr,
            returncode=0,
            pid=99999999,
            wait=process_wait,
        )

    async def wait_for_exit(*_args, **_kwargs):
        if command_timeout:
            raise asyncio.TimeoutError

    async def terminate(*_args, **_kwargs):
        pass

    monkeypatch.setattr(
        shell.asyncio,
        "create_subprocess_shell",
        create_process,
    )
    monkeypatch.setattr(shell, "_wait_for_unix_process_exit", wait_for_exit)
    monkeypatch.setattr(shell, "_terminate_unix_process_group", terminate)
    loop = asyncio.get_running_loop()
    # Model a detached child holding the pipe, with finite test cleanup.
    close_pipe = loop.call_later(2.5, stdout.feed_eof)
    started = loop.time()
    try:
        with bind_shell_output_spool(tmp_path, 512):
            if command_timeout:
                with pytest.raises(ToolExecutionError) as caught:
                    await shell.execute_shell_command("ignored", timeout=1)
                assert "received output" in str(caught.value)
            else:
                result = await shell.execute_shell_command("ignored")
                assert result.content[0]["text"] == "received output"
        assert loop.time() - started < 2
    finally:
        close_pipe.cancel()
        stdout.feed_eof()


@pytest.mark.parametrize(
    "raw",
    [b"\n" * 200000, b"\n" * 70000 + b"hello" * 20000 + b"\n" * 70000],
    ids=["all-newlines", "trimmed"],
)
def test_finalization_locates_each_stream_bounds_once(
    monkeypatch,
    tmp_path,
    raw,
):
    from swe.agents.tools import shell_output_spool as module
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    spool = module.ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    calls = []
    original = module._stream_bounds

    def bounds(reader):
        calls.append(reader)
        return original(reader)

    monkeypatch.setattr(module, "_stream_bounds", bounds)
    try:
        spool.streams["stdout"].append(raw)
        stdout, stderr = spool.decode_streams()
        text, _ = spool.prepare_response(0, stdout, stderr, "")
        artifacts = list((tmp_path / "tool_result").glob("*.txt"))
        actual = artifacts[0].read_text() if artifacts else text
        assert actual == _format_shell_response(0, smart_decode(raw), "")
        assert len(calls) == 2
    finally:
        for stream in spool.streams.values():
            stream.close()


def test_valid_utf8_finalization_uses_bounded_decode_passes(
    monkeypatch,
    tmp_path,
):
    from swe.agents.tools import shell_output_spool as module
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    raw = b"a" * (module._COPY_BYTES - 1) + "中🙂".encode() * 20000
    spool = module.ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    read_bytes = 0
    original = module._read_chunks

    def chunks(reader, bounds):
        nonlocal read_bytes
        for chunk in original(reader, bounds):
            read_bytes += len(chunk)
            yield chunk

    monkeypatch.setattr(module, "_read_chunks", chunks)
    try:
        spool.streams["stdout"].append(raw)
        stdout, stderr = spool.decode_streams()
        spool.prepare_response(0, stdout, stderr, "")
        assert (
            next((tmp_path / "tool_result").glob("*.txt")).read_bytes() == raw
        )
        assert read_bytes == 2 * len(raw)
    finally:
        for stream in spool.streams.values():
            stream.close()


@pytest.mark.parametrize("position", ["start", "middle", "end", "incomplete"])
@pytest.mark.parametrize("source", ["stdout", "stderr"])
def test_invalid_utf8_finalization_preserves_whole_locale_decode(
    monkeypatch,
    tmp_path,
    position,
    source,
):
    from swe.agents.tools import shell_output_spool as module
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    monkeypatch.setattr(
        module.locale,
        "getpreferredencoding",
        lambda _: "cp1252",
    )
    prefix = "é".encode() + b"a" * 70000
    suffix = b"z" * 70000
    invalid = b"\xe2\x82" if position == "incomplete" else b"\xff"
    raw = {
        "start": invalid + prefix + suffix,
        "middle": prefix + invalid + suffix,
        "end": prefix + suffix + invalid,
        "incomplete": prefix + suffix + invalid,
    }[position]
    spool = module.ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    try:
        spool.streams[source].append(b"\n\n" + raw + b"\n\n")
        spool.streams["stderr" if source == "stdout" else "stdout"].append(
            b"other stream",
        )
        stdout, stderr = spool.decode_streams("TimeoutError suffix")
        display, markers = spool.prepare_response(
            9,
            stdout,
            stderr,
            "\ndiagnostic",
        )
        expected = (
            _format_shell_response(
                9,
                smart_decode(spool.streams["stdout"].read_all()),
                smart_decode(spool.streams["stderr"].read_all())
                + "\nTimeoutError suffix",
            )
            + "\ndiagnostic"
        )
        assert (
            next((tmp_path / "tool_result").glob("*.txt")).read_bytes()
            == expected.encode()
        )
        assert len(display.encode()) <= 512
        assert "TimeoutError suffix" in markers
        assert not list((tmp_path / "tool_result").glob("*.tmp"))
    finally:
        for stream in spool.streams.values():
            stream.close()


def test_whole_locale_decode_preserves_exact_stderr_markers(
    monkeypatch,
    tmp_path,
):
    from swe.agents.tools import shell_output_spool as module
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    monkeypatch.setattr(
        module.locale,
        "getpreferredencoding",
        lambda _: "utf-16-le",
    )
    raw = b"Killed Read timed out" + b"x" * 140000 + b"\xff\xff"
    spool = module.ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    try:
        spool.streams["stderr"].append(raw)
        stdout, stderr = spool.decode_streams()
        _, markers = spool.prepare_response(1, stdout, stderr, "")
        assert markers == ""
        expected = _format_shell_response(1, "", smart_decode(raw)).encode()
        assert (
            next((tmp_path / "tool_result").glob("*.txt")).read_bytes()
            == expected
        )
    finally:
        for stream in spool.streams.values():
            stream.close()


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("Read timed out", "read timed out"),
        ("MemoryError", "MemoryError"),
        ("outside the allowed workspace", "outside the allowed workspace"),
        ("Outside the allowed workspace", ""),
    ],
)
def test_finalization_scans_markers_across_decode_chunks(
    monkeypatch,
    tmp_path,
    marker,
    expected,
):
    from swe.agents.tools import shell_output_spool as module
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    monkeypatch.setattr(
        module.locale,
        "getpreferredencoding",
        lambda _: "cp1252",
    )
    raw = (
        b"x" * (module._COPY_BYTES - 3)
        + marker.encode()
        + b"x" * 70000
        + b"\xff"
    )
    spool = module.ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    try:
        spool.streams["stderr"].append(raw)
        stdout, stderr = spool.decode_streams()
        _, markers = spool.prepare_response(1, stdout, stderr, "")
        assert markers == expected
        assert next((tmp_path / "tool_result").glob("*.txt")).read_bytes() == (
            _format_shell_response(1, "", smart_decode(raw)).encode()
        )
    finally:
        for stream in spool.streams.values():
            stream.close()


@pytest.mark.parametrize("stage", ["fsync", "rename"])
def test_finalization_durability_failure_retains_full_response(
    monkeypatch,
    tmp_path,
    stage,
):
    from pathlib import Path
    from swe.agents.tools import shell_output_spool as module
    from swe.agents.tools.shell import smart_decode, _format_shell_response

    def fail(*_args):
        raise OSError("durability failure")

    if stage == "fsync":
        monkeypatch.setattr(module.os, "fsync", fail)
    else:
        monkeypatch.setattr(Path, "replace", fail)
    spool = module.ShellOutputSpool(
        tmp_path,
        512,
        smart_decode,
        _format_shell_response,
    )
    raw = b"a" * 140000
    try:
        spool.streams["stdout"].append(raw)
        spool.streams["stderr"].append(b"warning")
        stdout, stderr = spool.decode_streams("TimeoutError suffix")
        with pytest.raises(ToolExecutionError) as caught:
            spool.prepare_response(3, stdout, stderr, "\ndiagnostic")
        assert caught.value.error_type == "shell_output_storage_failed"
        assert caught.value.returncode == 3
        assert "warning" in str(caught.value)
        assert len(str(caught.value).encode()) < 2048
        assert not list((tmp_path / "tool_result").glob("*.txt"))
        assert not list((tmp_path / "tool_result").glob("*.tmp"))
    finally:
        for stream in spool.streams.values():
            stream.close()
