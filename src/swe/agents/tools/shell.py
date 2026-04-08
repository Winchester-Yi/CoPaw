# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long
"""The shell command tool with tenant path boundary enforcement."""

import asyncio
import locale
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...security.tenant_path_boundary import (
    is_path_within_tenant_with_base,
    get_current_tenant_root,
    TenantPathBoundaryError,
)


# Commands that take string arguments which may look like paths
# but should NOT be treated as file paths
_STRING_ARG_COMMANDS = frozenset({
    "echo", "/bin/echo", "/usr/bin/echo",
    "printf", "/usr/bin/printf",
})

# Interpreter commands that have dangerous -c/-e code execution flags
# Only for these commands do we reject -c/-e flags
_INTERPRETER_COMMANDS = frozenset({
    # Python code execution temporarily allowed
    # "python", "python3", "python2",
    # "/usr/bin/python", "/usr/bin/python3", "/usr/bin/python2",
    # "/usr/local/bin/python", "/usr/local/bin/python3",
    "node", "/usr/bin/node", "/usr/local/bin/node",
    "nodejs", "/usr/bin/nodejs",
    "ruby", "/usr/bin/ruby", "/usr/local/bin/ruby",
    "perl", "/usr/bin/perl", "/usr/local/bin/perl",
    "bash", "/bin/bash", "/usr/bin/bash",
    "sh", "/bin/sh", "/usr/bin/sh",
    "zsh", "/bin/zsh", "/usr/bin/zsh",
    "ksh", "/bin/ksh", "/usr/bin/ksh",
    "dash", "/bin/dash", "/usr/bin/dash",
})


def _is_path_like(token: str) -> bool:
    """Check if a token looks like a file path.

    Args:
        token: The token to check.

    Returns:
        True if the token looks like a path (starts with /, ./, ../, or ~).
    """
    return token.startswith(('/', './', '../', '~'))


def _has_code_exec_flag(token: str) -> bool:
    """Check if a token contains code execution flags (-c or -e).

    Handles both standalone flags (-c, -e) and combined flags (-lc, -ec, -ce).
    Only checks the flag part, not whether the command is an interpreter.

    Args:
        token: The token to check (e.g., "-c", "-lc", "--eval").

    Returns:
        True if the token contains -c or -e as code execution flags.
    """
    # Long-form flags that are always code execution
    if token in ('--eval', '--exec', '--command'):
        return True

    # Short-form flags: -c, -e, or combined like -lc, -ec, -ce
    if token.startswith('-') and len(token) > 1:
        # Check if 'c' or 'e' appears in the combined flag
        # But exclude special cases like --option (already handled above)
        flag_body = token[1:]  # Remove leading -
        if 'c' in flag_body or 'e' in flag_body:
            return True

    return False



def _extract_path_tokens(command: str) -> tuple[list[str], bool]:
    """Extract path tokens from shell command.

    Implements a "path-first" validation strategy:
    - Any token that looks like a path (/..., ./..., ../..., ~...) is validated
    - Only exempt: echo/printf commands (their non-flag args are treated as strings)
    - Interpreter commands with -c/-e flags are flagged for rejection

    Args:
        command: The shell command string.

    Returns:
        Tuple of (file_paths, has_code_exec) where:
        - file_paths: List of explicit file path tokens found
        - has_code_exec: True if interpreter command contains code execution flags
    """
    file_paths = []
    has_code_exec = False

    # Split command into tokens for better parsing
    try:
        tokens = shlex.split(command)
    except ValueError:
        # If shlex fails, fall back to simple parsing
        tokens = command.split()

    if not tokens:
        return file_paths, has_code_exec

    # Check command type
    cmd_name = tokens[0]
    is_exempt_cmd = cmd_name in _STRING_ARG_COMMANDS
    is_interpreter = cmd_name in _INTERPRETER_COMMANDS

    i = 0
    while i < len(tokens):
        token = tokens[i]

        # Check for code execution flags - but ONLY for interpreter commands
        if is_interpreter and _has_code_exec_flag(token):
            has_code_exec = True
            i += 1
            continue

        # Check for path-like tokens
        if _is_path_like(token):
            # For exempt commands (echo/printf), only treat as path if not
            # preceded by a flag (to handle: echo -n "/etc/hosts")
            if is_exempt_cmd:
                if i > 0:
                    prev = tokens[i - 1]
                    if prev.startswith('-'):
                        # This is likely a flag argument, skip
                        pass
                    else:
                        file_paths.append(token)
                else:
                    # First token after command name - for echo/printf this is text
                    pass
            else:
                # Non-exempt command: any path-like token is a file path
                file_paths.append(token)

        i += 1

    return file_paths, has_code_exec


def _validate_shell_paths(command: str, base_dir: Path) -> Optional[str]:
    """Validate that all explicit file paths in the command are within tenant boundary.

    Args:
        command: The shell command to validate.
        base_dir: The base directory for resolving relative paths (typically the cwd).

    Returns:
        Error message if any path escapes the tenant boundary, None otherwise.
    """
    file_paths, has_code_exec = _extract_path_tokens(command)

    # Reject commands with code execution flags (-c, -e, etc.)
    if has_code_exec:
        return (
            "Error: Shell commands with code execution flags (-c, -e, etc.) "
            "are not allowed for security reasons."
        )

    for token in file_paths:
        # Skip checking if it's clearly not a path
        if not token or token in (".", ".."):
            continue

        # Check if the path is within tenant boundary, using base_dir for relative paths
        if not is_path_within_tenant_with_base(token, base_dir=base_dir):
            return (
                f"Error: Shell command contains path outside the allowed workspace: "
                f"'{token}'"
            )

    return None


def _resolve_cwd(cwd: Optional[Path]) -> Path:
    """Resolve and validate the working directory against tenant boundary.

    Args:
        cwd: The requested working directory, or None to use tenant root.

    Returns:
        The resolved working directory path.

    Raises:
        TenantPathBoundaryError: If the cwd is outside the tenant workspace
                                 or tenant context is missing.
    """
    tenant_root = get_current_tenant_root()

    if cwd is None:
        return tenant_root

    # Resolve the cwd and validate it's within tenant root
    resolved_cwd = cwd.resolve()
    try:
        resolved_cwd.relative_to(tenant_root.resolve())
    except ValueError:
        raise TenantPathBoundaryError(
            f"Working directory '{cwd}' is outside the tenant workspace boundary.",
            resolved_path=resolved_cwd,
        )

    return resolved_cwd


def _kill_process_tree_win32(pid: int) -> None:
    """Kill a process and all its descendants on Windows via taskkill.

    Uses ``taskkill /F /T`` which forcefully terminates the entire process
    tree, including grandchild processes that ``Popen.kill()`` would miss.
    """
    try:
        subprocess.call(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass


def _collapse_embedded_newlines(cmd: str) -> str:
    r"""Replace embedded newline characters with spaces in a command string.

    LLMs produce tool-call arguments in JSON where ``\n`` is parsed as an
    actual newline character.  In the original shell command the user
    intended the *literal* two-character sequence ``\n`` (e.g. inside a
    ``--content`` flag), but after JSON decoding it becomes a real line
    break.  When passed to a shell:

    * **Windows** ``cmd.exe`` truncates the command at the first newline.
    * **Unix** ``sh -c`` treats an unquoted newline as a command separator,
      so only the first "line" is executed with its arguments.

    Collapsing these newlines to spaces is a safe default because:

    1. For the bug case (JSON artefact) it prevents truncation.
    2. For intentional multi-line scripts on Windows the ``cmd /D /S /C``
       wrapper *already* breaks at newlines, so this is no worse.
    3. On Unix, callers should prefer ``&&`` / ``;`` over raw newlines for
       multi-command sequences; a stray newline inside an argument is
      almost certainly a JSON artefact.
    """
    if "\n" not in cmd:
        return cmd
    return cmd.replace("\r\n", " ").replace("\n", " ")


def _sanitize_win_cmd(cmd: str) -> str:
    """Fix common LLM escaping artefacts for Windows ``cmd.exe``.

    LLMs sometimes produce commands with backslash-escaped double quotes
    (``\\"``) — valid in bash/JSON but meaningless to ``cmd.exe``.  When
    *every* double-quote in the command is preceded by a backslash, it is
    almost certainly a double-escape artefact, so we strip them.
    """
    if '\\"' in cmd and '"' not in cmd.replace('\\"', ""):
        return cmd.replace('\\"', '"')
    return cmd


def _read_temp_file(path: str) -> str:
    """Read a temporary output file and return its decoded content."""
    try:
        with open(path, "rb") as f:
            return smart_decode(f.read())
    except OSError:
        return ""


# pylint: disable=too-many-branches, too-many-statements
def _execute_subprocess_sync(
    cmd: str,
    cwd: str,
    timeout: int,
    env: dict | None = None,
) -> tuple[int, str, str]:
    """Execute subprocess synchronously in a thread.

    This function runs in a separate thread to avoid Windows asyncio
    subprocess limitations.

    stdout/stderr are redirected to temporary files instead of pipes.
    On Windows, child processes inherit pipe handles and keep them open
    even after the parent exits, which causes ``communicate()`` to block
    until *all* holders close (e.g. a Chrome process launched via
    ``Start-Process``).  With temp-file redirection, ``proc.wait()``
    only waits for the direct child (``cmd.exe``) to exit, so commands
    that spawn background processes return immediately.

    .. note::

       Callers must pre-process *cmd* through
       :func:`_collapse_embedded_newlines` before passing it here.
       ``execute_shell_command`` already does this.

    Args:
        cmd (`str`):
            The shell command to execute (must not contain embedded
            newlines — see note above).
        cwd (`str`):
            The working directory for the command execution.
        timeout (`int`):
            The maximum time (in seconds) allowed for the command to run.
        env (`dict | None`):
            Environment variables for the subprocess.

    Returns:
        `tuple[int, str, str]`:
            A tuple containing the return code, standard output, and
            standard error of the executed command. If timeout occurs, the
            return code will be -1 and stderr will contain timeout information.
    """
    stdout_path: str | None = None
    stderr_path: str | None = None
    stdout_file = None
    stderr_file = None

    try:
        cmd = _sanitize_win_cmd(cmd)
        wrapped = f'cmd /D /S /C "{cmd}"'

        stdout_fd, stdout_path = tempfile.mkstemp(prefix="swe_out_")
        stderr_fd, stderr_path = tempfile.mkstemp(prefix="swe_err_")
        stdout_file = os.fdopen(stdout_fd, "wb")
        stderr_file = os.fdopen(stderr_fd, "wb")

        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            wrapped,
            shell=False,
            stdout=stdout_file,
            stderr=stderr_file,
            text=False,
            cwd=cwd,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        # Parent copies are no longer needed — the child inherited its own
        # handles via CreateProcess.  Closing here avoids holding the files
        # open longer than necessary.
        stdout_file.close()
        stdout_file = None
        stderr_file.close()
        stderr_file = None

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree_win32(proc.pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

        stdout_str = _read_temp_file(stdout_path)
        stderr_str = _read_temp_file(stderr_path)

        if timed_out:
            timeout_msg = (
                f"Command execution exceeded the timeout of {timeout} seconds."
            )
            if stderr_str:
                stderr_str = f"{stderr_str}\n{timeout_msg}"
            else:
                stderr_str = timeout_msg
            return -1, stdout_str, stderr_str

        returncode = proc.returncode if proc.returncode is not None else -1
        return returncode, stdout_str, stderr_str

    except Exception as e:
        return -1, "", str(e)
    finally:
        for f in (stdout_file, stderr_file):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        for path in (stdout_path, stderr_path):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# pylint: disable=too-many-branches, too-many-statements
async def execute_shell_command(
    command: str,
    timeout: int = 60,
    cwd: Optional[Path] = None,
) -> ToolResponse:
    """Execute a shell command and return its output.

    Platform shells: Windows uses cmd.exe; Linux/macOS use /bin/sh or /bin/bash.

    IMPORTANT: Always consider the operating system before choosing commands.

    Args:
        command (`str`):
            The shell command to execute.
        timeout (`int`, defaults to `60`):
            The maximum time (in seconds) allowed for the command to run.
            Default is 60 seconds.
        cwd (`Optional[Path]`, defaults to `None`):
            The working directory for the command execution.
            If None, defaults to the current tenant workspace.

    Returns:
        `ToolResponse`:
            The tool response containing the return code, standard output, and
            standard error of the executed command. If timeout occurs, the
            return code will be -1 and stderr will contain timeout information.
    """

    cmd = _collapse_embedded_newlines((command or "").strip())

    # Validate and resolve the working directory against tenant boundary
    try:
        working_dir = _resolve_cwd(cwd)
    except TenantPathBoundaryError as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: {e}",
                ),
            ],
        )

    # Validate explicit path tokens in the command, using working_dir as base for relative paths
    path_error = _validate_shell_paths(cmd, base_dir=working_dir)
    if path_error:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=path_error,
                ),
            ],
        )

    # Ensure the venv Python is on PATH for subprocesses
    env = os.environ.copy()
    python_bin_dir = str(Path(sys.executable).parent)
    existing_path = env.get("PATH", "")
    if existing_path:
        env["PATH"] = python_bin_dir + os.pathsep + existing_path
    else:
        env["PATH"] = python_bin_dir

    try:
        if sys.platform == "win32":
            # Windows: use thread pool to avoid asyncio subprocess limitations
            returncode, stdout_str, stderr_str = await asyncio.to_thread(
                _execute_subprocess_sync,
                cmd,
                str(working_dir),
                timeout,
                env,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                bufsize=0,
                cwd=str(working_dir),
                env=env,
                start_new_session=True,
            )

            try:
                # Apply timeout to communicate directly; wait()+communicate()
                # can hang if descendants keep stdout/stderr pipes open.
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
                stdout_str = smart_decode(stdout)
                stderr_str = smart_decode(stderr)
                returncode = proc.returncode

            except asyncio.TimeoutError:
                stderr_suffix = (
                    f"⚠️ TimeoutError: The command execution exceeded "
                    f"the timeout of {timeout} seconds. "
                    f"Please consider increasing the timeout value if this command "
                    f"requires more time to complete."
                )
                returncode = -1
                try:
                    # Kill the entire process group so that child processes
                    # spawned by the shell are also terminated.
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        os.killpg(pgid, signal.SIGKILL)
                        await asyncio.wait_for(proc.wait(), timeout=2)

                    # Drain remaining output.
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            proc.communicate(),
                            timeout=1,
                        )
                    except asyncio.TimeoutError:
                        stdout, stderr = b"", b""
                    stdout_str = smart_decode(stdout)
                    stderr_str = smart_decode(stderr)
                    if stderr_str:
                        stderr_str += f"\n{stderr_suffix}"
                    else:
                        stderr_str = stderr_suffix
                except (ProcessLookupError, OSError):
                    # Process already gone or pgid lookup failed — fall back
                    # to direct kill on the process itself.
                    try:
                        proc.kill()
                        await proc.wait()
                    except (ProcessLookupError, OSError):
                        pass
                    stdout_str = ""
                    stderr_str = stderr_suffix

        if returncode == 0:
            if stdout_str:
                response_text = stdout_str
            else:
                response_text = "Command executed successfully (no output)."
            if stderr_str:
                response_text += f"\n[stderr]\n{stderr_str}"
        else:
            response_parts = [f"Command failed with exit code {returncode}."]
            if stdout_str:
                response_parts.append(f"\n[stdout]\n{stdout_str}")
            if stderr_str:
                response_parts.append(f"\n[stderr]\n{stderr_str}")
            response_text = "".join(response_parts)

        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=response_text,
                ),
            ],
        )

    except Exception as e:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: Shell command execution failed due to \n{e}",
                ),
            ],
        )


def smart_decode(data: bytes) -> str:
    try:
        decoded_str = data.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        decoded_str = data.decode(encoding, errors="replace")

    return decoded_str.strip("\n")
