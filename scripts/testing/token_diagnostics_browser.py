"""Render the actual Console route with numeric API fixtures; no live DB writes."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect


def fixture():
    run = {
        "task_run_id": "ui-task",
        "branch_run_id": "ui-main",
        "parent_branch_run_id": None,
        "trace_id": "trace-demo",
        "started_at": "2026-10-11T03:00:00Z",
        "role": "main",
        "purpose": "main",
        "execution_state": "completed",
        "analysis_state": "insufficient_evidence",
        "analysis_summary": [
            {
                "rule_id": "history_growth",
                "state": "insufficient_evidence",
                "missing_fields": ["phase_identity"],
            }
        ],
    }
    calls = [
        {
            "call_id": f"ui-call-{i}",
            "logical_call_id": f"ui-logical-{i}",
            "logical_seq": i,
            "attempt_no": 1,
            "branch_run_id": "ui-main",
            "purpose": "main",
            "provider_id": "demo",
            "model_name": "example-model",
            "input_tokens": 15000 + i * 2000,
            "output_tokens": 500,
            "usage_status": "reported",
            "capture_state": "complete",
            "execution_state": "completed",
            "max_output_tokens": 4096,
            "max_input_tokens": 64000,
            "compaction_epoch": 0,
            "started_at": run["started_at"],
            "usage_details": {"cache_read_tokens": 12000},
        }
        for i in range(1, 5)
    ]
    known_input = sum(call["input_tokens"] for call in calls)
    summary = {
        "known_input_tokens": known_input,
        "known_output_tokens": 2000,
        "known_total_tokens": known_input + 2000,
        "observed_call_count": 4,
        "reported_call_count": 4,
        "partial_call_count": 0,
        "unknown_call_count": 0,
        "usage_complete": True,
        "capture_state": "complete",
        "composition_complete": True,
        "data_revision": 10,
        "analyzed_revision": 10,
    }
    inputs = [
        {
            "occurrence_index": i,
            "category": category,
            "source_ref": f"message:{i}",
            "estimated_tokens": amount,
            "fingerprint": "0123456789abcdef" * 4,
            "attribution_quality": "role",
        }
        for i, (category, amount) in enumerate(
            [
                ("system", 2000),
                ("tools", 3000),
                ("user", 500),
                ("assistant", 2500),
                ("tool_result", 9000),
            ]
        )
    ]
    issue = {
        "finding_id": "ui-finding",
        "rule_id": "repeated_tool_result",
        "rule_version": 1,
        "title": "同一工具结果重复进入模型输入",
        "action": "检查在线结果保留长度；保留完整工件供按需读回。",
        "affected_tokens": 18000,
        "evidence_quality": "needs_review",
        "evidence": {
            "call_ids": ["ui-call-1", "ui-call-2", "ui-call-3"],
            "occurrences": 3,
            "threshold_tokens": 8000,
        },
    }
    detail = {
        "task_run_id": "ui-task",
        "run": run,
        "branches": [run],
        "calls": calls,
        "summary": summary,
        "categories": [
            {
                "category": item["category"],
                "estimated_tokens": item["estimated_tokens"] * 4,
            }
            for item in inputs
        ],
        "findings": [issue],
        "analysis_state": "insufficient_evidence",
        "data_limited": False,
    }
    return (
        {"items": [{**run, **summary}], "next_cursor": None},
        detail,
        {"items": inputs, "has_more": False},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5175")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--channel",
        default=None,
        help="Optional installed browser, e.g. chrome",
    )
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    listing, detail, inputs = fixture()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, channel=args.channel
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        crashes = []
        page.on("pageerror", lambda error: crashes.append(str(error)))
        page.add_init_script(
            "sessionStorage.setItem('swe-iframe-context', JSON.stringify({state:{userId:'demo',source:'demo-source',hideMenu:true},version:0}))"
        )

        def respond(route):
            url = route.request.url
            path = urlparse(url).path
            if not path.startswith(("/api/", "/gateway-admin/")):
                route.continue_()
                return
            value, status = {"detail": "QA unrelated service unavailable"}, 503
            if "/gateway-admin/" in url:
                value, status = {
                    "data": {"accessToken": "test-token", "expiresIn": 3600}
                }, 200
            if "/auth/status" in url:
                value, status = {"enabled": False}, 200
            if "/token-diagnostics/" in url:
                status = 200
                if "/inputs" in url:
                    value = inputs
                elif "/by-trace/" in url:
                    value = {"items": [{"task_run_id": "ui-task"}]}
                elif "/runs/ui-task" in url:
                    value = detail
                else:
                    value = listing
            route.fulfill(
                status=status,
                content_type="application/json",
                body=json.dumps(value, ensure_ascii=False),
            )

        page.route("**/api/**", respond)
        page.route("**/gateway-admin/**", respond)
        page.goto(
            args.url + "/analytics/token-diagnostics?origin=Y",
            wait_until="networkidle",
        )
        expect(
            page.get_by_role("heading", name="Token 记录与诊断", exact=True)
        ).to_be_visible(timeout=30000)
        expect(page.get_by_role("banner")).to_have_count(0)
        expect(page.get_by_role("complementary")).to_have_count(0)
        page.get_by_role("button", name="用量与诊断", exact=True).click()
        expect(
            page.get_by_role("heading", name="Token 类型构成")
        ).to_be_visible()
        for width, height in [(1280, 720), (1440, 900), (1920, 1080)]:
            page.set_viewport_size({"width": width, "height": height})
            page.screenshot(
                path=str(output / f"token-diagnostics-{width}.png"),
                full_page=True,
                animations="disabled",
            )
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            ), f"page overflow at {width}"
        page.get_by_role("button", name="查看调用 ui-call-").first.click()
        expect(
            page.get_by_role("heading", name="调用详情", exact=True)
        ).to_be_visible()
        page.get_by_role(
            "heading", name="调用详情", exact=True
        ).scroll_into_view_if_needed()
        expect(page.get_by_text("message:4", exact=True)).to_be_visible()
        expect(page.locator("[class*='spin-blur']")).to_have_count(0)
        page.screenshot(
            path=str(output / "token-call-detail.png"),
            full_page=True,
            animations="disabled",
        )
        page.get_by_role("button", name="刷新记录").click()
        expect(
            page.get_by_role("heading", name="Token 类型构成")
        ).to_be_visible()
        assert not crashes, crashes
        browser.close()
    print(
        "Browser QA passed: 3 desktop sizes, embedded layout, task/call selection and refresh"
    )


if __name__ == "__main__":
    main()
