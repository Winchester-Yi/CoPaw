# Tool Artifact Read Optimization Implementation Plan

**Goal:** 完成阶段 B 的历史工具输出部分，保留 canonical 截断协议、UTF-8/行边界展示和缺失原文回退。

**Architecture:** 在现有输出 worker 中用 seek/read 只取展示需要的头尾。缓存按 resolved path、dev/ino、size、mtime_ns、ctime_ns、预算和恢复引用隔离，最多 128 项且累计字符串内存不超过 4 MiB。目录清理按 resolved directory 和 retention_days 节流，300 秒内同键最多扫描一次，记录最多 128 项；淘汰后允许提前重扫。

**Tech Stack:** Python pathlib、os.stat/fstat、OrderedDict、threading.Lock、pytest。

## 边界

只改历史输出压缩核心和对应测试；同步显式 cleanup 接口仍每次执行，异步历史压缩使用节流入口。进程内缓存只存展示字符串；超过容量的结果正常返回但不缓存。文件校验每次执行，缓存不承担租户权限判定。保持用户提交的输出 worker 容量 8。

## Tasks

- [x] 用 Path.open 包装记录 read(size)，证明原实现全量且重复读取。
- [x] 覆盖 UTF-8、长行、换行、marker、文件变化/删除、跨目录隔离及读取失败回退。
- [x] `_existing_artifact` 返回验证后的路径及 stat；有限头尾 helper 使用原 UTF-8 与 marker 内核。
- [x] `_compact_display_text` 引入有界 LRU；读取前后 fstat 对比签名，文件变化保留原展示，不缓存错误结果；缓存锁不包围文件 I/O。
- [x] 验证缓存容量淘汰和超大展示不缓存。
- [x] 验证清理频率、保留期变更及同目录并发，先观察旧入口重复扫描。
- [x] 异步入口调用 `_cleanup_artifacts_if_due`；锁内预留下次时刻，锁外执行扫描。
- [x] 运行定向 pytest、改动文件 pre-commit、diff 检查及 GitNexus detect_changes。
- [x] 更新专项设计及 Playbook，记录读取量，标明其余阶段 B/C/D 未完成。

## Validation

`../../venv/bin/python -m pytest tests/unit/agents/utils/test_tool_output_artifact_io.py tests/unit/agents/utils/test_tool_output_compaction.py tests/unit/agents/test_tool_output_budget_mixin.py tests/unit/agents/test_memory_compaction_checkpoint.py tests/unit/test_output_workers.py tests/unit/agents/tools/test_shell_live_output.py tests/unit/test_tool_output_base_dir.py -q`

读取量 <= 1.5 × 展示预算；相同签名/预算第二次读取 0 字节；预算、文件或路径变化不能错误命中；缓存遵守两项容量约束；300 秒窗口内同键最多一次清理；展示逐字节与旧内核一致，缺失/读取失败保留原展示。

## 交付记录

- 基线 `6b8cd90f3`，保留用户调整的输出 executor/permit=8。
- 新增 27 个 artifact I/O/兼容性测试；7 个文件整合回归 **85 passed（4.29s）**。
- 先观察 6 个预期性能/缓存失败：2.7 MB 原文被完整读取、同预算重复读、缓存容量未实现、清理重复扫描。实现后 512 字节展示预算的应用读取量 <=768 字节，命中缓存读取 0 字节。这不是生产延迟或物理磁盘读取量压测。
- 独立审查发现整个 tool_result 目录外部 symlink 的回归；新增两个 RED 回归并修复 workspace containment 与恢复引用缓存隔离，复审通过。
- 最终 pre-commit（AST、mypy、black79、flake8、pylint 等）及 `git diff --check` 通过。
- GitNexus 增量索引曾产生空字符及异常 CRITICAL 上游集合；已完整重建、重新分析为 LOW。完整变更检查 5 文件、57 符号，partial=false、truncated=false；图本身仍有动态调用与流程枚举覆盖限制。
- 本子阶段完成后提交至 `freshness-ttl-optimization`。其余 checkpoint epoch-only、S3 恢复计划、Shell spool 和 executor 隔离仍待后续。
