# SSE 批量发布本地内核基准（2026-10-15）

突发负载下，批量发布的 stream worker 提交数减少约 96%，完成时间减少约 64%–80%。这只验证了本机内核任务数和吞吐目标。事件交付延迟、部分 loop lag 分位数及 RSS 增加；稀疏输出没有减少任务数。不能据此宣布 Linux/Kubernetes 上的完整验收通过。

当前默认：用户决定默认开启 TaskTracker offer 聚合，`SWE_SSE_BATCH_WRITES=0` 可关闭。连续读取优化默认生效。正式套件之后另补了异常引用释放并调整默认开关；下方哈希只代表被测源码快照，不冒充最终交付源码的哈希。默认开启不改变本报告的测量边界和待验证事项。

完整原始结果见 [2026-10-15-sse-batching.json](2026-10-15-sse-batching.json)。文件保留连续执行的全部 17 次正式运行，以及一次在第一条事件写入前失败的准备尝试（`publish_lock` 已从实现移除，benchmark helper 修正后重跑完整套件）。没有挑选最快运行。6 个模式组合的小规模 smoke 和 `--help` 检查另行执行，不用于性能结论。

## 方法与边界

- macOS 15.8.1 arm64，Python 3.12.12；本机临时文件，无 HTTP、模型、数据库、容器或部署存储。不是 Linux 3.15 / Kubernetes 生产压测。
- 每个模拟租户有一个独立 producer、一个独立 fast consumer 和一个完全不读取的 slow subscriber。每条事件包含 1000 个 ASCII `x`，consumer 逐条核对完整 frame、租户和序号，并核对 EOF。没有合并或丢弃业务事件。
- 默认模式 producer 使用 `offer()`，结束时 `end()` drain；消费者独立调用 `get()`。`--immediate` 每条直接等待当前 `ReplayLog.append()`，推进 committed 后通知消费者，模拟改造前逐事件提交调度。它仍使用当前的批量写底层和连续读实现，**不是旧提交二进制对照**。每条调用新的兼容 `ReplayStream.append()` 会包含新 writer/flush 开销，因此没有用它冒充旧基线。
- `--serial` 保留 publish/get 握手，默认路径使用等待提交的兼容 `append()`。`--legacy` 保留内存 history/queue 模拟；其功能与持久 replay 不同，不能用其耗时推导 HTTP 吞吐差距。
- 每次运行独立启动 Python 进程；运行间顺序执行。4 租户突发和稀疏负载各 3 对，成对交换 immediate/batched 顺序；16 租户仅一对探索性运行。测量期间未并行运行测试。
- elapsed 计入发布、最终 drain 和全部 fast consumer 消费，排除采样收尾和日志关闭。worker 计数也在 close 前取得；关闭本身的 worker 任务不计入。worker timing 是现有最多 2048 个任务的滚动样本，completed count 是累计实际完成数。
- RSS / 全局 pending 与 loop lag 按 5 ms 定时采样。pending 另记录每次 publish 返回后的单流观测最大值，均不能保证捕获瞬时真峰值。RSS 包含 benchmark 自身时间样本等内存。loop 采样包含末尾约 10 ms 的采样收尾；运行时长不同导致样本数不同，**不可把这些 p99 当作等时长、等样本数的稳定尾延迟比较**。
- `publish_start_to_consume` 从调用发布入口前计到 fast consumer 收到 frame，包含 admission 背压、聚合等待、线程排队、磁盘、读取和 loop 调度；不等于 HTTP 客户端应用延迟，也不单独测量 accepted-to-committed 或主动聚合定时器。

## 突发负载

无 pacing。每租户 5000 条；4 租户共 20000 条，16 租户共 80000 条。

| 运行 | elapsed s | worker 完成数 | write batches | loop p99 ms（样本数） | RSS 采样峰值 MiB | 发布至 get p99 ms |
|---|---:|---:|---:|---:|---:|---:|
| 4租户 pair1 immediate | 1.5061 | 33915 | 20000 | 0.248（298） | 27.66 | 10.717 |
| 4租户 pair1 batched | 0.4915 | 1214 | 628 | 1.019（91） | 28.73 | 18.872 |
| 4租户 pair2 batched | 0.5400 | 1202 | 628 | 1.060（98） | 29.08 | 17.999 |
| 4租户 pair2 immediate | 1.5011 | 33808 | 20000 | 0.174（297） | 28.25 | 10.615 |
| 4租户 pair3 immediate | 1.5306 | 33893 | 20000 | 0.183（302） | 27.36 | 10.800 |
| 4租户 pair3 batched | 0.4889 | 1210 | 628 | 1.247（90） | 29.19 | 18.027 |
| 16租户 immediate | 5.2597 | 113426 | 80000 | 0.349（1029） | 36.48 | 41.883 |
| 16租户 batched | 1.0769 | 4518 | 2512 | 1.207（197） | 39.14 | 134.539 |

4 租户各对 worker 减少 96.42%、96.44%、96.43%，elapsed 减少 67.37%、64.03%、68.06%；超过突发任务数减少 75% 的初始目标。16 租户探索对 worker 减少 96.02%、elapsed 减少 79.53%。批量模式平均每次写入 31.85 条；immediate 为 1 条。

代价同样可见：4 租户 batched 发布至 get 的 p99 为 18.0–18.9 ms，immediate 为 10.6–10.8 ms；16 租户为 134.5 ms 对 41.9 ms。4 租户 loop p99 和 RSS 观测也上升。总完成时间变短不意味着单事件交付延迟改善。需要 Linux 固定时长多轮负载继续判断这些变化是否稳定，不能用本表放行生产默认启用。

4 租户两种模式都写入/读取 21035560 字节，16 租户为 84172240 字节（含记录头）。所有 replay 运行 written_records 等于完整输入数，无 quota_rejections 或 replay_errors。4 租户 batched 全局 pending 采样峰值 311552 字节、单流观测最大 77888 字节；16 租户分别 1245888 / 77952 字节。

## 稀疏负载

4 租户，每租户 40 条，目标速率 20 条/秒，约 1.95 秒。最后一条在 end 时立即 flush，其余事件通常等待聚合 deadline。每次运行实际写入/读取 167960 字节，write batches 160、written_records 160、worker 320；没有任务数收益。

| 运行（实际执行顺序） | elapsed s | loop p99 ms（样本数） | 发布至 get p50 ms | 发布至 get p99 ms | RSS 采样峰值 MiB |
|---|---:|---:|---:|---:|---:|
| pair1 batched | 1.9536 | 0.777（351） | 7.626 | 8.629 | 24.80 |
| pair1 immediate | 1.9528 | 0.770（348） | 1.929 | 3.010 | 24.66 |
| pair2 immediate | 1.9531 | 0.799（348） | 1.805 | 2.479 | 24.80 |
| pair2 batched | 1.9542 | 0.822（351） | 7.436 | 8.319 | 24.80 |
| pair3 batched | 1.9533 | 0.787（352） | 7.566 | 8.658 | 24.81 |
| pair3 immediate | 1.9541 | 0.782（349） | 1.813 | 3.183 | 24.80 |

batched 交付中位数比配对 immediate 增加约 5.6–5.8 ms。该端到端增量包括主动等待以外的调度/I/O，不能解释为定时器精确超限或证明严格 5 ms 上界。全局 pending 采样峰值 4860 字节，单流最大 1215 字节。

## 兼容与内存对照

| 运行 | 配置 | elapsed s | worker | loop p99 ms（样本数） | RSS 采样峰值 MiB |
|---|---|---:|---:|---:|---:|
| serial immediate | 4 × 500 | 0.2600 | 4000 | 8.292（50） | 25.08 |
| serial batched/兼容 append | 4 × 500 | 0.2519 | 4000 | 0.525（51） | 25.08 |
| legacy 内存模拟 | 4 × 5000 | 0.1397 | 0 | 4.938（17） | 56.30 |

serial 握手中每条都等待提交和读取，因此任务数完全没有减少；这正是旧 benchmark 无法验证新入口批量上限的原因。这两次很短的 serial 运行不足以证明延迟改善。legacy 保留 20000 个 slow queue entry；replay 所有运行的 slow subscriber buffered payload 为 0。

所有 17 次正式运行关闭后，reserved_bytes、active_logs、active_subscribers、subscriber_memory_bytes、pending_memory_bytes 均为 0。累计 I/O counter 保留是预期行为。

## 复现与源文件锁定

```bash
# 独立 producer/consumer：batched 与当前内核 immediate 调度对照
PYTHONPATH=src venv/bin/python scripts/benchmark_sse_replay.py --tenants 4 --events 5000
PYTHONPATH=src venv/bin/python scripts/benchmark_sse_replay.py --tenants 4 --events 5000 --immediate
# 稀疏输出
PYTHONPATH=src venv/bin/python scripts/benchmark_sse_replay.py --tenants 4 --events 40 --rate 20
# 兼容握手与内存模拟
PYTHONPATH=src venv/bin/python scripts/benchmark_sse_replay.py --tenants 4 --events 500 --serial
PYTHONPATH=src venv/bin/python scripts/benchmark_sse_replay.py --tenants 4 --events 5000 --legacy
```

JSON 内保留每次实际命令、exit code、原始 stderr、启动 stdout 前缀、完整结果和测试环境。正式套件开始时 HEAD 为 `f832ddf337888cc33a8a9f523ccbeba3ca9992bb`，被测改动仍在工作区；不能只凭 HEAD 复现。三份相关文件在整套运行前后的 SHA-256 完全相同：

| 文件 | SHA-256 |
|---|---|
| `src/swe/app/runner/sse_replay.py` | `564418fd8b7323fb9be79634d81d407c25959063647705d5be283d623bb8073c` |
| `scripts/benchmark_sse_replay.py` | `69f9ce63dc3ec52446e590685153c15b06935cf5ee4ec34e60c690cf33339185` |
| `src/swe/performance_metrics.py` | `3931a758bd59c7fd15cad6fe519386837d7ce3fe1106ac3e259a30b656611e6f` |

尚未覆盖固定时长的 32 租户、2000 events/s pacing、历史重连、真实 HTTP 客户端应用时刻、Linux/容器内存、实际部署临时盘和同负载尾延迟 gate。功能正确性回归由实现测试负责；本基准的成功运行不替代这些验证。
