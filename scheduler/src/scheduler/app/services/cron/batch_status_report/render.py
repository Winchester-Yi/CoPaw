# -*- coding: utf-8 -*-
"""Render and freeze complete batch summaries within the channel budget."""

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")
LABELS = {
    "completed": "已完成",
    "failed": "已失败",
    "running": "运行中",
    "pending": "待处理",
    "unavailable": "数据不可用",
}
PROGRESS_FIELDS = (
    ("success_count", "成功"),
    ("failed_count", "失败"),
    ("skipped_count", "跳过或取消"),
    ("active_count", "运行中"),
    ("waiting_count", "待处理"),
)


def label(value, length=40):
    text = (
        " ".join(str(value or "—").split())
        .replace("<", "＜")
        .replace(">", "＞")
    )
    return "".join(char for char in text if char.isprintable())[:length]


def pool_name(pool):
    return f"{label(pool['provider_id'])} / {label(pool['model_id'])}"


def batch_lines(batch):
    slot = datetime.fromisoformat(batch["logical_fire_at"]).astimezone(ZONE)
    first = (
        f"{slot:%H:%M} · {label(batch['name'])} · {batch['batch_id']}"
        f" · {LABELS[batch['status']]}"
    )
    if batch["status"] == "unavailable":
        return [first]
    done, total = batch["done_count"], batch["member_count"]
    rate = f"{done / total * 100:.1f}".removesuffix(".0")
    progress = " · ".join(
        f"{name} {batch[key]}" for key, name in PROGRESS_FIELDS
    )
    workers = batch["workers"]
    subtasks = batch["subtasks"]
    lines = [first, f"进度 {done}/{total}（{rate}%） · {progress}"]
    lines.extend(
        [
            f"本批 Worker 占用 · {pool_name(pool)} {pool['occupied']}"
            for pool in workers
        ]
        or ["本批 Worker 占用 0"],
    )
    if subtasks.get("incomplete"):
        lines.append(
            f"失败成员 {batch['failed_count']} · 异步子任务统计不可用",
        )
    else:
        failed, partial, timeout = (
            subtasks.get(key, 0) for key in ("failures", "partial", "timeouts")
        )
        lines.append(
            f"失败成员 {batch['failed_count']} · 异步子任务异常 {failed + partial + timeout}"
            f"（失败 {failed}、部分成功 {partial}、超时 {timeout}）",
        )
    return lines


def render_parts(source, business_date, snapshot, byte_budget):
    local = datetime.fromisoformat(snapshot["snapshot_at"]).astimezone(ZONE)
    header = f"【定时任务批调度简报】{label(source, 48)}\n业务日 {business_date} · 取数 {local:%Y-%m-%d %H:%M}（北京时间）"
    pools = snapshot["pools"]
    batches = snapshot["batches"]
    states = Counter(batch["status"] for batch in batches)
    summary = f"批次共 {len(batches)} 个" + "".join(
        f" · {name} {states[key]}"
        for key, name in LABELS.items()
        if states[key]
    )
    blocks = [
        (
            "共享模型池 · 当前占用 / 有效容量",
            [
                f"{pool_name(pool)} · {pool['occupied']} / "
                f"{pool['capacity'] if pool['capacity'] is not None else '容量未发布'}"
                for pool in pools
            ]
            or ["暂无模型池"],
        ),
        (summary, []),
    ]
    blocks.extend(
        (f"批次 {batch['batch_id']}（续）", batch_lines(batch))
        for batch in batches
    )
    if not batches:
        blocks.append(("截至当前暂无可报告的批次", []))

    digits = len(str(sum(len(lines) + 1 for _, lines in blocks)))
    reserve = len(
        f"{header}\n第 {'9' * digits}/{'9' * digits} 段\n\n".encode(),
    )
    limit = byte_budget - reserve
    pages = []
    current = ""

    def append(body, continuation=None):
        nonlocal current
        combined = f"{current}\n{body}" if current else body
        if len(combined.encode()) <= limit:
            current = combined
            return
        if current:
            pages.append(current)
        current = f"{continuation}\n{body}" if continuation else body
        if len(current.encode()) > limit:
            raise ValueError("cron_report_part_too_large")

    for title, lines in blocks:
        is_batch = title.startswith("批次 ") and bool(lines)
        content = lines if is_batch else [title, *lines]
        block = "\n".join(content)
        if len(block.encode()) <= limit:
            append(block)
            continue
        for number, line in enumerate(content):
            append(line, title if number else None)
    pages.append(current)
    return [
        f"{header}\n第 {index}/{len(pages)} 段\n\n{body}"
        for index, body in enumerate(pages, 1)
    ]
