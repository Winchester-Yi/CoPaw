# Batched Stream and Shell Finalization Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Apply TDD and impact analysis before symbol changes.

**Goal:** 减少 SSE worker 与小块磁盘操作，并消除 Shell 最终处理重复扫描，保留取消、编码、顺序与完整输出语义。

**Architecture:** SSE 连续读取与 atomic append_many 作为文件内核；独立有界 writer 提供 offer/flush，兼容 append 仍等待提交。Shell 复用已封闭流的边界；单遍 UTF-8 候选因非法尾字节稳定回退超过20%而撤回。当前用户指定 perf/event-loop-optimization 分支继续，不改其他本地文件。

**Tech Stack:** Python asyncio、现有专用 executor、pytest、cProfile、既有 benchmark。

Specs: `docs/superpowers/specs/2026-10-15-sse-batching-design.md`, `docs/superpowers/specs/2026-10-15-shell-finalization-design.md`。用户“继续”授权实施。

## Task 1: SSE 文件内核

- [x] 在 `tests/unit/app/test_sse_replay.py` 增加真实文件包装计数测试：多条 read 仅一次 seek/read；Unicode/大帧/短读保持边界。运行专项 pytest 观察旧实现失败。
- [x] 实现 `_read` 连续区间读取、worker 内逐头解析，最多 64 条/64 KiB（单大帧例外）。
- [x] 新增 append_many 原子批次测试，覆盖单帧过大、配额、任意位置短写及 truncate 失败。实现批次容量预留、分块拼装、全成功后安装 offsets，失败回滚。
- [x] 运行 `venv/bin/python -m pytest tests/unit/app/test_sse_replay.py -q`。

## Task 2: SSE 有界 writer 与生命周期

- [x] 先测试 `offer/flush/end` 水位、32 条/64 KiB/5 ms、容量等待、重复取消和失败唤醒，再实现。
- [x] 保留 `append(text)` await committed；TaskTracker 按用户决定默认选择 `offer`，`SWE_SSE_BATCH_WRITES=0` 保留等待提交路径；本地延迟/RSS及部署验证边界继续保留。`end()` 收敛已接受事件后才移除 `_runs`，所有提前关闭复用同一 drain。
- [x] 每轮单 writer/单写任务，pending/in-flight 合计最多64条，普通256KiB、大帧独占；全进程32MiB输入引用预算。释放许可必须晚于实际worker完成。
- [x] 验证并发 emitter、断连不取消业务、结束尾批次、错误控制、global-budget等待取消。测试沿用 `test_task_tracker.py`、ASGI/replay/coordinator 回归。

## Task 3: Shell 最终处理

- [x] 先计数测试边界扫描：每流一次，断言 artifact 内容逐字节一致；实现 finalization-local bounds 复用。
- [x] 【已评估并撤回，不交付候选】测试合法 UTF-8 一遍读、非法首/中/尾与不完整尾字节的 locale 完整重写。曾实现 `_write_stream` 保存段起点、严格增量解码、失败 seek/truncate 后全段 fallback；不改 fsync/rename/完整字符串回退。
- [x] 测试 stderr marker 重置、双流前缀、截断失败、超时与取消，运行完整 spool 专项。
- [x] 按编码场景测量最终处理，多轮比较；非法尾部若稳定恶化超过20%，交付阶段A、保留原预校验，并记录原因。

## Task 4: 测量、复核和交付

- [x] 更新 `scripts/benchmark_sse_replay.py`，新增独立生产/消费模式，保留串行兼容负载；记录 worker 数量、排队、吞吐、p95/p99、RSS与配额归零。按突发/稀疏比较当前连续读内核的 immediate 调度模拟；不是旧HEAD二进制对照。
- [x] 添加 Shell 阶段测量脚本与多轮原始记录，不能把单轮 profiler 当作生产收益。
- [x] 先做规格符合性复核，再做代码质量复核；修复所有 Important/Critical。
- [x] 运行相关 pytest（SSE、TaskTracker、Console、coordinator、Shell）、修改文件 pre-commit、diff check、staged detect-changes；仅暂存本轮文件，正常钩子提交。
- [x] 更新两份设计实施状态、Playbook 和基准记录，验证最终 Git 状态；不涉及 S3 恢复计划复用。

## 上线前仍待完成（本轮不宣称通过）

- [ ] Linux/Kubernetes 固定时长多轮，4/16/32租户，实际临时盘与容器内存，等样本量loop/交付p95/p99及RSS准入。
- [ ] Shell 256MiB、并发worker排队/loop/RSS、fallback次数和字节的完整矩阵。
- [ ] SSE 2000event/s pacing、历史重连压力与真实HTTP客户端应用延迟；最终源码与被测快照的性能复核。

本地17次SSE测量及Shell两轮候选门槛记录均已保留。SSE任务数/吞吐目标达到，用户决定默认开启聚合，仍记录延迟代价和部署验证缺口；Shell只交付阶段A。
