# Event Loop Followups Implementation Plan

**Goal:** 完成 epoch-only、Shell spool、SSE 重放分批、测量后 runtime-state 隔离；排除 S3 恢复计划复用，记录其分页调用次数。

**Architecture:** 沿用已批准的兼容方案。进程级指标不含租户/路径标签，worker 时长使用每类最多 2048 个已完成任务的滚动样本，累计次数/读取量单独保存。复用 RuntimeDiagnosticManager 日志输出 loop p95/p99、当前与采样峰值 RSS 以及 worker 指标。Shell 仅在启用工具压缩的请求上下文使用 spool，关闭截断或磁盘失败保留全输出兼容回退。

**Tech Stack:** asyncio、ThreadPoolExecutor、threading、psutil、pytest。

## Tasks

- [x] 指标 RED：固定时长样本验证 p95/p99、样本容量、线程并发计数；实际四类 worker 验证计时、异常及取消后仍记录真实完成时间。
- [x] 指标 GREEN：`record_worker_timing(worker, queue_seconds, execution_seconds)` 与 `increment(name, value=1)`；worker 类型仅 output/runtime_state/provider/backup，checkpoint 字节/调用与 S3 分页计数使用固定键。
- [x] 诊断 RED/GREEN：日志新增 loop p99、采样数、RSS 采样峰值与 performance_metrics；已有 API、日志字段保留。loop 延迟、RSS 使用原诊断采样周期，专项压测更密集采样。
- [x] checkpoint RED/GREEN：绑定只在 Chat 锁内读取 epoch 元数据，不读完整 record/events；损坏 epoch 立即报错，record/events 损坏由实际完整状态使用验证。记录 active-state epoch/record/events 的应用原始读取字节数（非物理磁盘 I/O，其他辅助路径不计）。
- [x] Shell RED/GREEN：实时流保持顺序，以有限缓冲与 worker 文件写入代替无界 chunk list，最终生成完整格式化 artifact 与 canonical 展示。验证小输出逐字兼容、大输出边界、UTF-8、错误/超时/一次/重复取消、短写及磁盘失败回退；Mixin 只在 enabled 路径绑定准确 workspace 与 budget。
- [x] SSE RED/GREEN：每 64 个重放事件 `await asyncio.sleep(0)`，保持锁内顺序直到订阅注册；取消不注册残留订阅。验证 unrelated heartbeat、历史/实时顺序与 attach_or_start。
- [x] 压测脚本：项目虚拟环境运行固定大输出、多租户请求上下文和 worker 混合负载；报告 loop p95/p99、RSS 当前/峰值、worker 排队/执行、checkpoint 字节与 S3 mock 分页次数，明确本机模拟范围。
- [x] 根据默认 executor 饱和实验执行 runtime-state 隔离，保留请求 ContextVar、异常与取消行为，许可在真实任务完成后释放；运行 token usage/file I/O/session 等调用方回归。
- [x] 所有改动文件 pre-commit、定向集成 pytest、git diff --check、完整 GitNexus detect_changes；更新设计与 Playbook，提交当前分支。

## Validation

新指标测试先观察缺失能力失败；专项压测使用 `PYTHONPATH=src ../../venv/bin/python scripts/benchmark_event_loop_followups.py`。压测中 S3 使用分页替身，不调用真实数据库、模型或 S3；多租户验证 ContextVar 与工作区隔离，不代表端到端 HTTP 吞吐。

图影响记录：run_output_work HIGH；run_runtime_state_work CRITICAL（36 个直接调用方、token usage/subagent 流程）；Provider get_or_start_instance CRITICAL。RuntimeDiagnosticManager 构造图 UNKNOWN，由 `_app.py` 和测试的实际构造补充确认。索引存在动态调用/流程枚举边界，空流程不作为无影响依据。

性能结果与指标窗口见 [专项记录](../benchmarks/2026-10-14-event-loop-followups.md)。loop/RSS 为诊断发射窗口，worker 为最近 2048 次真实完成任务，I/O 为进程累计；本机压测不代表端到端生产吞吐。
