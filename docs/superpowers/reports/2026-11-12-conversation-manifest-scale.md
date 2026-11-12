# 大迁移清单的进度镜像规模测量

日期：2026-11-12。本地目录，三个独立 Python 进程；不含 MySQL、NAS 正文或完整迁移，不作为万级端到端性能验收。

脚本：`scripts/benchmark_conversation_manifest.py`。分别执行：

```bash
PYTHONPATH=src:tests/fakes ../../venv/bin/python scripts/benchmark_conversation_manifest.py --entries 10000
PYTHONPATH=src:tests/fakes ../../venv/bin/python scripts/benchmark_conversation_manifest.py --entries 20000
PYTHONPATH=src:tests/fakes ../../venv/bin/python scripts/benchmark_conversation_manifest.py --entries 40000
```

在 worktree 根运行。每个 fixture 有同样数量的 Session assignments 与 batches；输入逐条生成，测量覆盖加载、流式写出、再次加载和逐条语义对账。保留源与输出两套磁盘索引，故索引峰值包含二者。fixture 生成不在计时或内存测量内。

| 每个 mapping 条目 | 总进度条目 | 输入字节 | 秒 | Python 分配峰值字节 | RSS 基线字节 | 采样 RSS 峰值字节 | 索引峰值字节 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 10,000 | 20,000 | 446,723 | 5.478 | 566,440 | 191,135,744 | 195,805,184 | 1,990,656 |
| 20,000 | 40,000 | 926,723 | 11.317 | 567,735 | 190,873,600 | 198,803,456 | 4,050,944 |
| 40,000 | 80,000 | 1,886,723 | 24.235 | 652,908 | 191,348,736 | 204,308,480 | 8,298,496 |

三个规模均逐条语义相等。DiskMap 按 key 排序输出，因此不要求 JSON 字节顺序与输入一致。输出 SHA256 分别为 `dfdaeb274be5ab1dcf38d2204b6a79794752d15ad9d2c8fb4b9c877a3974542f`、`f9ec62a2ed3fa276193cde43f3719d488e790d4035fcd72b69c8cd77ad1d70f6`、`184744c55caca284eb0cc7ead3811a1b49a5d574f16f589996587717142a857a`。

在此范围内，Python 分配峰值未随条目数同比增长；索引占用约随条目数增长。原生 RSS 仍有所增加，不能从这三个采样点宣称严格常量内存或操作系统级 RSS 硬上限。RSS 每 5ms 采样可能漏掉瞬时峰值；tracemalloc 不统计 SQLite 等原生分配。本轮与后台 MySQL 验收共享主机，耗时仅作资源测量背景，不作吞吐比较。

该 fixture 仅模拟大量小型 JSON 进度条目，其 assignment 值不是完整有效的 V3 票据（未包含 operation_key/relative_path，首个 target_id 为 0）。本脚本调用 JSON 层 `load_disk_manifest`，没有执行 `V3Manifest` 的业务绑定/票据校验或 SQL hydrate，不能据此宣称完整恢复控制面资源已经验收。不可变顶层 workspace 索引、超大单条 JSON 值的成本另受 reader 上限约束；还需结合完整迁移的 RSS、spool、SQL 记录才能放行。
