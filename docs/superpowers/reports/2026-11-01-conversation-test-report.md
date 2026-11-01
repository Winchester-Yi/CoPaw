# 会话元数据迁移测试报告

日期：2026-11-01，Asia/Shanghai。工作树 `feature/conversation-mysql-nas-migration`，
本轮修复源码 `74057c17b`；完整 270 项 MySQL 目录证据属于先前 `0ff2f5ee8`，
后续基线只执行新增专项，未重跑整个目录。本报告分开记录每个测试范围，不把重叠测试数求和；
首次失败和后续有针对性的通过结果均保留。

## 主要结果

| 范围 | 结果 | 证据与边界 |
| --- | --- | --- |
| 合并后会话与主线 cross-regression | 1,238 passed，0 failed，0 skipped，17.72s | `/private/tmp/conversation-final-head-cross-regression.xml`；63 个测试文件，含 storage、Runner/approval/Stop、nickname/delegation/Guard。首次沙箱运行有 26 项因本机 psutil 进程枚举和 `~/.swe.secret` 锁目录权限失败；获批提升权限后原命令全过。 |
| 最终完整真实 MySQL 集成目录复跑 | 270 passed，0 failed，0 skipped，2 warnings，2873.82s（47:53） | `../../venv/bin/python /private/tmp/run_conversation_full_mysql_rerun.py` 包装项目虚拟环境 `pytest tests/integrated/conversation_storage -q --tb=short --junitxml=/private/tmp/conversation-final-full-mysql-rerun.xml`；XML 已核读，`PYTEST_EXIT_CODE 0`，`GENERATED_CONTROL_SCHEMA_REMOVED`。两个 warning 仅为既有 `record_property` 与 xunit2 格式不兼容；本轮完整目录包含已修正的两个测试契约。 |
| 首次完整真实 MySQL 集成目录 | 268 passed，2 failed，0 skipped，47:25 | `/private/tmp/conversation-final-full-mysql.xml`。两个失败分别为 archive runtime 测试 doubles 缺少新 `mutation_lock`/`pending_operation`，以及旧测试错误地把 `get_latest_turn_state` 的 `(question_key, state)` tuple 当成 state。隔离 control schema 已清理。 |
| 原 workspace/NAS 装配专项复核 | 28 passed，0 failed，0 skipped，0.56s | `../../venv/bin/python -m pytest tests/unit/app/conversation_storage/test_factory.py -q --tb=short --junitxml=/private/tmp/conversation-existing-workspace-final.xml`；验证原 workspace/共享 NAS 复用、缺失根不创建、scope 拒绝与旧数据遗漏阻断。独立只读复核未发现该迁移路径初始化新业务 workspace。 |
| SQL 合同修正 delta | 12 passed，0 failed，0 skipped，157.02s | `/private/tmp/conversation-final-contract-delta-mysql.xml`；重跑 archive runtime 与 execution 两个真实 MySQL 测试文件；隔离 control schema 已清理。此 delta 为先前局部证据；最终完整目录复跑 270 项全绿另列。 |
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

以上完整目录与 cross-regression 结果来自 `0ff2f5ee8` 基线，不外推为后续源码的全目录复跑。
以下为 `3176050f5` 基线新增的独立专项：

| 范围 | 结果 | 证据与边界 |
| --- | --- | --- |
| 旧历史 primary 显示顺序单测 | 15 passed | `/private/tmp/conversation-migration-import-final.xml`；memory/task/error 与 runtime 顺序一致，anchor 规则未改。 |
| 历史顺序与 answer-turn MySQL | 13 passed | `/private/tmp/conversation-history-display-order-mysql.xml`；随机隔离 schema。 |
| 既有 workspace 的真实 app 装配 | 1 passed，9.35s | `/private/tmp/conversation-app-bootstrap-report.xml`；真实 lifespan/Auth/Tenant middleware，坏 token 401、有效 token 200 并读取 SQL chat；仅单文件隔离运行。 |
| 本地模拟契约回归 | 68 passed，0.67s | `/private/tmp/conversation-local-simulation-contracts.xml`；首次路径错误退出 4，没有执行测试，修正后才计入结果。 |
| 既有历史子集往返 | 40 Session / 55 Chat，139 项 verify 通过 | import、幂等重入、一致备份/空库恢复、当前树 legacy reader 继续写均通过；完整 inventory 仍 ready=false，固定旧镜像不可用。 |
| 真实 semantic batch 本地基准 | small/default/large publication 为 18/5/2 | `/private/tmp/conversation-semantic-benchmark.json`；单 producer、state-only，一次顺序实验；DB lock wait=null，不调生产默认值。 |
| typed Turn 答案边界 RED / GREEN | 有效 RED 1 failed；最终 GREEN 3 passed，65.55s | `/private/tmp/conversation-answer-turn-red.xml`、`/private/tmp/conversation-answer-turn-green.xml`；真实 AgentScope Msg，分别覆盖无后续 Turn、同 Chat 后续 Turn、另一 Chat 后续 Turn。首次简化夹具的 green 报错不计为通过。 |
| 回答相关单元契约 | 34 passed，3.02s | `/private/tmp/conversation-answer-turn-unit.xml`；history、answer-turn 及 API 契约。 |
| 当前修复源码四模块 MySQL 回归 | 78 passed，0 failed，0 skipped，766.13s | `/private/tmp/conversation-typed-turn-regression.xml`；execution、history、session projection、API 四模块，生成 control schema 已清理；不等于整个目录复跑。 |
| 修复后真实历史增量往返 | passed=true，退出 0 | `/private/tmp/conversation-local-increment-89634288187d4759a5f7b6c7c9d6c224/increment-report.json`；恢复历史后正式 admission/start/finish，fresh readers、增量备份/第二空库恢复、反向导出及当前树 legacy reader 继续写均通过。原 workspace 哈希不变，生成 schema 全清理；固定旧镜像未验证。 |

原 workspace 只读样本、源哈希、完整 inventory blockers、NAS 故障报告路径及新增
增量演练的逐阶段结果统一见 [本地模拟报告](2026-11-01-conversation-local-simulation.md)。
以上不是互斥集合，不提供累计 pass 总数。其他阶段性 suite 和替代证据见进度记录；同类重叠不重复累计。

## 全仓静态检查

最终变更文件的精确 pre-commit 检查通过，包括 AST、docstring、密钥、格式、mypy、
Black、flake8 和 pylint。先前三文件 delta 命令为：

```sh
PYLINTHOME=/private/tmp/conversation-contract-delta-pylint PYTHONPATH=src \
  ../../venv/bin/python -m pre_commit run --files \
  src/swe/agents/tools/emit_wplus_sop_event.py \
  tests/integrated/conversation_storage/test_archive_runtime_mysql.py \
  tests/integrated/conversation_storage/test_execution_mysql.py
```

本轮 typed Turn 修复限定 `src/swe/app/conversation_storage/history.py` 与
`tests/integrated/conversation_storage/test_execution_mysql.py` 的显式 pre-commit
检查通过。首次 Black 排版后第二次全通过；独立 reviewer 阅读最终 diff 与三参数
XML，未发现阻断项。完整 78 项回归和本地增量演练使用该修复后的源码。
提交前再次通过精确 hooks，真实 MySQL 三参数 3 passed（62.38 秒）及回答相关
单元 34 passed（2.93 秒）；XML 分别为 `/private/tmp/conversation-answer-turn-green-fresh.xml`
与 `/private/tmp/conversation-answer-turn-unit-fresh.xml`。直接 pytest 初次因未注入
显式测试库配置而跳过三项，不计为通过；随后隔离库包装命令完成实际运行并清库。
修复提交 `74057c17b` 的提交钩子也全部通过，未跳过检查。

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
- `74057c17b` 持久 Turn 的完整答案读取与三边界场景回归。

最终三文件测试契约/mypy delta 已独立提交为 `0ff2f5ee8`，并经 settlement reviewer 先规格后质量复审，无确认 blocker。审查者亲读 12-case SQL 与 1,238-case cross XML。本报告不包含生产环境验收。
