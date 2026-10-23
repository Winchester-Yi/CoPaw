# -*- coding: utf-8 -*-
"""Execution-free checks for the platform's Python installation policy."""

import pytest


@pytest.mark.parametrize(
    "command",
    [
        "pip install requests",
        "pip3 install -r requirements.txt",
        "pip3.11 install --upgrade requests",
        "/venv/bin/pip install -e .",
        "python -m pip install requests",
        "python3.11 -W ignore -m pip install requests",
        "/venv/bin/python -Im pip install requests",
        "uv pip install requests",
        "uv --directory . pip install requests",
        "pip --disable-pip-version-check install requests",
        "pip --log output.log install requests",
        "pip --python /venv/bin/python install requests",
        "X=1 pip install requests",
        "env X=1 pip install requests",
        "env -u X pip install requests",
        "sudo -u root pip install requests",
        "command -- pip install requests",
        "exec pip install requests",
        "nohup pip install requests",
        "timeout -s TERM 5 pip install requests",
        "nice -n 5 pip install requests",
        "p'ip' ins\\tall requests",
        "mkdir output && pip install requests && python main.py",
        "false && pip install requests",
        "echo hello; pip install requests",
        "echo hello\npip install requests",
        "pip install requests | cat",
        "if false; then pip install requests; fi",
        "for x in a b; do pip install requests; done",
        "f() { pip install requests; }; f",
        "bash -c 'pip install requests'",
        "sh -ec 'pip install requests'",
        "eval 'pip install requests'",
        "eval -- 'pip install requests'",
        "builtin eval -- 'pip install requests'",
        'echo "$(pip install requests)"',
        "echo `pip install requests`",
        "cat <(pip install requests)",
        "echo hello > $(pip install requests)",
        "cat <<EOF\n$(pip install requests)\nEOF",
        "xargs pip install < packages.txt",
        "find . -exec pip install requests ';'",
        "pip < /dev/null install requests",
        "python < /dev/null -m pip install requests",
        "python 0<&0 -m pip install requests",
        "pip 2>log install requests",
        "pip 2>&1 install requests",
        "pip >log install requests 2>err",
        "pip <<EOF install requests\nhello\nEOF",
        "pip <<<hello install requests",
        "bash -c -- 'pip install requests'",
        "bash -c - 'pip install requests'",
        "bash -c -e 'pip install requests'",
        "bash -c -o errexit 'pip install requests'",
        "bash -ec -- 'pip install requests'",
        "sh -c -e 'pip install requests'",
        "time pip install requests",
        "/usr/bin/time -f '%e' pip install requests",
        "builtin command pip install requests",
        "builtin eval 'pip install requests'",
        "trap 'pip install requests' EXIT",
        "trap -- 'pip install requests' 0",
    ],
)
def test_denies_visible_installation(command):
    from swe.security.shell_installation_policy import (
        INSTALLATION_DENIED,
        shell_installation_denial,
    )

    assert shell_installation_denial(command) == INSTALLATION_DENIED


@pytest.mark.parametrize(
    "command",
    [
        "echo hello",
        "pip list",
        "pip show requests",
        "python -m pip list",
        "pip uninstall requests",
        "pip --log install list",
        "pip --log=install list",
        "pip help install",
        "uv sync",
        "poetry install",
        "npm install",
        "bash setup.sh",
        "python install_deps.py",
        "python -c 'import subprocess; subprocess.run(\"pip install x\")'",
        "echo 'pip install requests'",
        'echo "$INSTALLER install requests"',
        "rg 'pip install' README.md",
        "printf '%s' 'pip install'",
        "# pip install requests\necho hello",
        "cat <<'EOF'\npip install requests\nEOF",
        "cat <<'EOF'\n$(pip install requests)\nEOF",
        "cat <<EOF\npip install requests\nEOF",
        "bash -c 'echo \"pip install requests\"'",
        "env X=1 pip list",
        "echo $(pip list)",
        "pip show $PACKAGE",
        "echo $'pip install requests'",
        "trap -p",
        "trap -l",
        "trap - EXIT",
        "trap 'echo \"pip install\"' EXIT",
        "echo hello >log pip install requests",
        "pip --log install >log list",
        "source setup.sh",
        "alias pip",
        "alias -p",
    ],
)
def test_allows_non_installation_and_text_references(command):
    from swe.security.shell_installation_policy import (
        shell_installation_denial,
    )

    assert shell_installation_denial(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "$INSTALLER install requests",
        "p${SUFFIX} install requests",
        "$(echo pip) install requests",
        "pip $ACTION requests",
        "pip in${X}stall requests",
        "pip --log $LOG install requests",
        "env $OPTIONS pip install requests",
        'bash -c "$SCRIPT"',
        'eval "$SCRIPT"',
        "pip 'unterminated",
        "echo hello &&",
        "echo hello\x00pip install x",
        "pip --unknown-option x install requests",
        "echo pip install requests | sh",
        "bash <<'EOF'\npip install requests\nEOF",
        "bash /dev/stdin <<'EOF'\npip install requests\nEOF",
        ". /dev/stdin <<'EOF'\npip install requests\nEOF",
        'source "$SCRIPT"',
        'trap "$ACTION" EXIT',
        'bash -c --unknown-option "pip install requests"',
        "sudo -s <<'EOF'\npip install requests\nEOF",
        "csh -c 'pip install requests'",
        "fish -c 'pip install requests'",
        "pwsh -Command 'pip install requests'",
        "alias p=pip\np install requests",
        "builtin alias p=pip\np install requests",
    ],
)
def test_fails_closed_for_unverifiable_commands(command):
    from swe.security.shell_installation_policy import (
        UNCHECKABLE_COMMAND,
        shell_installation_denial,
    )

    assert shell_installation_denial(command) == UNCHECKABLE_COMMAND


def test_parser_failure_does_not_allow_execution(monkeypatch):
    from swe.security import shell_installation_policy as policy

    def broken_parser(*args):
        raise RuntimeError("parser unavailable")

    monkeypatch.setattr(policy, "Parser", broken_parser)
    assert policy.shell_installation_denial("echo hello") == (
        policy.UNCHECKABLE_COMMAND
    )


def test_windows_input_fails_closed_without_a_matching_grammar(monkeypatch):
    from types import SimpleNamespace
    from swe.security import shell_installation_policy as policy

    monkeypatch.setattr(policy, "os", SimpleNamespace(name="nt"))
    assert (
        policy.shell_installation_denial("echo hello")
        == policy.UNCHECKABLE_COMMAND
    )


@pytest.mark.parametrize("descriptor", ["", "0", "1", "2", "9", "10"])
@pytest.mark.parametrize(
    "redirect",
    [">sink", "<source", ">&1", "<&0", ">>sink", "<>sink", ">|sink"],
)
@pytest.mark.parametrize("prefix", ["pip", "python -m pip", "env X=1 pip"])
def test_redirection_forms_cannot_hide_installation(
    descriptor,
    redirect,
    prefix,
):
    from swe.security.shell_installation_policy import (
        shell_installation_denial,
    )

    command = f"{prefix} {descriptor}{redirect} install requests"
    assert shell_installation_denial(command) is not None
