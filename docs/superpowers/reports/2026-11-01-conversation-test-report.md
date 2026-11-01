# 会话元数据迁移测试报告

日期：2026-11-01，Asia/Shanghai。工作树 `feature/conversation-mysql-nas-migration`，
最终源码 HEAD `0ff2f5ee8`。本报告分开记录每个测试范围，不把重叠测试数求和；
首次失败和后续有针对性的通过结果均保留。

## 主要结果

| 范围 | 结果 | 证据与边界 |
| --- | --- | --- |
| 合并后会话与主线 cross-regression | 1,238 passed，0 failed，0 skipped，17.72s | `/private/tmp/conversation-final-head-cross-regression.xml`；63 个测试文件，含 storage、Runner/approval/Stop、nickname/delegation/Guard。首次沙箱运行有 26 项因本机 psutil 进程枚举和 `~/.swe.secret` 锁目录权限失败；获批提升权限后原命令全过。 |
| 首次完整真实 MySQL 集成目录 | 268 passed，2 failed，0 skipped，47:25 | `/private/tmp/conversation-final-full-mysql.xml`。两个失败分别为 archive runtime 测试 doubles 缺少新 `mutation_lock`/`pending_operation`，以及旧测试错误地把 `get_latest_turn_state` 的 `(question_key, state)` tuple 当成 state。隔离 control schema 已清理。 |
| SQL 合同修正 delta | 12 passed，0 failed，0 skipped，157.02s | `/private/tmp/conversation-final-contract-delta-mysql.xml`；重跑 archive runtime 与 execution 两个真实 MySQL 测试文件；隔离 control schema 已清理。此 delta 不伪装成整个 270 项目录的一次全绿运行。 |
| API/分享 MySQL 全矩阵及增量 | 57 passed + 2 passed，其中 1 项重叠，共 58 个唯一 SQL case | `/private/tmp/conversation-api-matrix-final-report.xml`、`/private/tmp/conversation-api-final-delta-report.xml`。没有单一 58-case XML；无失败/错误/skip，生成隔离 schema 均清理。详见 [API 矩阵证据](2026-11-01-conversation-api-evidence.md)。 |
| 语义发布最终真实 MySQL/NAS 专项 | 6 passed，0 failed，0 skipped，270.29s | `/private/tmp/conversation-semantic-mysql-report.xml`；包含合并后 failure-hook gate 回归。使用测试 NAS 根，不代表目标 NAS 双节点验收。 |
| 存储失败后的 ToolGuard failure-hook 单测 | 170 passed，0 failed，0 skipped，0.84s | `/private/tmp/conversation-final-hook-fence-unit.xml`；prompt/command/HTTP、工具结果与后置上下文、storage/unknown 错误和普通错误控制。 |
| Conversation storage unit | 453 passed，0 failed，0 skipped，1.23s | `/private/tmp/conversation-semantic-storage-all.xml`。 |
| 支撑脚本/archive/ledger/cutover unit | 72 passed，0 failed，0 skipped，6.55s | `/private/tmp/conversation-support-final-unit.xml`。 |
| backup/maintenance authority 边界组合 | 97 passed，0 failed，0 skipped，1.27s | `/private/tmp/conversation-boundary-combined-final.xml`。 |
| W+ SOP unit | 30 passed，0 failed，0 skipped，1.08s | `tests/unit/app/wplus_sop/test_runtime.py`；对 W+ SQL scope 增加显式 non-null narrowing 后回归通过。 |
| Console 兼容契约 | 93 passed，8 个测试文件 | 已有命令覆盖 contextUsage、sessionApi、share selection、expert binding；测试运行于主树的相同 Console 源码。 |
| Restore 保留 Stop 的真实 MySQL | 1 passed，36.15s | `/private/tmp/conversation-restore-stop-mysql-report.xml`。 |
| Terminal commit 后 renew/cleanup 的真实 MySQL | 1 passed，11.02s | `/private/tmp/conversation-terminal-renewal-mysql-report.xml`。 |

以上不是互斥集合，不提供累计 pass 总数。其他阶段性 suite 和替代证据见进度记录；同类重叠不重复累计。

## 全仓静态检查

最终变更文件的精确 pre-commit 检查通过，包括 AST、docstring、密钥、格式、mypy、
Black、flake8 和 pylint。最新三文件 delta 命令为：

```sh
PYLINTHOME=/private/tmp/conversation-contract-delta-pylint PYTHONPATH=src \
  ../../venv/bin/python -m pre_commit run --files \
  src/swe/agents/tools/emit_wplus_sop_event.py \
  tests/integrated/conversation_storage/test_archive_runtime_mysql.py \
  tests/integrated/conversation_storage/test_execution_mysql.py
```

`pre_commit run --all-files` **未通过**，不报告为通过。TypeScript 检查因 worktree
没有 `console/node_modules` 而报 `tsc: command not found`。全仓 mypy 在修复 W+ scope
收窄前为 87 errors；修复后为 85 errors，主线基准也是 85，逐条差异为空。Black、
encoding pragma、trailing whitespace/trailing comma hooks 对 199 个无关 tracked 文件
做了自动修改；这些 hook-only 修改已经恢复，未动用户的主树文件。flake8 通过；pylint
报告 9.99/10。全仓输出保存在 `/private/tmp/conversation-final-all-files.log` 与
`/private/tmp/conversation-final-mypy-after-delta.log`。

## 数据库与安全边界

集成 harness 从项目原 `src/swe/config/envs/dev.json` 读取连接配置，仅创建随机生成的
空测试 control/child schema，结束时删除。测试期间没有对应用 schema 做 DDL、清理或迁移，
没有输出凭证。主树 `src/swe/config/envs/dev.json`、`goal_card.html`、`goal_design.md`
保持用户原有状态。

SQL authority 仍显式 opt-in，默认 legacy。未执行生产 DDL、业务数据迁移、停机、部署或
push。真实目标 MySQL/TDSQL、原 NAS 跨节点 durability、固定旧镜像回读后继续写、全生产
scope import/verify、全规模 backup/restore、停机与性能预算尚未验收。

## 小步提交

- `58ee8c62b` 维护路径保护与 SQL authority 边界。
- `63e73f2c3` 语义发布、unknown settlement 与重启恢复。
- `caed335f5` SQL 列表/分享/Console 重连权威。
- `201deb168` 合入主线 nickname/catalog-only 变更。
- `4228d9167` storage failure 时 fence ToolGuard 外部 failure hooks。

最终三文件测试契约/mypy delta 已独立提交为 `0ff2f5ee8`，并经 settlement reviewer 先规格后质量复审，无确认 blocker。审查者亲读 12-case SQL 与 1,238-case cross XML。本报告不包含生产环境验收。
