# 会话 MySQL / NAS 功能交付报告

日期：2026-11-01，Asia/Shanghai。实现分支 `feature/conversation-mysql-nas-migration`，
本轮修复源码 `74057c17b`（含主线合并 `201deb168`）。本报告说明代码实现与本地隔离验证；
不表示生产切换或目标环境验收已完成。

## 存储与身份契约

复用原业务 workspace 及其已挂载 NAS，不初始化新业务 workspace。Git worktree 仅用于
代码隔离。可信 tenant/source/agent 由现有 workspace 装配取得，Session 身份保留原始
UTF-8 user/channel/session；不从目录反推身份。六表保存元数据、索引、执行权及引用，
正文写入原 NAS 下受控的 `bundles/` 不可变前缀。原分享表保留八旧列并增加 locator。
SQL authority 显式 opt-in，默认 legacy；新权威模式数据库不可用时不回退文件。

用户澄清后的专项复核：factory/runtime/recover 均要求原 workspace 和 NAS 根已经存在；
离线导入不实例化业务 Workspace，正常空 workspace 接入分支也只登记已有目录的映射。
28 项装配测试通过，独立只读审查未发现确认 blocker；SQL staging schema、冻结源工作副本
和代码 Git worktree 不属于新业务 workspace。

## 已实现能力

- Chat CRUD、完整 metadata、专家绑定 CAS、同 Session 多 Chat、旧/新 cursor、无分页数组
  与精确 total，均复用既有返回模型。删除提交 tombstone，不在 HTTP 中删除正文。
- Session/current/history/archive、冷热 Turn、重复源 ID、fragment、Chat anchor、context
  数字快照与目标 scope Tracing 通过 SQL locator 和 NAS extent 读取，使用显式 reader pins。
- durable admission 在模型调用前完成；generation/lease 保护执行权；Stop 先持久化再取消。
  terminal root、Turn 终态与执行槽释放同提交，最终帧在确认之后发送。
- 实际 AgentScope/ToolGuard 工具和审批边界发布完整恢复根；文本按 UTF-8 bytes/events/time
  批量发布。共享 mutation lock 串行本请求的 archive 与定时发布，保留尚未入内存的输出、
  未知顶层模块与 Cron receipts。unknown 停止推进并使用原 operation 对账，不重放工具。
- 本地请求排空后受控结算；新进程 `conversation-storage recover` 从持久化 admission
  重建原 owner/generation，先 fence 再结算。操作者须提供明确退出/IO drain 证据和审计
  receipt；CLI 不声称自动验证 Kubernetes 死亡。lease 过期不能代替死亡证明。
- 分享保留永久 token、旧孤立分享、访问计数和冷热问答。服务端 selection proof 在提交锁
  内重验具体 occurrence、完成状态、revision/high/epoch；不信客户端 scope 或 meta channel。
- 原 workspace 的 Console/CLI/Agent-scoped/Cron/W+ 入口接入；SQL 列表和重连使用持久化
  状态，空本地 coordinator 不把活动 Turn 伪造成 idle/terminal。
- 历史 inventory/manifest/import/verify 支持旧多格式、完整恢复模块、幂等批次及机器可读
  blockers；源备份只读，旧 pending 恢复在工作副本进行。
- 一致 DB/NAS backup/restore、完整 legacy reverse export、七表原子安装/撤回和一次连续
  停机运行手册已实现。恢复保留 reserved/stopping，Stop 不因恢复变成 settling 而丢失。
- GC 默认 report-only，引用闭包/fence/pins/deleting 状态防误回收。通用文件治理保护
  bundles 的 direct/alias 路径；legacy Shell/Python NAS-only backup 在 SQL authority 下拒绝。

## 本轮修复与提交

| 提交 | 内容 |
| --- | --- |
| `58ee8c62b` | bundles 治理保护与 Shell backup/restore authority 边界 |
| `63e73f2c3` | 语义发布、unknown 结算、进程重启 recover、Stop 恢复与续租竞态 |
| `caed335f5` | SQL 列表/分享/Console 重连权威与真实 API 矩阵 |
| `201deb168` | 合入主线 `5c8e96e5c` 的 nickname/catalog-only 变更 |
| `4228d9167` | 存储发布失败时禁止 PostToolUseFailure 外部 hook 继续执行 |
| `0ff2f5ee8` | SQL 集成 fixture/tuple 契约与 W+ scope 类型收窄 |
| `7eec5aa0f` | 真实 AgentScope semantic batch 本地基准与测量边界 |
| `0be85c33f` | 无 turn_states 的旧历史按 runtime primary 显示顺序导入及核验 |
| `3176050f5` | 既有 workspace 的真实 app lifespan/Auth/SQL 隔离验收 |
| `74057c17b` | 持久 Turn 按完整 scoped/visible 消息读取，避免 recovery 问题副本截断答案 |
| `aa846b947` | 本地切换故障矩阵、真实消息批次 commit 后恢复与 backup 失败边界；仅扩充测试 |

此前部分修复提交在精确同文件集 hooks 显式通过后使用 `--no-verify` 提交，避免共享 worktree
提交钩子 stash 干扰；不把它表述为 commit hook 自动通过。合并后范围 hooks 再次通过。
本轮 `74057c17b` 同时通过提交前显式检查和实际提交钩子。
主线 nickname/catalog-only、通用审批 replay 和明确 Stop 的通用子代理取消保持；不恢复
自然结束时强制 selected-expert wait/cancel。

## 审查与验收边界

三个代理按职责实现并交叉进行 specification 后 code-quality 审查；父代理读取差异、
真实 JUnit 和新回归。审查真实发现并修复定时发布/归档 CAS、unknown 后错误内存追加、
终态已提交但续租取消清理、Stop restore 语义、reconcile None sentinel、分享/重连竞态，
以及存储失败后继续派发工具失败 hook 的旁路。后续全量 SQL 集成还发现两处旧测试契约；
修正后 12 项真实 MySQL delta 与精确 hooks 通过；独立 settlement 复审无 blocker。
源码 `0ff2f5ee8` 的完整 MySQL 集成目录再次运行，270 项全部通过、无失败/跳过，退出码 0，隔离
control schema 清理已确认。首次失败与局部修正证据仍保留在测试报告。
后续新增专项与当前已提交基线完整目录分别记账：`35f749b4e`/runtime `74057c17b`
完整目录 274 passed、0 failed/errors、1 skipped（本机样本未配置的 app-bootstrap；
该项另有配置样本后单独通过证据），2910.12 秒，控制库清理确认。本轮新增故障参数
晚于该次收集；修改的三个集成文件完整复跑另为 74 passed、0 failures/errors/skips，
1376.02 秒，退出 0 且控制库清理。十阶段隔离演练完整 ledger 见
[本地模拟报告](2026-11-01-conversation-local-simulation.md)。
真实历史增量演练又发现 admission 问题在 recovery projection 中重复出现时，
`answer_turn` 会误以该 user occurrence 为下一轮边界而截掉答案。修复后，选中问题
关联持久 Turn 时按原 scope、Chat 和可见性读取该 Turn 的完整消息；legacy 未关联
记录仍按原 session_order 区间读取，archive-anchor suppression 与读后重验保留。
真实 Msg 的三个边界场景（无后续 Turn、同 Chat 后续 Turn、另一 Chat 后续 Turn）
均通过，严格返回原 question/answer；当前修复源码的四模块真实 MySQL 回归
78 passed、0 failed、0 skipped。修复后的历史增量备份/空库恢复、反向导出与当前树
legacy reader 继续写通过，原 workspace 未变；固定旧镜像仍未验证。
合并静态审查未发现确认 blocker；测试证据见配套测试报告。

目标 MySQL/TDSQL capabilities/权限/MDL、原 NAS 跨两节点双 Pod、固定旧生产镜像读回
继续写、全量生产 scope 数据、完整规模恢复与连续停机/RSS/NAS/锁等待/p95/p99 尚未验收。
本地十阶段演练覆盖旧 writer 等待超时与停止、离线备份/导入/核验/安装、readiness、
新写、share 访问、反向导出和当前树 legacy 续写；不是目标入口与所有 Pod writer 的排空，
也未验证固定旧镜像。
未执行生产 DDL、业务数据迁移、停机、部署、push 或将 feature 合回用户主工作树。
默认批次 64 KiB/64 events/1 秒仅为当前实现值，未完成生产基准调优；time 触发发布
不承诺远端 SQL/NAS durability 在固定毫秒内结束。已启动并行工具的外部副作用不可撤销。

## 相关交付

- [测试报告](2026-11-01-conversation-test-report.md)
- [逐项审计（78 个复选步骤）](2026-11-01-conversation-completion-audit.md)
- [API 查询矩阵证据](2026-11-01-conversation-api-evidence.md)
- [既有 workspace 本地模拟](2026-11-01-conversation-local-simulation.md)
- [切换与恢复手册](../../../analysis/playbook/conversation-storage-cutover.md)
- [直接入口清单](../../../analysis/playbook/conversation-storage-entry-inventory.md)
