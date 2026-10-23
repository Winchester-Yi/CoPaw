# -*- coding: utf-8 -*-
"""Mandatory, execution-free inspection of visible pip installation commands.

This is an input policy, not a process sandbox. Script files and Python
program internals are deliberately outside its inspection boundary.
"""

from __future__ import annotations

import logging
import os
import re
import shlex

try:
    import tree_sitter_bash
    from tree_sitter import Language, Node, Parser
except ImportError:  # A broken runtime dependency must never allow execution.
    Parser = None

logger = logging.getLogger(__name__)

INSTALLATION_DENIED = (
    "平台策略禁止通过 shell 工具安装 Python 依赖，本次命令未执行。"
)
UNCHECKABLE_COMMAND = (
    "无法可靠检查该 shell 命令，本次命令未执行，请使用明确的命令写法重试。"
)
SHELL_TOOL_NAMES = frozenset(
    {"execute_shell_command", "start_background_process"},
)
_PIP_NAME = re.compile(r"pip(?:\d+(?:\.\d+)*)?(?:\.exe)?\Z")
_PYTHON_NAME = re.compile(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?\Z")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*=")
_SHELL_NAMES = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_UNSUPPORTED_SHELL_NAMES = frozenset(
    {
        "fish",
        "csh",
        "tcsh",
        "pwsh",
        "powershell",
        "cmd",
        "cmd.exe",
        "pwsh.exe",
        "powershell.exe",
    },
)
_PIP_VALUES = frozenset(
    {
        "--log",
        "--python",
        "--proxy",
        "--retries",
        "--timeout",
        "--exists-action",
        "--trusted-host",
        "--cert",
        "--client-cert",
        "--cache-dir",
        "--keyring-provider",
        "--use-feature",
        "--use-deprecated",
        "--resume-retries",
    },
)
_PIP_FLAGS = frozenset(
    {
        "--isolated",
        "--require-virtualenv",
        "--require-venv",
        "--no-input",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--no-color",
        "--no-python-version-warning",
        "--debug",
        "--help",
        "--version",
        "--verbose",
        "--quiet",
        "-h",
        "-V",
    },
)
_UV_VALUES = frozenset(
    {
        "--directory",
        "--project",
        "--config-file",
        "--cache-dir",
        "--color",
        "--default-index",
        "--index",
        "--index-url",
        "--extra-index-url",
        "--python",
        "--allow-insecure-host",
    },
)
_UV_FLAGS = frozenset(
    {
        "--no-config",
        "--no-cache",
        "--offline",
        "--native-tls",
        "--no-progress",
        "--quiet",
        "--verbose",
        "--help",
        "--version",
        "--no-python-downloads",
        "--managed-python",
        "--no-managed-python",
    },
)
_WRAPPER_OPTIONS = {
    "env": (
        {"-u", "--unset", "-C", "--chdir"},
        {"-i", "--ignore-environment", "-0", "--null"},
    ),
    "sudo": (
        {
            "-u",
            "--user",
            "-g",
            "--group",
            "-h",
            "--host",
            "-p",
            "--prompt",
            "-C",
            "-T",
            "-D",
            "-R",
        },
        {"-n", "-E", "-H", "-S", "-k", "-K", "-b", "-i", "-s"},
    ),
    "command": (set(), {"-p"}),
    "exec": ({"-a"}, {"-c", "-l"}),
    "builtin": (set(), set()),
    "time": (
        {"-f", "--format", "-o", "--output"},
        {"-p", "--portability", "-v", "--verbose", "-a", "--append", "-q"},
    ),
    "nohup": (set(), set()),
    "timeout": (
        {"-s", "--signal", "-k", "--kill-after"},
        {"--preserve-status", "--foreground", "-v", "--verbose"},
    ),
    "nice": ({"-n", "--adjustment"}, set()),
    "xargs": (
        {
            "-n",
            "--max-args",
            "-P",
            "--max-procs",
            "-L",
            "--max-lines",
            "-s",
            "--max-chars",
            "-d",
            "--delimiter",
            "-E",
            "--eof",
        },
        {
            "-0",
            "--null",
            "-r",
            "--no-run-if-empty",
            "-t",
            "--verbose",
            "-p",
            "--interactive",
            "-x",
            "--exit",
        },
    ),
}


class _Uncheckable(ValueError):
    """Input cannot be reliably interpreted without executing it."""


def _literal(node: Node) -> str | None:
    if node.type == "command_name":
        return _literal(node.named_children[0])
    if node.type not in {
        "word",
        "number",
        "raw_string",
        "string",
        "concatenation",
    }:
        return None
    if any(
        child.type
        not in {"string_content", "word", "number", "raw_string", "string"}
        or (child.type != "string_content" and _literal(child) is None)
        for child in node.named_children
    ):
        return None
    raw = node.text.decode("utf-8").replace("\\\n", "")
    # Unquoted glob/tilde expansion can change the executable or subcommand.
    if node.type == "word" and re.search(r"(?<!\\)[*?\[~]", raw):
        return None
    words = shlex.split(raw)
    return words[0] if len(words) == 1 else None


def _required(words: list[str | None], index: int) -> str:
    if index >= len(words):
        raise _Uncheckable()
    word = words[index]
    if word is None:
        raise _Uncheckable()
    return word


def _positional(words, values, flags) -> int:
    index = 0
    while index < len(words):
        token = _required(words, index)
        if token == "--":
            return index + 1
        if not token.startswith("-") or token == "-":
            return index
        option = token.split("=", 1)[0]
        if option in values:
            if "=" not in token:
                _required(words, index + 1)
                index += 1
        elif token not in flags and not re.fullmatch(r"-[vq]+", token):
            # Short options with attached values, such as sudo -uroot.
            if len(token) <= 2 or token[:2] not in values:
                raise _Uncheckable()
        index += 1
    return index


def _python_command(words: list[str | None]) -> list[str | None]:
    index = 0
    while index < len(words):
        token = _required(words, index)
        if token in {"-m", "-W", "-X"}:
            value = _required(words, index + 1)
            if token == "-m":
                return (
                    ["pip", *words[index + 2 :]]
                    if value in {"pip", "pip.__main__"}
                    else []
                )
            index += 2
            continue
        if not token.startswith("-") or token in {
            "--",
            "-c",
            "--help",
            "--version",
        }:
            return []  # Script/program internals are out of scope.
        if token.startswith(("-W", "-X")):
            index += 1
            continue
        short = token[1:]
        for offset, flag in enumerate(short):
            if flag == "c":
                return []
            if flag == "m":
                module = short[offset + 1 :] or _required(words, index + 1)
                start = index + (1 if short[offset + 1 :] else 2)
                return (
                    ["pip", *words[start:]]
                    if module in {"pip", "pip.__main__"}
                    else []
                )
            if flag not in "bBdEiIOPqRsSuvVh":
                raise _Uncheckable()
        index += 1
    return []


def _shell_code_position(words: list[str | None]) -> tuple[bool, int]:
    inline = False
    index = 0
    while index < len(words):
        token = _required(words, index)
        if token in {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}:
            _required(words, index + 1)
            index += 2
            continue
        if token in {"-", "--"}:
            return inline, index + 1
        if not token.startswith(("-", "+")):
            return inline, index
        if token not in {
            "--noprofile",
            "--norc",
            "--posix",
            "--restricted",
            "--verbose",
            "--login",
            "--debugger",
            "--noediting",
        }:
            if token.startswith("--") or set(token[1:]) - set(
                "abcefhiklmnprtuvxBCEHPT",
            ):
                raise _Uncheckable()
            inline = inline or (token.startswith("-") and "c" in token[1:])
        index += 1
    raise _Uncheckable()


def _nested_shell(words: list[str | None], depth: int) -> str | None:
    inline, index = _shell_code_position(words)
    command = _required(words, index)
    if inline:
        return _inspect(command, depth + 1)
    _check_script_path(command)
    return None  # A script file, not inline shell code.


def _check_script_path(path: str) -> None:
    if path == "/dev/stdin" or path.startswith(("/dev/fd/", "/proc/self/fd/")):
        raise _Uncheckable()


def _inline_builtin(executable: str, arguments, depth: int) -> str | None:
    if executable == "alias":
        # Creating aliases changes subsequent command identities. Queries do
        # not, but definitions need shell evaluation and are unverifiable.
        if any(arg is None or "=" in arg for arg in arguments):
            raise _Uncheckable()
        return None
    if executable in {"source", "."}:
        _check_script_path(_required(arguments, 0))
        return None
    if executable == "trap":
        if not arguments or arguments[0] in {"-p", "-l"}:
            return None
        index = 1 if arguments[0] == "--" else 0
        action = _required(arguments, index)
        return None if action == "-" else _inspect(action, depth + 1)
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    return _inspect(
        " ".join(_required(arguments, i) for i in range(len(arguments))),
        depth + 1,
    )


def _find_commands(words: list[str | None], depth: int) -> str | None:
    for index, token in enumerate(words):
        if token not in {"-exec", "-execdir", "-ok", "-okdir"}:
            continue
        end = index + 1
        while end < len(words) and words[end] not in {";", "+"}:
            end += 1
        reason = _classify(words[index + 1 : end], depth + 1)
        if reason:
            return reason
    return None


def _wrapped_command(executable: str, arguments, depth: int) -> str | None:
    if executable == "command" and arguments and arguments[0] in {"-v", "-V"}:
        return None
    values, flags = _WRAPPER_OPTIONS[executable]
    index = _positional(arguments, values, flags)
    if executable == "env":
        while index < len(arguments) and _ASSIGNMENT.match(
            _required(arguments, index),
        ):
            index += 1
    elif executable == "timeout":
        _required(arguments, index)
        index += 1
    if executable == "sudo" and index == len(arguments):
        raise _Uncheckable()
    return _classify(arguments[index:], depth + 1)


def _classify(words: list[str | None], depth: int) -> str | None:
    if depth > 16:
        raise _Uncheckable()
    if not words:
        return None
    executable = _required(words, 0).rsplit("/", 1)[-1]
    arguments = words[1:]
    if executable in _UNSUPPORTED_SHELL_NAMES:
        raise _Uncheckable()
    if executable in _WRAPPER_OPTIONS:
        return _wrapped_command(executable, arguments, depth)
    if _PYTHON_NAME.fullmatch(executable):
        return _classify(_python_command(arguments), depth + 1)
    if _PIP_NAME.fullmatch(executable):
        index = _positional(arguments, _PIP_VALUES, _PIP_FLAGS)
        if index < len(arguments) and _required(arguments, index) == "install":
            return INSTALLATION_DENIED
    elif executable == "uv":
        index = _positional(arguments, _UV_VALUES, _UV_FLAGS)
        if index < len(arguments) and _required(arguments, index) == "pip":
            return _classify(["pip", *arguments[index + 1 :]], depth + 1)
    elif executable in _SHELL_NAMES:
        return _nested_shell(arguments, depth)
    elif executable in {"eval", "trap", "source", ".", "alias"}:
        return _inline_builtin(executable, arguments, depth)
    elif executable == "find":
        return _find_commands(arguments, depth)
    return None


def _command_words(node: Node) -> list[str | None]:
    name = node.child_by_field_name("name")
    if name is None:
        raise _Uncheckable()
    arguments = list(node.children_by_field_name("argument"))
    parent = node.parent
    if (
        parent.type == "redirected_statement"
        and parent.child_by_field_name("body") == node
    ):
        for redirect in parent.children_by_field_name("redirect"):
            # The grammar can leave fd 0 as a numeric argument. Only an
            # unquoted number directly touching the operator is a descriptor.
            arguments = [
                arg
                for arg in arguments
                if not (
                    arg.type == "number"
                    and arg.end_byte == redirect.start_byte
                )
            ]
            # Bash grammar includes post-redirection arguments as extra
            # destinations; only the first destination belongs to the redirect.
            if redirect.type == "file_redirect":
                arguments.extend(
                    redirect.children_by_field_name("destination")[1:],
                )
            else:
                arguments.extend(redirect.children_by_field_name("argument"))
    arguments.sort(key=lambda arg: arg.start_byte)
    return [_literal(name), *(_literal(arg) for arg in arguments)]


def _inspect(command: str, depth: int = 0) -> str | None:
    if depth > 16 or len(command) > 262144 or "\0" in command:
        raise _Uncheckable()
    root = (
        Parser(Language(tree_sitter_bash.language()))
        .parse(command.encode("utf-8"))
        .root_node
    )
    if root.has_error:
        raise _Uncheckable()
    pending = [root]
    unverifiable = False
    while pending:
        node = pending.pop()
        pending.extend(reversed(node.named_children))
        if node.type != "command":
            continue
        try:
            reason = _classify(_command_words(node), depth)
            if reason:
                return reason
        except _Uncheckable:
            unverifiable = True
    if unverifiable:
        raise _Uncheckable()
    return None


def shell_installation_denial(command: str) -> str | None:
    """Return a mandatory denial reason, or None when inspection succeeds."""
    try:
        if os.name == "nt":
            # Bash syntax cannot reliably inspect cmd.exe / PowerShell input.
            raise _Uncheckable()
        return _inspect(command)
    except _Uncheckable:
        return UNCHECKABLE_COMMAND
    except Exception:
        logger.debug(
            "Shell installation policy could not inspect input",
            exc_info=True,
        )
        return UNCHECKABLE_COMMAND
