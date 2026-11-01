# 会话迁移实施记录

更新：2026-11-01，Asia/Shanghai。实施工作树：

`.worktrees/conversation-mysql-nas`，分支
`feature/conversation-mysql-nas-migration`。

## 已验证增量

| 范围 | 提交 | 本次证据 |
| --- | --- | --- |
| 最终设计与十五任务实施计划 | dc3332d2e | 文档差异检查与提交钩子通过 |
| T1 基础 fixture 与真实旧 Session/archive reader 契约 | fd880c6b8 | 旧 API 契约基线 79 passed；新增旧 reader 5 passed |
| T2 精确 UTF-8 身份与 immutable bundle | 3bf159b8b | 独占发布、fsync 故障、路径/symlink、范围与流式读取；本地测试通过；生产 NAS 原语未验收 |
| T3 专用隔离 schema、正式 DDL/索引/分享扩展 | 556a6cde5 | 真实 MySQL 8.0.46，page size 16384，strict mode，UTC；8 passed，无 skip；151 列和分享扩展均验证 |
| T4 publication 基础与 unknown 对账 | e35ff1925 | 真实 MySQL 6 passed；预留/最终 COMMIT 响应丢失、安装回滚、unique-key 竞争、writer/abort 竞争 |
| T5 reader/dependency pin 基础 | 11a0861fa | 17 单元通过；真实 MySQL 4 passed；取消时排空 NAS worker、released key 不复用、guard 形状验证 |
| T6 SQL Chat 与原子完整 metadata | b20722f84 | 独立复审通过；真实 MySQL 14 passed；最后删除读取竞态新增回归；综合单元与旧分页/专家契约 155 passed；提交钩子全通过 |
| T7 索引历史与归档查询增量 | 209de073a | 已提交；完整入口验收仍按 T10/T14 记录 |
| T11 稳定清单与多格式导入核验 | 32044ca56、eab7f222f | 已提交 inventory/import/verify；离线零写入修复已在 b93ebbf4e 提交 |
| T8/T9/T10 执行、归档及分享发布基础 | fe7d0b1db | 已提交；后续运行时装配已提交；外部全入口验收见 Gate |
| NAS 根缺失不冒充已回收 | 33cc6e2f6 | 失败回归确认后修复；bundle 专项与提交钩子通过 |
| 原 workspace 的 SQL runtime 装配 | 72a7161a1 | 独立复审发现项已回归；564 单元/旧契约、3 真实 MySQL，提交前显式范围 hooks 全通过 |
| T15 scope-fenced GC | d5fd5e053 | READ COMMITTED、锁序、取消 drain、迟到文件精确重扫；最终隔离 MySQL 29 passed，默认 report-only |
| T12 一致备份/恢复 | ed24a35be | 有效引用闭包、fence/pin、空目标恢复；Python legacy NAS-only 路径拒绝 SQL authority |
| T12 完整反向导出 | 9169ca33d | v3 ledger/journal/新提交/删除语义，旧 reader/writer 本地回归；固定旧镜像仍未验收 |
| schema 漂移检查 | e39380f1f | charset/collation 与 share locator 属性精确验证 |
| T13 离线 CLI 与原子安装 | b93ebbf4e | 七命令、零写 verify、七表 RENAME/回退及手册；49 测试混合真实 DB 与 fixtures |
| T14 NAS 与停机验收工具 | a1fc353b9 | 两节点 Indexed Job 模板、真实 Pod/PVC/imageID 证据采集和故障测试；未部署目标集群 |
| 旧维护路径正文保护 | 58ee8c62b | 真实 bundle direct/alias 治理保护、Shell backup/restore SQL authority 拒绝；97 组合回归及精确文件集 hooks 通过 |
| semantic failure-hook fence | 4228d9167 | storage publication failure fences prompt/command/HTTP failure hooks; 170 unit/hook-fence tests and six real semantic SQL cases pass |
| final SQL fixture contract delta | 0ff2f5ee8 | 12 focused real MySQL integration cases pass; updated stale fixture lock/latest-turn tuple; W+ scope mypy narrowing; exact hooks pass |
| semantic batch 本地基准 | 7eec5aa0f | 真实 AgentScope print，单 producer/state-only 的三组顺序测量；不代替生产调优 |
| 旧历史显示顺序 | 0be85c33f | primary memory/task/error 对齐 runtime 排序；15 单元、13 真实 MySQL 通过，anchor 规则未改 |
| 既有 workspace 真 app 装配 | 3176050f5 | lifespan/Auth/Tenant/SQL 1 项通过；非 loopback 坏 token 401、有效 token 200 |
| 持久 Turn 答案边界修复 | 74057c17b | RED/GREEN 三场景、34 回答单元、78 MySQL 回归；显式 hooks 与实际提交钩子通过 |

上述提交证明各个实现增量存在，不代表全部目标环境验收通过。完整状态以
[逐项审计](../reports/2026-11-01-conversation-completion-audit.md) 和最终回归为准。

## 本次收敛与证据

运行时装配复用原 workspace 与共享 NAS；已安装映射必须匹配可信 tenant/source/agent。
正常新 provision 的空 workspace 可以按已解析身份接入，遗漏的旧会话数据必须先迁移；
不从路径反推身份。单用户默认入口仅复用清单明确记录的 default 身份。

独立检视发现并修复：终帧早于 terminal commit、preflight 异常无终态、外层协调器覆盖
已提交终态、SQL 审批拒绝清理调用文件私有接口、分享误用编码 runtime tenant、scope
跨租户错绑、静态清单阻止正常新建、只按列数接受错误 schema。readiness 现在检查
精确字段/可空性/默认值/自增、索引及 InnoDB base table；过程不执行 DDL。

2026-11-01 本次实际结果（不相加去重）：

- 新组件与旧 API、Session/AnswerTurn 专项：564 passed，0 skipped，4.151 秒；
  `/private/tmp/conversation-runtime-regression.xml`。
- 增量、CLI、备份边界与协调器专项：462 passed，0 skipped，1.434 秒；
  `/private/tmp/conversation-increment-regression.xml`。
- 真实 MySQL readiness/已关闭 execution 的未知终态确认：3 passed，0 skipped，17.955 秒；
  `/private/tmp/conversation-runtime-mysql-report.xml`；生成隔离库已清理。
- Console 原有兼容契约：8 个测试文件、93 passed；按 T14 计划执行 contextUsage、
  sessionApi、shareSelection、useExpertBinding。仅临时链接既有依赖，测试后已移除。
- T12 既有真实回退集：7 passed（366.919 秒），另 prepared 收敛/恢复验证 2 passed
  （60.532 秒）。后续 GC→backup 交叉缺口已由最终集成 suite 覆盖。
- T15 既有真实 GC：26 passed，后补并发专项 10 passed；最终收集 29 cases，不能把
  两次数量直接相加。独立审查确认默认 report-only，并发现 GC→backup 交叉缺口。

先前源码基线 `0ff2f5ee8` 的跨模块回归为 1,238 passed（63 个文件，17.81 秒）；
实际 MySQL 集成目录首次全量运行 268 passed、2 failed（47:25），暴露一个旧测试
执行 fixture 未含新 mutation lock，以及一个 latest-turn tuple 断言已过期。更新后，
涉及的两个集成文件最终真实 MySQL delta 为 12 passed、0 skipped（157.14 秒），隔离
control schema 已清理。首次失败和 delta 均保留在测试报告。随后同一最终源码的完整
MySQL 集成目录复跑为 270 passed、0 failed、0 skipped、2 warnings（2873.82 秒 / 47:53），
`PYTEST_EXIT_CODE 0` 且 `GENERATED_CONTROL_SCHEMA_REMOVED`；XML 为
`/private/tmp/conversation-final-full-mysql-rerun.xml`，已核读。两个 warning 是既有
`record_property` 与 xunit2 的格式兼容提示，不是测试失败。

三文件最终 SQL delta 在提交前精确 hooks 通过。全仓 hooks 仍未通过：worktree 的
TypeScript compiler/node_modules 缺失；全仓 mypy 最终 feature/main 均为 85 条，
逐条差异为空。格式 hooks 改写的 199 个无关 tracked 文件已经恢复，未动用户主树文件。
所有范围测试数量不相加，避免重叠计数；详见测试报告。
后续源码基线 `3176050f5` 仅执行新增专项，未重跑完整 270 项 MySQL 目录。
用户确认暂无 K8s/NAS，授权基于 `~/.swe` 本地模拟；只读复制既有 workspace，
40 Session / 55 Chat 可解析子集导入、幂等重入、139 项 verify、一致备份与空库
恢复、当前树 legacy reader 继续写通过。完整 inventory 仍因缺失文件和可信映射
ready=false，不猜测身份。证据与增量演练结果见
[本地模拟报告](../reports/2026-11-01-conversation-local-simulation.md)。
真实历史增量演练暴露 typed Turn 的 recovery 问题副本误作下一轮边界，已最小修复
并保留 legacy 区间行为。真实 Msg 回归有效 RED 1 failed，最终三参数 GREEN
3 passed；回答相关单元 34 passed。当前修复源码 execution/history/projection/API
四模块 MySQL 回归 78 passed、0 failed、0 skipped（766.13 秒），隔离 control
schema 已清理。最终增量演练报告 `passed=true`：正式 admission/start/finish、
fresh readers、增量备份与第二空库恢复、反向导出、当前树 legacy reader 续写均通过；
原 workspace 205 文件、4,140,461 字节的逐文件 SHA-256 不变，生成 schema 均清理。
固定旧镜像不可用，`image_verified=false`、`rollback_ready=false`；不关闭外部门槛。

## 环境与边界

- 数据库连接读取原工作区 `src/swe/config/envs/dev.json`，仅用于生成的一次性隔离 schema；
  不输出凭证、不提交用户配置、不修改业务表。每次测试生成的控制库/子库在结束清理。
- 用户明确复用原有 workspace/NAS 挂载；无需初始化新 workspace。新 bundle 使用受控新前缀。
  用户澄清后重新核读 factory/runtime/离线导入/recover，独立审查无确认 blocker；
  `test_factory.py` 本次 28 passed（0.56 秒），缺失业务根被拒绝且不创建目录。
- 运行时 SQL authority 为显式 opt-in，默认仍是 legacy；未修改生产配置、执行生产停机、
  业务七表安装或部署。
- 真实 NAS、跨节点双 Pod、全量生产规模迁移、完整恢复/回退和停机预算仍未验收。

## 外部验收门槛

- T10 全入口分支清单已提交：
  [conversation-storage-entry-inventory.md](../../../analysis/playbook/conversation-storage-entry-inventory.md)。
- governance/Shell bypass 已修复；最终直接入口与 SQL authority 分支记录在 entry inventory。
- 目标 MySQL/TDSQL capabilities、权限、MDL 和 atomic RENAME；原 NAS durability、跨节点
  双 Pod/迟到 IO 与 Pod death；固定旧生产镜像反向导出后读写；全部生产 scope 历史导入与
  full verify；完整规模 backup/restore、连续停机及 RSS/NAS/锁等待/p95/p99 测量均待目标
  环境验收。当前本机不能代表生产挂载或 Kubernetes。目标参数此前已询问，当前无需阻塞本地交付。

上述外部 Gate 未完成，因此 15 项计划的本地实现和报告交付不等于生产切换验收；生产
SQL authority 仍 opt-in，默认 legacy，不执行 DDL、业务数据迁移、停机或部署。

本记录不替代主计划的逐项验收；外部目标环境范围仍未验收。
