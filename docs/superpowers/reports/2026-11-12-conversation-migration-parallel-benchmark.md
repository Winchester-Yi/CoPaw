# 会话迁移并行策略本地基准

日期：2026-11-12。源码基线：`2db056542`，仅新增基准脚本与本报告。

## 范围与方法

运行 `scripts/benchmark_conversation_inventory.py --workspaces 10000 --io-delay-ms 1`。基准生成只读本地源目录，10,000 个 workspace 中每 20 个包含一个 Chat 和一个 Session，其余为空；每个 Session 含两条短消息。对 1、4、8、16 worker 分别执行真实 `inventory_workspace` 解析、临时 spool 编码与写入，并在 workspace 解析前注入 1 ms 可控延迟。脚本同时采样 Python allocation peak、进程 RSS 和 spool 字节数。

这是 workspace inventory/spool 阶段基准，不执行数据库导入、SQL journal 恢复、内容发布、语义 verify 或 finalize；源为本地文件系统，不代表业务 NAS 的延迟、吞吐或故障特性。

## 结果

| Workspace workers | Workspace/s | 耗时 | Python peak | 采样进程 RSS peak | Spool |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 172.7 | 57.894 s | 5.63 MB | 212.1 MB | 6.23 MB |
| 4 | 229.7 | 43.542 s | 9.30 MB | 221.6 MB | 6.23 MB |
| 8 | 223.8 | 44.677 s | 9.39 MB | 227.8 MB | 6.23 MB |
| 16 | 221.7 | 45.098 s | 5.43 MB | 228.2 MB | 6.23 MB |

在这组本地数据中，4 worker 比 1 worker 快约 33%；8/16 worker 没有继续提速，RSS 随并发上升约 16 MB。结果支持默认并发保持 4，不支持仅凭此基准调高默认值。RSS 是 10 ms 间隔采样，可能低估短暂峰值；Python peak 是 tracemalloc 数值，两者不可互换。

## 代码与数据库验证

- 并行、spool、inventory 定向测试：31 passed。
- 真实隔离 MySQL 的 v3 import 中断、resume、verify、finalize 生命周期集成测试：1 passed；fixture 使用少量 workspace 和本地 NAS 目录。
- `scripts/benchmark_conversation_inventory.py` 需要通过 `PYTHONPATH=src:tests/fakes` 运行，以复用测试环境中的可选 tracing SDK fake；基准业务代码仍从当前 worktree 导入。

上述 MySQL 测试验证功能生命周期和 SQL owner 行为，不是 10,000 workspace 数据库负载测试。万级端到端迁移的 SQL 吞吐、journal 成本、完整 verify/finalize 时间、恢复耗时和资源峰值仍未验收；实际 NAS 上线前也必须用代表性 NAS 再测。
