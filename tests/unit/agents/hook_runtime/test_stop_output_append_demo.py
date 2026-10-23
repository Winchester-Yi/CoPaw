# -*- coding: utf-8 -*-
"""Stop 追加样例只列出最终正文中真实存在的参考链接。"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

DEMO_ROOT = (
    Path(__file__).resolve().parents[4]
    / "wiki/hook/stop-output-append-command-demo"
)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("已完成，没有参考链接。", None),
        (
            "参考 [规范](https://example.org/spec) 和 "
            "[同一规范](https://example.org/spec)。",
            "### 回复中的参考链接\n\n1. [参考链接 1](https://example.org/spec)",
        ),
    ],
)
def test_append_demo_uses_only_links_present_in_final_response(
    response,
    expected,
):
    result = subprocess.run(
        [sys.executable, str(DEMO_ROOT / "scripts/append_reference_links.py")],
        input=json.dumps(
            {"hook_event_name": "Stop", "assistant_response": response},
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["decision"] == "allow"
    assert output["reason"]
    assert output.get("hookSpecificOutput", {}).get("appendText") == expected
    assert set(output) <= {"decision", "reason", "hookSpecificOutput"}
    assert not result.stderr
