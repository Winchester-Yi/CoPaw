# -*- coding: utf-8 -*-
"""Render and freeze complete batch summaries within the channel budget."""

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")
ZHAOHU_TEXT_LINE_LIMIT = 200
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
    level = int(batch.get("follow_level") or 0)
    first = (
        f"{slot:%H:%M} · {label(batch['name'])}"
        f" · {LABELS[batch['status']]}"
        + (f" · 关注 L{level}" if level > 0 else "")
    )
    finished_at = batch.get("finished_at")
    if batch["status"] in {"completed", "failed"} and finished_at:
        finished = datetime.fromisoformat(finished_at).astimezone(ZONE)
        time_format = (
            "%H:%M" if finished.date() == slot.date() else "%Y-%m-%d %H:%M"
        )
        first += f" · 结束 {finished:{time_format}}"
    if batch["status"] == "unavailable":
        return [first]
    done, total = batch["done_count"], batch["member_count"]
    rate = f"{done / total * 100:.1f}".removesuffix(".0")
    progress = " · ".join(
        f"{name} {batch[key]}" for key, name in PROGRESS_FIELDS
    )
    workers = batch["workers"]
    lines = [first, f"进度 {done}/{total}（{rate}%） · {progress}"]
    lines.extend(
        [
            f"本批 Worker 占用 · {pool_name(pool)} {pool['occupied']}"
            for pool in workers
        ]
        or ["本批 Worker 占用 0"],
    )
    if not batch["failed_count"]:
        return lines
    failure = batch.get("failure_details")
    if not failure:
        return [*lines, "失败分类暂不可用"]
    types = failure["subtask_types"]
    failure_line = f"主任务失败 {failure['main_count']} · 子任务失败 {sum(types.values())}"
    type_counts = [
        f"{label(kind, 80)} {types[kind]}"
        for kind in sorted(types, key=lambda kind: (-types[kind], kind))
    ]
    if type_counts:
        inline = f"{failure_line}（{'、'.join(type_counts)}）"
        if len(inline) <= ZHAOHU_TEXT_LINE_LIMIT:
            return [*lines, inline]
    lines.append(failure_line)
    lines.extend(f"子任务失败类型 · {item}" for item in type_counts)
    return lines


def render_parts(source, business_date, snapshot, byte_budget):
    local = datetime.fromisoformat(snapshot["snapshot_at"]).astimezone(ZONE)
    header = f"【定时任务批调度简报】{label(source, 48)}\n业务日 {business_date} · 取数 {local:%Y-%m-%d %H:%M}（北京时间）"
    batches = snapshot["batches"]
    states = Counter(batch["status"] for batch in batches)
    summary = f"关注批次共 {len(batches)} 个" + "".join(
        f" · {name} {states[key]}"
        for key, name in LABELS.items()
        if states[key]
    )
    summary = f"{header}\n\n{summary}"
    if not batches:
        summary += "\n截至当前暂无可报告的关注批次"

    pool_lines = [
        f"{pool_name(pool)} · {pool['occupied']} / "
        f"{pool['capacity'] if pool['capacity'] is not None else '容量未发布'}"
        for pool in snapshot["pools"]
    ]
    pools = [
        "全来源共享模型池 · 当前占用 / 有效容量",
        *(pool_lines or ["暂无模型池"]),
    ]
    blocks = [batch_lines(batch) for batch in batches]
    summary_with_pools = "\n".join((summary, "", *pools))
    if len(summary_with_pools.encode()) <= byte_budget:
        summary = summary_with_pools
    else:
        summary += "\n共享模型池见后续明细"
        blocks.append(pools)
    if len(summary.encode()) > byte_budget:
        raise ValueError("cron_report_part_too_large")
    if not blocks:
        return [summary]

    detail_header = f"{header}\n明细 · 第"
    digits = len(str(sum(map(len, blocks))))
    reserve = len(
        f"{detail_header} {'9' * digits}/{'9' * digits} 段\n\n".encode(),
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

    for lines in blocks:
        block = "\n".join(lines)
        if len(block.encode()) <= limit:
            append(block)
            continue
        continuation = f"{lines[0]}（续）"
        for number, line in enumerate(lines):
            append(line, continuation if number else None)
    pages.append(current)
    return [
        summary,
        *(
            f"{detail_header} {index}/{len(pages)} 段\n\n{body}"
            for index, body in enumerate(pages, 1)
        ),
    ]
