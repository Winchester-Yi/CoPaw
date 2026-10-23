# -*- coding: utf-8 -*-
"""将已放行正文中的 Markdown 参考链接整理为一条独立附加消息。"""

from __future__ import annotations

import json
import re
import sys


def main() -> int:
    """读取 HookContext；没有参考链接时成功返回 no-op。"""
    try:
        payload = json.load(sys.stdin)
        if (
            not isinstance(payload, dict)
            or payload.get("hook_event_name") != "Stop"
        ):
            raise ValueError("expected Stop context")
        response = payload.get("assistant_response")
        if not isinstance(response, str) or not response.strip():
            raise ValueError("expected nonempty assistant response")
        links = list(
            dict.fromkeys(
                re.findall(r"\[[^\]\n]+\]\((https?://[^\s()]+)\)", response),
            ),
        )
        output = {"decision": "allow", "reason": "reference links collected"}
        if links:
            entries = [
                f"{index}. [参考链接 {index}]({url})"
                for index, url in enumerate(links, start=1)
            ]
            output["hookSpecificOutput"] = {
                "appendText": "### 回复中的参考链接\n\n" + "\n".join(entries),
            }
    except (ValueError, TypeError):
        print("invalid Stop HookContext", file=sys.stderr)
        return 1
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
