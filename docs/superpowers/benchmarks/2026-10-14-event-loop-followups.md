# 事件循环后续优化：性能记录

本轮完成 checkpoint epoch-only、Unix Shell 流式 spool、SSE 重放分批、runtime-state 独立线程池与进程级指标。S3 恢复计划复用不在本轮范围内，分页次数只做观测。

## 复现与范围

在 freshness-ttl-optimization worktree 使用项目虚拟环境，依次独立运行：

```bash
PYTHONPATH=src ../../venv/bin/python scripts/benchmark_event_loop_followups.py --baseline
PYTHONPATH=src ../../venv/bin/python scripts/benchmark_event_loop_followups.py
```

本机 Darwin，4 个并发租户请求上下文及独立工作区，每个 Unix 子进程产生 16 MiB stdout，展示预算 4096 字节，checkpoint 10000 条事件。另运行 12 个 Provider 模拟任务、4 个三页 S3 mock 任务，以及默认 executor 两个线程被 250 ms 任务占满的隔离探针。fixture 构造不计入采样窗口。每个最终 artifact 大小与完整格式化输出一致，租户 ContextVar 经 runtime-state worker 传播保持隔离。

基线是旧内核行为模拟：Shell 全量积累、checkpoint 完整状态读取、探针使用默认 executor；不是完整旧版本的端到端应用。基线的其他 runtime-state 调用仍经过当前独立池，两种模式均有指标采集。无 HTTP、模型、数据库或真实 S3 调用；生产 Linux/Kubernetes 多实例表现需要部署环境继续测量。

## 结果

| 指标 | 基线模拟 | 优化后 |
|---|---:|---:|
| loop p95 / p99 (ms) | 33.264 / 34.175 | 1.111 / 1.136 |
| loop 样本数 | 34 | 113 |
| RSS 起始 (MiB) | 182.328 | 182.469 |
| RSS 结束 (MiB) | 353.922 | 186.469 |
| RSS 采样峰值 (MiB) | 362.047 | 186.797 |
| runtime-state 饱和探针等待 (ms) | 252.699 | 0.126 |
| active-state checkpoint 读取字节 / 调用 | 2337807 / 2 | 19 / 1 |
| S3 分页尝试次数 | 12 | 12 |
| 总耗时 (s) | 0.477 | 1.179 |
| 每租户结束时原始缓冲字节 | 16777215 | 0 |
| 每租户展示字节 | 4096 | 4096 |

流式 spool 降低了本次负载的 RSS 与 loop 延迟，同时增加小块 worker 提交和文件 I/O，总耗时增加。output 已完成任务从 12 增至 16400；这是当前 4 KiB pipe chunk 的代价，不能把两个 output 样本集合的单任务时长直接解释成吞吐提升。上述数字是单次本机采样，不是生产容量承诺。

| 优化后 worker | 完成数 / 样本数 | 排队 p95 / p99 (ms) | 执行 p95 / p99 (ms) |
|---|---:|---:|---:|
| output | 16400 / 2048 | 0.039 / 0.066 | 0.085 / 0.126 |
| runtime_state | 6 / 6 | 0.106 / 0.106 | 0.924 / 0.924 |
| provider | 12 / 12 | 22.758 / 22.758 | 27.656 / 27.656 |
| backup | 4 / 4 | 0.191 / 0.191 | 0.117 / 0.117 |

原始数据：[基线 JSON](2026-10-14-event-loop-baseline.json)、[优化后 JSON](2026-10-14-event-loop-optimized.json)。JSON 同时保留 avg/max 与全部 worker 数据。

## 指标口径与边界

- 运行诊断 `runtime_diagnostic` 日志增加 loop p99、样本数、`process_rss_sampled_peak_bytes`、`performance_metrics`。loop/RSS 是两次日志发射之间的窗口，诊断沿用原 10 秒采样周期；专项脚本使用 10 ms。RSS 采样峰值可能漏掉瞬时尖峰。
- worker 排队时间从协程开始等待容量/提交到真实线程开始，执行时间到真实线程结束；每类仅保留最近 2048 次完成任务。完成计数与 I/O counters 为本进程生命周期累计；失败任务以及请求取消后仍运行的任务计入真实完成。等待中取消且未提交的任务不计入完成样本。
- checkpoint 字节是应用实际读到的原始字节，覆盖 active-state 的 epoch、record、events 三条读取路径，不是物理磁盘 I/O，也不包含 evidence、candidate、transaction 或 `_write_epochs` 历史辅助读取。
- `s3_list_pages` 统计 `list_backups` 调用 `list_objects_v2` 的尝试次数，包括失败尝试；不统计 SDK 内部网络重试。没有复用恢复计划或减少分页次数。
- 指标按进程汇总，无租户/路径标签。Kubernetes 多实例需要按 Pod 汇总累计量；不同 Pod 的 p95/p99 不能直接取平均得到全局分位数。
- Unix Shell 每条 pipe 原始内存缓冲最多 budget，stdout/stderr 合计最多 2×budget，另有 pipe/解码缓冲（64 KiB）、字符串和解释器开销；这不是整个请求 RSS 上限。未启用压缩、Windows 和磁盘失败回退保留原全输出行为，不承诺内存有界。artifact 内容沿用 smart_decode/newline 与 UTF-8 格式化语义，不承诺保存原始非 UTF-8 字节。
- SSE 每 64 条历史入队/消费让出 loop，保持传输锁和历史/实时顺序；不会减少历史队列内存或总入队工作量。

## 验证记录

初次联合非 Shell 回归 264 passed、2 failed。两项失败在 HEAD 基线复现：技能目录直接写入限制；模型激活测试与 Provider TTL 测试连跑导致的缓存状态串扰（TTL 单独运行通过）。最终包含 Shell/真实 Toolkit 的联合回归为 291 passed、2 deselected，排除上述两项基线失败。扩大 Shell 回归另有三个基线失败：网络传输规则两项和 OpenCLI 引号断言一项。本轮不修改这些既有行为。

新增回归覆盖指标容量与并发、真实 worker 异常/取消计时、默认池饱和隔离、checkpoint epoch-only/损坏状态、原始读取字节、S3 成功/失败分页计数、Shell artifact 完整性/短写/重复取消/分类兼容、真实 Toolkit canonical 复用、SSE 顺序/取消/loop 让出。提交前执行全部改动文件 pre-commit、定向 pytest、diff 检查与完整 GitNexus 变更分析。

GitNexus 最终 detect_changes(scope=all, limit=1000) 返回 29 文件、160 符号、337 关联流程，partial=false、truncated=false，整体风险 CRITICAL。共享 runtime-state/output/Provider 影响已预先警告，并检查 token usage/subagent 等调用方回归。图索引存在动态调用与流程枚举容量边界，部分改动文件没有符号重叠映射；空映射不当作无影响，S3/SSE 仍执行明确的分页、顺序和取消测试。索引重建完成，但 FTS 因 UTF-8 错误降级；MCP 连接关闭后使用同一 LocalBackend 获取完整结果，未依赖关键词查询给出无风险结论。
