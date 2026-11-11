# 定位路径

## 只暂停当前执行批次

- 看板详情的“暂停本批／恢复本批”通过批次级接口修改回执的 `batch_paused`，不会修改任务启停或批入口模式。
- 部署前手工执行 `scripts/sql/cron_managed_batch_pause.sql` 新增默认 0 的字段；应用只检查结构，不自动 ALTER 业务表。
- 派发前和自动／人工／每日业务重试前检查同一暂停标记；已交接、已受理及运行中的执行继续收尾和上报。恢复后保留原队列与次数继续执行。
- 领取按真实批成员关联检查，兼容普通入口先受理、后续被批次复用的执行；暂停候选在分页前排除，避免挡住其他批次。

## 批调度子任务错误类型与模型反馈

- 执行历史 `swe_cron_executions.error_type` 保留原始值；子任务 `async_status=error` 且原值包含大小写敏感的 `Prompt` 时才按模型失败处理。逗号分隔文本和包含该词的 JSON 字符串均命中。
- 新版统一分类入口为 `scheduler/src/scheduler/app/services/cron/managed/failure_policy.py`，用于自动、每日和人工业务重试及模型池反馈。非模型失败保留业务失败状态，但计入 worker 成功样本。
- 当前尝试关联的原始错误类型在看板成员及失败预览中显示，空值显示 `-`。
- 数据库列由外部预先新增；不会自动对已有库执行 ALTER。终态 `async_status` 和 `error_type` 应在一次提交中写入。

## 新任务工作日与通知

- 执行开关是 `schedule.run_on_non_workday`（缺省 false）；完成招呼通知开关是来源 `cron_notifications.skip_weekend_zhaohu_enabled`（缺省 false）。允许假日执行不等于必须在假日通知，两者不能互相代替。
- 日期/年份/日历组合从 `scheduler/.../calendar_schedule.py`、`managed/calendar_views.py`、`managed/external_query.py` 查起；外部查询缓存键必须包含日期区间。SWE 的 `managed_models.py` 也要接受同样的新版年份与 W/LW，不能只修 Scheduler 后在 prepare 再报 422。
- 新通知从 `managed_execution_api._source_config_scope` 的来源快照传到后台，再在 `ExecutionWriter.complete` 事务外查询原定通知日。`managed_history.notification_fields` 写 pending/not_required；Monitor 子任务汇总不重置该字段，领取必须仍为 pending。
- 排查先区分手动执行（豁免）、来源开关关闭、任务根本未执行、日历 fallback，以及已有 pending 积压记录。发送 worker 不按实际发送日重新过滤，不能把工作日旧通知在周末发送误判为本轮判定失效。
- 系统任务使用独立 `system.platform.system_platform_cron`，保留原五段平台规则，不走用户 W/LW 每日候选；不能将共享年份解析的放宽误传给不携带年字段的系统转换器。
- 最新范围和预发检查见 `docs/deployment/cron_workday_system_port_20261104.md`；组合回归见 `test_managed_workday_year_dates.py` 和 `test_managed_notification_calendar_context.py`。

## 新版批调度看板查询缓慢

- 入口：`GET /api/monitor/cron/managed-dispatch/batches`。列表及详情共用 `monitor/src/monitor/app/services/cron/managed_board_sql.py`；响应装配位于 `managed_board.py`。先区分 Pending/504 和明确 503，后者还需检查成员关系完整性，不能伪装为空批次。
- 规模检查按 `logical_fire_at`（UTC）比较批次数、`JSON_LENGTH(members_json)` 及最大批；无时区筛选按北京时间解释。933 成员应能正常读取，大批次只是定位线索。
- 回执沿用索引 `idx_managed_receipt_board(source_id,kind,status,logical_fire_at,batch_id)`，成员及执行沿用现有主键。本修复不增加业务列或索引。
- 原 `members_json` 为 LONGTEXT，逐成员提取执行 ID 和四项身份字段、再重复执行统计/列表聚合会放大文本解析成本。现在先筛选回执，以 `CAST(... AS JSON)` 和 `NO_MERGE` 将清单物化为原生 JSON；每批聚合只带预期成员数和类型，统计与分页复用一次聚合。
- 使用真实 MySQL `EXPLAIN ANALYZE` 和 optimizer trace 核对 `board_receipts`、`board_aggregates` 各物化一次并复用；仅写 CTE 不足以保证。单条列表 EXPLAIN 的统计部分可能提前求值，不能将最终分页节点耗时当作完整查询时间。
- 保留完整身份与顺序校验、最新重试指针、零成员、空页统计，以及不被状态筛选隐藏的坏关系。依赖仓库既有 MySQL 8.0.21+ 部署前提；SQLite 测试只适配语法，不能证明 MySQL 性能。
- 主工作区历史基准与本轮迁入分支的验证分别记录在 `docs/superpowers/plans/2026-11-03-managed-batch-board-query-performance.md`；模拟数据的 Monitor 查询方法耗时不等于生产库或 HTTP 网关延迟。
- 回归入口：`tests/unit/scheduler/test_managed_board_query.py`、`test_managed_board_api.py`，以及 `tests/integrated/scheduler/test_managed_board_mysql.py`、`test_managed_board_scaling_mysql.py`。真实 MySQL 仅接受显式 `CRON_MANAGED_TEST_MYSQL_JSON` 和 `test_cron_managed_*` 测试库，各测试只清理自己的随机租户数据。

## 定时执行凭据数据库镜像

- src/swe/app/crons/auth_migration*.py：POST /api/internal/cron/auth-migration/tasks，GET 按 task_id 与 after_page 查询。
- 在线镜像 auth_mirror.py、严格仓库 auth_repository.py；[排查说明](cron-auth-database-mirror.md)、[运维操作](../../docs/deploy/cron-auth-database-mirror.md)。

## Console 全局侧栏菜单

- 菜单定义、可见性规则和展开／折叠数据转换集中在 `console/src/layouts/sidebarNavigation.tsx`；`Sidebar.tsx` 保留状态订阅、渲染和交互。新增显示条件时先检查 `getSidebarVisibility`，避免在两种渲染分支各写一遍。
- 当前行为有意在本次纯重构中保留：`hideChat=true` 时，展开模式隐藏聊天、运营看板和用户消息；折叠模式仅隐藏用户消息。专家社区和 Hook 管理仅在展开模式出现。`source === "RMASSIST"` 与 `space === "RM_ASSISTANCE"` 是两个独立条件。
- 展开模式通过 `layouts/constants.ts` 的 `KEY_TO_PATH` 跳转，折叠模式也复用该映射，但技能运行看板保留原有 `collapsedPath`：该项尚无展开模式路由映射。修复该差异应作为独立行为变更处理。
- 回归入口为 `console/src/layouts/Sidebar.test.tsx`，覆盖身份、来源、空间、bbk、hideChat 组合下两种模式的菜单顺序及既有跳转差异。先确认预期业务规则，再更新测试，避免通过结构整理改变菜单范围。

## 按来源查询租户及机构过滤

- `GET /user-info/tenants/by-source` 位于 `src/swe/app/routers/user_info.py::list_tenants_by_source`；`source_id` 未传时沿用请求头来源，查询包含模板条目。
- 可选 Query 参数 `bbk_id` 通过 `TenantInitSourceStore.get_by_source` 下推 SQL 精确匹配；不传时返回该来源全部条目，无匹配时返回空列表。回归入口：`tests/unit/routers/test_user_info_tenant_source.py`、`tests/unit/workspace/test_tenant_init_source.py`。
- 在 `migrate_tenant_init_source_bbk.sql` 之后执行 `scripts/sql/migrate_tenant_init_source_source_bbk_index.sql`，为新旧数据库补充 `idx_source_bbk (source_id, bbk_id)` 联合索引；脚本检查当前数据库索引是否存在，可重复执行。

## 定时任务第一阶段全量快照迁移

- 一次提交：`POST /api/internal/cron/migration/tasks`，operation 为 preview/import/verify；后台自动翻页，不必人工传递游标。只写新定义/状态业务表，旧 CRUD、文件、内存及执行入口不切换，独立系统任务不迁移。
- 任务编排与持久进度：`src/swe/app/crons/migration_tasks.py`，复用既有 `swe_async_tasks`、`swe_async_task_items`。查询 `GET /tasks/{task_id}` 可跨实例；`pages` 是逐页诊断，`after_page` 只控制诊断读取，不驱动迁移。
- `partial_failed` 表示失败/冲突，核对时也涵盖缺失/差异/来源丢失。内存缺失另看 `progress.memory_incomplete`，全量文件扫描不代表集群内存齐全。
- 停止用 `POST /tasks/{task_id}/stop`，当前批结束后生效。停止 Postman 请求不等于停止后台 worker。没有重启自动续跑；疑似中断先确认实例，再同范围重新提交安全重跑。
- Postman、认证、已有进度表前提和保护上限见 [迁移操作说明](../../docs/deploy/cron-phase1-snapshot-migration.md)；原同步分页接口仍保留。

## W+ SOP active-session 空结果

- 接口：`src/swe/app/wplus_sop/router.py` 的 `get_active_wplus_sop_session`。Chat 存在且归属匹配、但没有活动 SOP 时返回 `200` 和 JSON `null`；Chat 不存在或身份、归属校验失败仍返回 `404`。
- 调用：`console/src/pages/Chat/components/WPlusSopActiveBar/index.tsx` 在挂载、Chat 切换、窗口 focus 和页面恢复可见时刷新。有待确认入口时跳过查询，有 SOP session ID 时查询具体会话，否则用 Chat ID 查询 active-session。
- 前端空快照清除活动状态；比较快照版本前必须判空，避免从已有活动会话刷新为 `null` 时崩溃。测试见 `tests/unit/app/wplus_sop/test_router.py` 和状态栏 `index.test.tsx`。

## 批调度暂停／恢复与关闭任务跳过

- 独立状态与旧任务归属：`scheduler/src/scheduler/app/services/cron/batch_run_state.py`。先排除普通任务和批子任务，只对批父任务初始化；状态存储在控制表，不跟随父任务后续 enabled 变化。
- 入队和交接：`batch_admission.py` 原子建批；`dispatch_gate.py` 检查 control、token 和自身任务状态。暂停退回未交接领取，已关闭任务结算为 skipped，不作为模型失败样本。
- API：SWE `/api/cron/jobs/{id}/batch-dispatch/run-state`，Scheduler 内部 `/api/scheduler/cron/dispatch/parents/{id}/run-state`。暂停先内部门控、再关闭 batch_dispatch_external_job_id；恢复先恢复该物理批定时器、再解除门控，均不操作普通定时器。外部暂停失败时内部仍暂停；Console `BatchRunStateControl.tsx` 刷新后可点“同步外部暂停”重复提交当前版本的暂停请求，刷新本身只读。
- 迁移 CLI：`python -m scheduler.migrate_batch_run_state` 默认只读预览，显式 `--apply` 才写入。部署、回滚及真实 MySQL 并发验收见 `docs/deploy/cron-dispatch-run-state-upgrade.md`；不能新旧 Scheduler 混跑。

## Worker 调整记录截断 / 模型变化折线

- Monitor 查询入口仍为 `/api/monitor/cron/dispatch/workers`。`capacity_events_next_cursor` 非空时继续携带 `capacity_cursor` 请求相同 source/时间范围；第一页有策略和当前容量，续页仅取历史。
- 分页 SQL 在 `monitor/src/monitor/app/services/cron/capacity_history.py`：每页 100 条、created_at/id keyset、固定 max(id)；不要把单页条数误认为历史总量限制。
- Console `CronBatchDispatch/workerHistory.ts` 拉取完整分页链并按 provider/model 构造阶梯线；`WorkerHistoryChart.tsx` 渲染图表。加载失败不展示不完整历史，切换范围使旧请求失效。

## 批调度优先名单 / 人工重试

- 模型限流分类包含网关错误码 `LAILGW0429`（访问请求过多）和 `LAILGW0433`（输出 Token 每分钟上限），兼容 `rate-limited` 连字符文案；Python 分类与 SQL 失败统计共用 `FAILURE_RULES`。这类错误仍按原有失败/重试反馈参与 worker 调整，与鉴权过期排除规则不同。

- 配置与代理入口：`src/swe/app/crons/batch_operations.py`；保存仅作用于广播源任务，等待 Monitor 定义同步确认，失败时保留本地配置并提示重试保存。
- Scheduler 操作：`scheduler/src/scheduler/app/services/cron/batch_operations.py`；失败分类统计覆盖全部当前失败 intent，预览与每次入队最多 200 个，以 id/attempt 防重，人工重试事件和状态更新同事务。
- 排序依据：`batch_priority.py` 与 intent payload 的 `dispatch_priority`；排序策略快照在 batch 的 `callback_metadata.batch_dispatch_priority`，重复 callback 不覆写。
- 容量边界：`capacity_claim.py`；scope 行锁保护实际领取和容量写入，已有 claimed/acknowledged/dispatched 均占用名额。
- Console：`Control/CronJobs/components/BatchPriorityEditor.tsx`、`Monitor/CronBatchDispatch/FailurePanel.tsx`。失败批次只展示结果数，补跑整个 intent，需按错误类型确认配置修复或旧执行停止。
- 验证：Scheduler 单测、`test_cron_batch_operations.py`、Monitor dispatch 查询测试及上述前端组件测试；SQLite 测试验证状态和 SQL 逻辑，不能替代部署 MySQL 多实例锁验证。

## NAS session lock release gate

在生产等价 StorageClass 上运行以下检查；`/mnt/sessions` 必须是所有 Runner Pod 共享的同一 NAS 挂载：

```bash
python3 scripts/verify_session_nas_lock.py /mnt/sessions
kubectl get pvc swe-sessions-nas -o jsonpath='{.status.phase}{" "}{.status.accessModes[*]}{"\n"}'
test "$(kubectl get pvc swe-sessions-nas -o jsonpath='{.status.phase}')" = "Bound"
kubectl get pvc swe-sessions-nas -o jsonpath='{.status.accessModes[*]}' | grep -qw ReadWriteMany
kubectl delete job swe-session-nas-lock-verification --ignore-not-found
kubectl apply -f deploy/session-nas-lock-verification-job.yaml
kubectl wait --for=condition=complete job/swe-session-nas-lock-verification --timeout=10m
```

检查必须同时证明：第二个 Pod 在持锁期间拿不到 `.verification.json.lock`、持锁进程被 SIGKILL 后可以接管、
第二个 Pod 并发读取原子 JSON 替换时始终可解析且 revision 不倒退，写者逐次核对连续 revision，读者须实际读取中途水位并核对最终 revision。脚本也检查硬链接独占发布及删除暂存名后的内容保留。失败时禁止以无锁写入或 Redis 锁替代；
先保留旧版本。Job 镜像必须是包含 `scripts/verify_session_nas_lock.py` 的本次 release 镜像；示例默认使用
`agentscope/swe:latest`，生产环境应改为已验证的不可变 release tag。

通过后暂停 cron dispatch，排空现有 session 请求，将旧 Runner Pod 缩容到零，再部署新版本，确认所有新 Pod
使用同一绝对 session 路径后恢复 cron。禁止新旧 Runner writer 混跑，否则旧逻辑仍可能覆盖新事务快照。

每次验证使用唯一、全新共享测试子目录，不直接使用用户 session 目录；失败后保留现场，下一次换新目录。两份脚本 JSON 及 Pod 节点、镜像摘要、PVC/StorageClass 都须留档，单机两个进程不算两 Pod NAS 通过。2026-10-16 本地 76 项测试、迁移/旧格式导出及 `ddb683634` 旧源码继续写入已通过，详见[本地验收报告](../../docs/superpowers/specs/2026-10-16-memory-storage-verification-report.md)；真实 NAS 门禁仍未执行。

真实历史副本入口为 `scripts/rehearse_memory_history.py`，源目录只读、输出目录必须全新且在源目录外，原始数据和私有指纹不得入库。2026-10-16 已完成 17 个 session、4 份 events-only 日志的脱敏迁移/导出及旧源码追加，380 个源文件 hash 未变化；[历史副本报告](../../docs/superpowers/specs/2026-10-16-memory-history-rehearsal-report.md)同时记录耗时、Python/RSS 峰值和文件数。样本最多 3 个 turn、77 条事件，不能把该结果当作长会话冷存储收益或真实 NAS 验收。

按问题类型给出优先查看的路径，减少无效搜索。

## Shell 子进程 / Python runtime guard / `/opt/.swe`

- shell 工具环境构造：[src/swe/agents/tools/shell.py](../../src/swe/agents/tools/shell.py)
- 重点看 `_prepare_subprocess_env()` 是否保留后端 `SWE_WORKING_DIR` / `SWE_SECRET_DIR`
- runtime env 过滤：[src/swe/envs/runtime.py](../../src/swe/envs/runtime.py)
- 重点看 `PROTECTED_RUNTIME_ENV_KEYS`、`_scrub_user_tool_subprocess_env()` 和 `preserve_boundary_env_keys`
- Python runtime guard 注入：[src/swe/security/python_runtime_path_guard.py](../../src/swe/security/python_runtime_path_guard.py)
- 重点看 `prepare_python_runtime_path_guard_env()`、trusted paths 和 trusted entrypoint roots
- 包导入期 env 加载：[src/swe/**init**.py](../../src/swe/__init__.py)、[src/swe/envs/store.py](../../src/swe/envs/store.py)
- CLI 根命令读取 last API：[src/swe/cli/main.py](../../src/swe/cli/main.py)、[src/swe/config/utils.py](../../src/swe/config/utils.py)
- 回归测试：[tests/unit/test_shell_tenant_boundary.py](../../tests/unit/test_shell_tenant_boundary.py)

## Console 复制 / Clipboard 权限策略

- 通用复制工具：[console/src/utils/clipboard.ts](../../console/src/utils/clipboard.ts)
- Chat 工具卡片复制入口：[console/src/components/agentscope-chat/Util/copy.ts](../../console/src/components/agentscope-chat/Util/copy.ts)
- 工具调用卡片渲染：[console/src/components/agentscope-chat/OperateCard/preset/ToolCall.tsx](../../console/src/components/agentscope-chat/OperateCard/preset/ToolCall.tsx)
- 复制兼容性测试：[console/src/components/agentscope-chat/Util/copy.test.ts](../../console/src/components/agentscope-chat/Util/copy.test.ts)

## Console 流式会话切换 / reconnect

- 后端入口：[src/swe/app/routers/console.py](/Users/shixiangyi/code/Swe/src/swe/app/routers/console.py)
- 运行态跟踪：[src/swe/app/runner/task_tracker.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/task_tracker.py)
- Chat 映射管理：[src/swe/app/runner/manager.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/manager.py)
- 前端会话映射：[console/src/pages/Chat/sessionApi/index.ts](/Users/shixiangyi/code/Swe/console/src/pages/Chat/sessionApi/index.ts)
- 前端 reconnect 触发：[console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Context/ChatAnywhereSessionsContext.tsx](/Users/shixiangyi/code/Swe/console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Context/ChatAnywhereSessionsContext.tsx)
- 前端请求 owner 透传：[console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/useChatRequest.tsx](/Users/shixiangyi/code/Swe/console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/useChatRequest.tsx)

## Console 第二轮提问 / OpenAI system role 顺序

- 后端 query 生命周期：[src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 `_emit_stop_hook_if_needed()` 是否把 STOP hook additionalContext 追加成 hook 前缀 `system` 消息
- Tool hook 结果记录：[src/swe/agents/tool_guard_mixin.py](/Users/shixiangyi/code/Swe/src/swe/agents/tool_guard_mixin.py)
- 重点看 `_record_tool_hook_result()` 是否把 tool hook additionalContext 追加成 hook 前缀 `system` 消息
- Hook 消息构造：[src/swe/agents/hook_runtime/messages.py](/Users/shixiangyi/code/Swe/src/swe/agents/hook_runtime/messages.py)
- 重点看 `build_hook_additional_context_msg()` 是否只生成标准 `system` 消息
- OpenAI-compatible formatter：[src/swe/agents/model_factory.py](/Users/shixiangyi/code/Swe/src/swe/agents/model_factory.py)
- 重点看 `_strip_top_level_message_name()` 是否保留 hook 前缀 `system`，并把其他非首位 system 降级成 `user`
- OpenAI-compatible 请求兼容层：[src/swe/providers/openai_chat_model_compat.py](/Users/shixiangyi/code/Swe/src/swe/providers/openai_chat_model_compat.py)
- 重点看是否还残留 `developer -> user` 的自动重试
- Runner 回归测试：[tests/unit/app/test_runner_hook_runtime.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_runner_hook_runtime.py)
- Formatter 回归测试：[tests/unit/agents/test_model_factory_tenant.py](/Users/shixiangyi/code/Swe/tests/unit/agents/test_model_factory_tenant.py)
- Provider 兼容回归测试：[tests/unit/providers/test_openai_stream_toolcall_compat.py](/Users/shixiangyi/code/Swe/tests/unit/providers/test_openai_stream_toolcall_compat.py)
- Tool hook 回归测试：[tests/unit/agents/test_tool_guard_hook_runtime.py](/Users/shixiangyi/code/Swe/tests/unit/agents/test_tool_guard_hook_runtime.py)

## 会话恢复 / developer role 反序列化断言

- 会话加载边界：[src/swe/app/runner/session.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/session.py)
- 重点看 `load_session_state()` 调底层 `load_state_dict()` 前是否把 legacy `developer` 单向迁移成 `system`
- Hook 消息构造：[src/swe/agents/hook_runtime/messages.py](/Users/shixiangyi/code/Swe/src/swe/agents/hook_runtime/messages.py)
- 重点看 `build_hook_additional_context_msg()` 是否仍只生成 `system`
- Runner 入口：[src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 `get_state_loaded()` 是否把 session 恢复异常直接暴露到 query 主链路
- 会话加载回归测试：[tests/unit/app/test_session.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_session.py)

## accepted plan 内部 tool exchange

- accepted plan 注入 helper：[src/swe/agents/react_agent.py](/Users/shixiangyi/code/Swe/src/swe/agents/react_agent.py)
- 重点看 `_build_accepted_plan_tool_exchange()` 是否只接受 `accepted_plan_source=server_plan_store` 且避开 Plan Mode
- Agent 推理入口：[src/swe/agents/react_agent.py](/Users/shixiangyi/code/Swe/src/swe/agents/react_agent.py)
- 重点看 `_reasoning()` 是否仅把内部 exchange 注入当前轮次，不进入 Toolkit、ToolGuard、hook 或前端工具卡片
- OpenAI / Anthropic formatter：[src/swe/agents/model_factory.py](/Users/shixiangyi/code/Swe/src/swe/agents/model_factory.py)
- 重点看 tool call / tool result 是否保持同一个 id，且最终请求里不出现 `developer`
- Plan Mode 请求上下文装配：[src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- 重点看 `_create_agent_for_query()` 是否只把服务端 accepted plan 放进 `request_context`
- 回归测试：
  - [tests/unit/app/test_task_progress_switch.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_task_progress_switch.py)
  - [tests/unit/app/test_runner_plan_mode_state.py](/Users/shixiangyi/code/Swe/tests/unit/app/test_runner_plan_mode_state.py)
  - [tests/unit/agents/test_model_factory_tenant.py](/Users/shixiangyi/code/Swe/tests/unit/agents/test_model_factory_tenant.py)

## Plan Interaction Card 发出后继续 reasoning

- Plan Interaction Tool 执行路径：[src/swe/agents/tool_guard_mixin.py](/Users/shixiangyi/code/Swe/src/swe/agents/tool_guard_mixin.py)
- 重点看 `_run_plan_interaction_tool_call()` 是否只在成功产出 `plan_interaction_card` metadata 后设置 turn boundary
- AgentLoop 下一轮 reasoning：[src/swe/agents/tool_guard_mixin.py](/Users/shixiangyi/code/Swe/src/swe/agents/tool_guard_mixin.py)
- 重点看 `_reasoning()` 是否消费 turn boundary 并返回空 assistant 消息，让 AgentScope 在同批工具完成后自然退出本轮
- 回归测试：[tests/unit/subagents/test_react_agent_and_guard_integration.py](/Users/shixiangyi/code/Swe/tests/unit/subagents/test_react_agent_and_guard_integration.py)

## 长 Tool 执行 / 用户中断 / running 状态

- 前端 chat 请求入口：[console/src/pages/Chat/index.tsx](/Users/shixiangyi/code/Swe/console/src/pages/Chat/index.tsx)
- 前端 abort 语义：[console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/abortReasons.ts](/Users/shixiangyi/code/Swe/console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/abortReasons.ts)
- 前端停止与请求 owner：[console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/useChatRequest.tsx](/Users/shixiangyi/code/Swe/console/src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/hooks/useChatRequest.tsx)
- 后端运行态跟踪：[src/swe/app/runner/task_tracker.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/task_tracker.py)
- 后端 query timeout：[src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)
- Console stop API：[src/swe/app/routers/console.py](/Users/shixiangyi/code/Swe/src/swe/app/routers/console.py)

## Tool Result 截断 / `<<<TRUNCATED>>>`

- 内置截断标志：[src/swe/constant.py](/Users/shixiangyi/code/Swe/src/swe/constant.py)
- 文件读取首次截断：[src/swe/agents/tools/file_io.py](/Users/shixiangyi/code/Swe/src/swe/agents/tools/file_io.py)
- 文件截断 helper：[src/swe/agents/tools/utils.py](/Users/shixiangyi/code/Swe/src/swe/agents/tools/utils.py)
- Agent 运行配置默认值：[src/swe/config/config.py](/Users/shixiangyi/code/Swe/src/swe/config/config.py)
- source 覆盖合成：[src/swe/app/source_system_config/runtime.py](/Users/shixiangyi/code/Swe/src/swe/app/source_system_config/runtime.py)
- 历史 tool_result 压缩 hook：[src/swe/agents/hooks/memory_compaction.py](/Users/shixiangyi/code/Swe/src/swe/agents/hooks/memory_compaction.py)
- MCP 工具返回转换：[src/swe/app/mcp/**init**.py](/Users/shixiangyi/code/Swe/src/swe/app/mcp/__init__.py)
- 详细经验：[analysis/playbook/tool-result-truncation.md](tool-result-truncation.md)

## Tenant bootstrap / default workspace scaffold

- 最小 bootstrap：[src/swe/app/migration.py](/Users/shixiangyi/code/Swe/src/swe/app/migration.py)
- 重点看 `ensure_default_agent_exists()`、`_do_ensure_default_agent()` 和它们只保证到哪一层
- 租户初始化总控：[src/swe/app/workspace/tenant_initializer.py](/Users/shixiangyi/code/Swe/src/swe/app/workspace/tenant_initializer.py)
- 重点看 `initialize_minimal()`、`ensure_seeded_bootstrap()`、`ensure_default_workspace_scaffold()`
- 租户池自愈入口：[src/swe/app/workspace/tenant_pool.py](/Users/shixiangyi/code/Swe/src/swe/app/workspace/tenant_pool.py)
- 重点看 cached tenant 再次 `ensure_bootstrap()` 时是否会补齐缺失的 `config.json`、`agent.json` 和模板文件

## 当前 Source 系统配置页 / task progress 开关

- Console 页面入口：[console/src/pages/SystemConfigPage/index.tsx](/Users/shixiangyi/code/Swe/console/src/pages/SystemConfigPage/index.tsx)
- 重点看 current-source 页面只读当前 iframe/source、403 态和保存/删除后是否调用 effective config 刷新
- 前端 current-source API：[console/src/api/modules/sourceSystemConfig.ts](/Users/shixiangyi/code/Swe/console/src/api/modules/sourceSystemConfig.ts)
- 前端权限头：[console/src/api/authHeaders.ts](/Users/shixiangyi/code/Swe/console/src/api/authHeaders.ts)
- 前端聊天页步骤条渲染开关：[console/src/pages/Chat/index.tsx](/Users/shixiangyi/code/Swe/console/src/pages/Chat/index.tsx)
- 开关读取 helper：[console/src/pages/Chat/taskProgressConfig.ts](/Users/shixiangyi/code/Swe/console/src/pages/Chat/taskProgressConfig.ts)
- 后端 current-source 路由：[src/swe/app/source_system_config/router.py](/Users/shixiangyi/code/Swe/src/swe/app/source_system_config/router.py)
- 后端注册表与默认值裁剪：[src/swe/app/source_system_config/registry.py](/Users/shixiangyi/code/Swe/src/swe/app/source_system_config/registry.py)
- 后端 service 合成与裁剪入口：[src/swe/app/source_system_config/service.py](/Users/shixiangyi/code/Swe/src/swe/app/source_system_config/service.py)
- Agent 提示词门控：[src/swe/agents/react_agent.py](/Users/shixiangyi/code/Swe/src/swe/agents/react_agent.py)
- 工具 no-op 兜底：[src/swe/agents/tools/update_task_progress.py](/Users/shixiangyi/code/Swe/src/swe/agents/tools/update_task_progress.py)
- runner stream 附加门控：[src/swe/app/runner/runner.py](/Users/shixiangyi/code/Swe/src/swe/app/runner/runner.py)

## 定时任务会话历史清理

- 配置入口：`source_system_config.cron_task_session_cleanup`，后端默认值与校验在 [src/swe/app/source_system_config/registry.py](../../src/swe/app/source_system_config/registry.py)，运行时解析在 [src/swe/app/source_system_config/runtime.py](../../src/swe/app/source_system_config/runtime.py)
- 默认状态：清理默认关闭；在当前 Source 配置页打开并保存后，会通过 [src/swe/app/source_system_config/router.py](../../src/swe/app/source_system_config/router.py) 刷新当前 Agent 的外部系统任务注册。
- Console 管理页：[console/src/pages/SystemConfigPage/index.tsx](../../console/src/pages/SystemConfigPage/index.tsx)，前端读写/时间转 cron helper 在 [console/src/pages/SystemConfigPage/registry.ts](../../console/src/pages/SystemConfigPage/registry.ts)
- 系统任务注册与执行：[src/swe/app/crons/manager.py](../../src/swe/app/crons/manager.py)，cleanup 外部系统任务 ID 保存在 source 级 `.system_jobs/sources/.../system_jobs.json`，同一 source 换用户保存配置时复用同一个 external id；不会写入业务 `jobs.json`
- 外部调度回调分发：[src/swe/app/routers/internal.py](../../src/swe/app/routers/internal.py)，`task_type=cleanup` 不需要业务 `job_id`，并按回调 `source_id` 展开该 source 绑定的所有逻辑租户；`tenant_id` 不是单用户清理边界
- session 文件写锁：[src/swe/app/runner/session.py](../../src/swe/app/runner/session.py)，cron agent 写回路径在 [src/swe/app/runner/runner.py](../../src/swe/app/runner/runner.py)
- 数据边界：只清理文件系统 task session JSON 中的 `task_runs`、对应 `agent.memory.content` 和可判定时间的 `task_messages`；不清理 `swe_cron_executions`、Monitor、Tracing 或审计数据

## Cron callback 洪峰 / event loop lag / workspace 冷启动串行

- 外部调度回调入口：[src/swe/app/routers/internal.py](../../src/swe/app/routers/internal.py)，重点看 `/api/internal/cron/callback` 如何解析 `task_type`、`tenant_id`、`source_id` 和 `job_id`
- Cron job 定义仓库：[src/swe/app/crons/repo/json_repo.py](../../src/swe/app/crons/repo/json_repo.py)，重点看 `JsonJobRepository.load()` / `save()` 是否通过 `asyncio.to_thread()` 包住文件读写、JSON 编解码和 pydantic 校验，`get_job()` 是否命中 mtime/size 快照索引
- Dream 系统任务：[src/swe/app/crons/manager.py](../../src/swe/app/crons/manager.py)，重点看 `_load_dream_logs()` 与 `run_dream_archive_maintenance()` 是否仍通过 worker thread 执行，避免 dream 日志读取和归档维护放大普通 cron lag
- Dream 孤立文件候选与删除边界：[src/swe/app/routers/dream_logs.py](../../src/swe/app/routers/dream_logs.py)，重点看 `_scan_orphan_files()` 是否跳过隐藏目录，以及根目录 `dialog` 是否通过 `KEEP_DIRS` 与通用路径解析保持为保留目录
- Workspace 冷启动：[src/swe/app/multi_agent_manager.py](../../src/swe/app/multi_agent_manager.py)，重点看 `MultiAgentManager.get_agent()` 是否只在全局锁内访问 `agents` / `_agent_start_tasks`，不同 cache key 的配置加载、`Workspace` 构造和 `start()` 不应互相阻塞
- 首轮验证：优先跑 `venv/bin/python -m pytest tests/unit/app/test_cron_json_repo.py tests/unit/app/test_cron_dream_nonblocking.py tests/unit/app/test_multi_agent_manager_concurrency.py -q`

## 批调度 Agent 已成功但 intent 仍为 dispatched

- SWE `/api/scheduler/cron/execution` 回执仅完成身份校验和执行记录持久化；Scheduler 在调度循环中读取 `swe_cron_executions.status` 和 Monitor 写入的 `async_status`，双成功才结束 intent。
- 扫描与名额入口：[scheduler/src/scheduler/app/services/cron/scheduling_service.py](../../scheduler/src/scheduler/app/services/cron/scheduling_service.py)，`dispatch_ready_once()` 必须在容量门槛前汇总结果、回收超时。
- 结果关联及回收：[scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py](../../scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py)，检查 `reconcile_dispatched_executions()` 是否匹配 intent、batch、当前 attempt、job、tenant；不要仅按 job_id 查最新记录。
- `async_status` 为空时继续占用名额。Agent 执行和子任务等待共用现有派发超时预算；主成功但子结果缺失的回收错误为“获取子任务状态超时”。
- 先确认 Scheduler 与 Monitor 访问同一执行表，且 Monitor 原有子任务同步/聚合正常运行；沿用 Monitor 的无子任务成功规则，不应把这一行为误诊为 Scheduler 提前完成。
- 首轮验证：`$env:PYTHONPATH='scheduler/src'; .\.venv\Scripts\python.exe -m pytest tests/unit/scheduler -q`。

## 定时任务详情分行维度 Excel 导出

- 页面与按钮入口：`console/src/pages/Analytics/CronJobOverview/index.tsx`。前端传入当前日期、分行及排序，不再克隆 DOM 或使用 ExcelJS 生成文件。
- API：`GET /api/monitor/cron/export-branch-dimension`，`start_date/end_date` 必填且包含起止日；`bbk_ids` 可选；`sort_by/sort_order` 成对提供，排序指标使用白名单。
- 后端路由：`monitor/src/monitor/app/routers/cron_branch_export.py`；复用 `QueryService.get_branch_behavior` 查询全量数据，由 `monitor/src/monitor/app/services/cron/branch_export.py` 生成原始 XLSX 二进制，前端读取 blob 下载。
- 保留 22 列、两层合并表头；计数千分位、比例两位小数，同值稳定排序，序号排序后从 1 开始。洞察/电访用户比例继续以 plan_managers 为分母。分行名强制文本避免公式解析；空数据返回合法表头工作簿。
- 用户于 2026-09-09 明确本次只需沿用现有查询过滤：使用 X-Source-Id（缺省 default）及 bbk_ids；未新增身份鉴权或分行权限机制。
- 加载中、无数据或导出中禁用按钮；失败提示并允许重试。参数或导出失败使用非 200 中文 detail JSON，成功为 Excel MIME 和 Content-Disposition 附件。
- 验证：Monitor 项目虚拟环境运行 `venv/Scripts/python.exe -m pytest tests/test_branch_export.py tests/test_branch_export_api.py tests/test_export_service.py tests/test_export_integration.py -q`；前端验证入口为 `console/src/api/modules/monitor.branchExport.test.ts` 和 `console/src/pages/Analytics/CronJobOverview/branchExport.test.tsx`。

## 我的任务自动预览文件选择

- 报告提取入口：`console/src/components/agentscope-chat/autoPreviewSelection.ts`，由任务结果卡片和页面预览共用。原始消息按从旧到新排列，选择最后一条含报告的消息及其中最后一个符合原有自动预览条件的文件；包括带 `auto-preview` 标记的 HTML，以及文件卡片中带 `resultId`、`templateId` 的动态报告，不解析文件名时间戳。同次执行优先取最终结果，没有匹配才取步骤结果。
- 页面入口：`ChatAutoPreviewHtmlProvider.tsx` 在会话加载完成后确定目标 URL；其他消息仍在生成时，已经出现的最新报告仍可自动预览。`AutoPreviewHtmlContext.tsx` 只接受该目标的文件卡片，120ms 防抖后打开一次。URL 比较沿用聊天媒体地址转换，父组件回调变化不能重置已经消费的预览机会。
- 排错注意：`MessageList` 会倒序挂载消息，不能用组件注册先后判断报告新旧。历史执行默认折叠规则在 `console/src/pages/Chat/sessionApi/index.ts`；最新执行没有报告时，不应回退到折叠的旧执行。加载期间不应消耗 5 秒候选等待窗口。
- 回归验证：在 `console/` 运行 `npm run test:run -- src/components/agentscope-chat/ChatAutoPreviewHtmlProvider.test.tsx src/components/agentscope-chat/autoPreviewSelection.test.ts src/pages/Chat/components/TaskRunGroupCard/index.test.tsx src/components/agentscope-chat/DownloadFileCard/index.test.tsx`。

## 定时任务关联计划 planId

- 创建接口 `POST /cron/jobs` 接受可选字符串 `planId`（最长 255 字符）；内部字段、返回值和 `jobs.json` 使用 `plan_id`，也接受 snake_case 输入。省略或 null 保存为 SQL NULL。
- 分发通过 `src/swe/app/crons/api.py` 的 `_build_broadcast_job` 复制任务；重复分发通过 `_merge_existing_child_with_source` 继承父任务当前的 `plan_id`。
- 同步链路：`monitor_sync_client.py::_build_job_sync_data` → Monitor `CronJobSyncRequest` → `SyncService.sync_job`，INSERT/UPDATE 都写 `swe_cron_jobs.plan_id`。同步仍为异步，失败查 Monitor sync 日志。
- 部署先在 Monitor 数据库执行 `deploy/migrations/2026_09_21_add_cron_plan_id.sql`，再部署 SWE/Monitor；脚本可重复执行，旧记录保持 NULL。`schema.py` 的初始化也覆盖新表与缺列升级，但当前服务启动路径不自动调用该初始化，不能只靠重启完成升级。
- 回归：`tests/unit/app/test_cron_plan_id.py`、`tests/unit/app/test_tenant_cron_api.py`。

## Claw 技能运行看板

- 页面：`/analytics/claw-data-overview`；组件：`console/src/pages/Analytics/ClawDataOverview/index.tsx`。
- 接口适配：`console/src/api/modules/taskTypeReport.ts`，使用共享 request 向 monitor 的 `/api/monitor/cron/task-type-report` 请求，继承来源与认证头。
- 日期按同一自然月限制。使用实际认证头 `X-Bbk-Id`：100 可选全部分行，非100只显示本分行并锁定；缺失身份不请求。分行和支行级联选项从 `/task-type-report/options` 查询 `jkh_user_inf` 同一名单快照；切换分行清空支行。
- 技能明细追加 `skill_detail=true` 和当前行分行/支行/user_id；客户经理查询支持 keyword（姓名/SAP号/岗位）。分行/支行全量查回、先展示20行并本地滚动追加；经理和经理技能明细请求 page/page_size=20，滚动逐页查询。切换条件取消旧请求，后续页失败保留已加载行并重试同一页。
- `ratio_unit=percent` 的值直接加百分号，不能再乘 100；null 显示“—”，比率可能超过 100%。技能明细是关联归属，不能将明细行相加作为总计。
- 本地 Vite 将 `/api/monitor` 代理至 `127.0.0.1:9090`；未启动 monitor 时可出现代理 500，先检查该服务，不要在页面伪造数据。
- 验证入口：`npm run test:run -- src/pages/Analytics/ClawDataOverview/index.test.tsx src/api/modules/taskTypeReport.test.ts`。
- 开发预览：`http://localhost:5173/analytics/claw-data-overview?demo=1`（注意是 data，不是 date）。仅开发环境启用显式模拟数据，包含三类任务、24 个默认分行及支行/经理/技能明细；不写入数据库。去掉 demo=1 恢复正式接口。

- 统计和技能明细各自通过 GET /task-type-report/export 导出后端 XLSX，传当前完整筛选、移除分页，包含未加载的行；模拟模式禁用导出。50000行以上后端返回413。契约见 docs/superpowers/specs/2026-09-14-claw-report-console/API.md。
- 客户行为同时展示去重客户数和点击总次数：`insight_customer_count` / `insight_count`、`phone_customer_count` / `phone_count`；次数列位于对应覆盖率之后，非名单方案显示“—”。

## 财富规划场景、权限与发布

- 场景列表由前端请求 `POST /api/agent/workspace/skill-config/list`，发送当前分行 `bbkId` 和分类 `category`；上游返回本分行与共享场景。创建与修改的场景归属、角色和冲突校验由 Java 规划服务负责。
- 岗位映射见 `console/src/pages/WealthWorkbench/permissions.ts`；`RB0108` 沿用行长权限。行长可见本分行的“分行关注”和本人创建的“行长关注”；中台只看本分行的“分行关注”；客户经理看本分行自己创建或分发给自己的规划。列表与详情权限应在 Java 校验。
- 规划发布、分发及状态回写由 Java 负责。排查子任务时用场景对应的源任务及广播 ID 对照共享 Cron 的广播结果和失败日志；不要仅从源租户的任务列表或规划发布状态判断目标子任务是否创建。
- 旧 Python `/api/wealth/*` 路由和规划编排已移除。共享 Cron、工作流绑定、机构人员接口和规划表保留；Java 接入见 [网关说明](../../docs/api/wealth-plans-java-gateway.md)。

## 财富规划推送客户数

- 创建/修改规划的每个场景可选填“推送客户数”，必须为正整数且不超过 JavaScript 安全整数范围，空值表示不限。配置区采用紧凑单行：经营场景和经营方向为独立列，场景与方向按约 6:4 分配可用宽度，最小宽度分别为 230px 和 140px，优先展示场景；方向列仅显示输入框并铺满该列，相邻列间距 25px，任务周期和执行频率都使用直接点击的按钮组；每日/每小时保持单行，每周/自定义的频率配置允许两行（频率按钮与时刻可同行，执行日或自定义规则放下一行）；客户数列 110px，横向操作列 82px。每日/每小时行高 56px，每周/自定义展开后约 93px，数量提示移至表头悬停说明，非法数量仍在控件下方提示。表头和数据行用 CSS subgrid 共享列宽，周期和频率按内容定宽，场景和方向共同分配剩余空间，输入框宽度为方向列的 100%；场景名称过长省略并提供悬停全文。1920px 下已验证每日单行、每周/自定义两行，含自定义日期和多个执行日均无横向滚动；窄屏仅配置区内部横向滚动，不挤压控件。
- 表头统一展示“经营场景 / 经营方向 / 任务周期 / 执行频率 / 推送客户数（选填）”，使用浅蓝色背景（#F7FAFF）及 12px、600 字重的蓝灰色列名（#596E8C），与数据行连续衔接，行间用横向细分隔线。自定义重复规则下拉框固定 104px；不再显示标准或自定义频率的重复摘要，仅旧版复杂规则保留编辑提示；发布确认仍展示完整频率。
- 任务周期使用原有按钮组，通过 `onClick` 调用原 `setTaskCycle`，五种周期及选中状态直接可见；重复点击当前快捷周期按当前日期重算起止范围，点击“自定义”保持手填日期。周期、场景、方向及数量保持横向排列；每周/自定义仅频率配置区允许两行，空间仍不足时配置区域通过 `overflow-x:auto` 横向滚动，不挤压控件。执行频率使用原按钮组，仍调用原 `updateSchedule`，cron 更新逻辑保持原样。`Create/index.test.tsx` 覆盖五种当前周期的重复点击、初始渲染不改日期及其他场景数据隔离。
- `PlanItem.custNum` 由草稿状态独立保存，发布确认展示具体人数或“不限”。`api.ts` 创建 POST、修改 PUT `/zt/wealth/plans` 均在对应 `scenes[]` 条目携带数值 `custNum`；空值完全省略该字段，不能转换为 0 或 null。规划查询响应的 `scenes[].custNum` 回填至编辑草稿，缺失或 null 显示不限。此链路直接转发 Java，已移除的 Python 规划模块不参与。
- `/api/agent/workspace/name-list` 的 `skillIds` 为对象数组：`[{ skillId, custNum? }]`。客户数从当天有效规划场景经任务树传入查询；经营视角只传当前场景，客户视角、待触达、已完成和人数角标传当天全部场景，未配置客户数时每项仅传 `skillId`，不得携带 null 或默认数量。按技能去重，沿用任务树首个命中规划的配置。
- 回归入口：`Create/index.test.tsx`（输入、清空、无效提示、发布确认）、`api.test.ts`（名单对象参数、去重及省略客户数）和 `store.test.ts`（正整数校验、逐场景隔离、创建/修改 payload、编辑回显、各名单透传客户数）。

## 财富工作台看板与列表标题

- 看板模式周、月日历仅在“今天”的已发布规划卡片右下角显示名单入口：周视图“查看名单”与发布状态同行；月视图有名单入口时不展示执行频率，规划名、技能数与右侧“查看名单”同排一行；无名单入口时，规划名、技能数与执行频率同排一行，排程过长时省略，卡片 Tooltip 提供完整执行频率。周、月入口文字跟随卡片色调（蓝色卡片深蓝、紫色卡片深紫），搭配 10px Ant Design 右箭头，悬停显示下划线；入口悬停或聚焦提示“查看客户名单”，且仅向拥有任务页权限的客户经理展示。入口与卡片详情按钮为并列控件，点击不触发详情；按 `buildTaskTree([plan], today)` 取该规划首个当天有效场景 ID，通过 `/wealth/today?skillId=...` 进入经营视角。任务页使用 ID 初始化选择，规划异步加载期间保留该参数，失效后回退任务树首项。其他日期和无当天有效场景的规划不显示入口。回归入口为 `Board/Calendar.test.tsx`、`Board/index.test.tsx` 和 `Tasks/loading.test.tsx`。

- 规划列表、日历提示与详情主状态通过 `utils.ts` 的 `planPublishLabel` 按 `publishStatus` 展示“发布中 / 发布成功 / 发布失败”，缺失或未知值展示“状态未知”。列表、日历提示和详情均不展示分发结果。`Plan.status` 仍保留Java `boardStatus` 聚合结果，继续供自动轮询和统计失效判断使用；不要用展示文案替换底层分发状态。

- 列表入口为 `console/src/pages/WealthWorkbench/Board/index.tsx`，标题使用 `Plan.name`（规划名称）。日历入口为同目录的 `Calendar.tsx`，周视图卡片标题使用规划名，下方显示“经营场景：”并汇总该规划的 `PlanItem.sceneName`，多项场景用中文逗号连接，最多两行，超出省略。月视图标题同样使用规划名，名称后用纯数字表示该规划关联的全部技能数量，不新增场景正文；两种视图统一使用 Ant Design Tooltip，悬停或键盘聚焦时分行展示完整规划名、经营场景与状态，长名称自动换行；移除原生 `title`，避免重复提示。
- 列表的“规划周期”按所选日期范围与规划 `start/end` 的闭区间交集筛选，不按 `Plan.period` 类型筛选。“自定义”显示日期范围选择器，默认本月，切换快捷周期后保留此前选定的范围；来源分类和概览统计使用同一筛选结果，日历仍使用自身的周/月区间。
- 日历按各场景排程筛选当天执行的技能，点击卡片仍传递日期与规划 ID；回归测试见 `Board/Calendar.test.tsx`，覆盖周、月两种维度。
- 日历的目标客户与已生成任务复用 `Board` 的 `planStat`，与列表一样由前端直接查询 `/api/agent/workspace/skill-stats`，请求 `planIds` 为规划 ID 数组，由 Nginx 转发；按返回项 `planId` 读取 `targetCustomerCount` 和 `targetTaskCount`，不再按场景或日期区间求和。未返回或接口不可用时显示 `--`，真实零值显示 `0`。不要直接使用规划映射中初始化为零的 `Plan.customers/tasks`；回归测试见 `Board/index.test.tsx`。

- 周/月切换仅本地筛选规划，不按周重新请求 `/zt/wealth/plans`。统计由 `Board/usePlanStats.ts` 按账户和去重排序后的规划 ID 集合缓存；相同在途请求合并，规划状态轮询不会清空缓存。成功与失败响应缓存 30 秒，到期后在下一次周期/筛选变化或规划轮询时后台更新；页面静止时不定时刷新。后台更新保留已有数字，失败提示暂显示上次数据；无已有数据时显示 `--`；加载/后台更新仅通过刷新图标旋转和禁用反馈，失败仍提示统计不可用或暂显示上次数据。发布/分发从进行中结束时使统计失效并主动查询，若请求在途则返回后串行补查；切换账户清空缓存，过期响应不覆盖当前周期。回归入口为 `Board/usePlanStats.test.ts`。
- 看板仅在存在发布中/分发中规划时自动查询：`Board/useBoardRefresh.ts` 在上一轮完成后等待 5 秒再查询，每次连续处理周期最多 3 分钟；到期静默暂停，手动“刷新看板”不会重新启动自动轮询。看板的规划与统计单次请求均由 `Board/requestTimeout.ts` 限制为 60 秒：超时取消底层请求并释放等待，保留已有数据；离开看板会立即取消在途规划请求；被取消的规划请求即使晚返回也不更新 store。自动与手动规划刷新共用在途请求，刷新入口使用图标按钮（无障碍名称“刷新看板”），在规划或统计请求期间旋转并禁用；手动成功刷新会绕过统计有效期更新当前统计。看板刷新使用严格读取，接口失败保留规划，不额外展示暂停或规划刷新失败文案，其他读取仍保持原有容错行为。回归入口：`Board/useBoardRefresh.test.ts`。

## 财富任务名单动态字段

- 经营视角“经营机会”必须显式保留 260px，不能在 fixed 表格中依赖 `width:auto` 或 `min-width` 分配剩余空间；姓名 110px、方案 135px，表格最小宽度按其余固定列合计 1029px（含边框）加指标区宽度计算。指标区按断点为 520 / 356 / 190px，空间不足时表格内部滚动，执行列继续固定在右侧。只调整经营视角，不覆盖客户视角、待触达和已完成的列宽。

- 今日任务（经营/客户视角）、待触达及已完成清单在执行/完成时间列之前展示“最近接触时间”，直接使用 `name-list.strongContactTime` 映射的 `Customer.time`，空值显示“—”，悬停可读原值。最近接触时间列宽 128px，标题保持单行；内容使用 12px 数字，日期正常展示，完整日期时间超宽时省略，单元格 title 保留原值。`.recentContactValue` 限制内容宽度，避免自动表格布局被完整时间撑宽。窄屏仅表格内部横向滚动。空态 `colSpan` 必须包含新列。已完成页原“完成时间”也沿用 `Customer.time`，暂保留原行为；将来若接口提供独立完成时间，应分别映射。回归入口：`Tasks/groupTables.test.tsx`。

- 今日任务客户视角、待触达和已完成的“经营场景”列提供单选筛选，选项从当前模块原始名单的 `skillNames` 去重生成，按展示名称精确匹配任一场景；不使用客户的首个 `skillId` 代表全部场景。同名场景合并为一个筛选项。选中标签单行显示，超长省略；悬停或键盘聚焦通过 Tooltip 查看完整名称，清除按钮不收缩。支持“全部场景”和清除，与搜索、重点标签、接触状态筛选叠加；仅过滤表格，不重算顶部后端汇总。经营视角不应用该列筛选。入口为 `Tasks/SceneFilter.tsx` 和 `Tasks/index.tsx`，回归入口为 `Tasks/groupTables.test.tsx`。

- 今日任务经营视角以场景 ID 作为唯一选中依据，高亮、名单查询、客户筛选和分类折叠状态均关联该 ID，名称仅用于展示。同分类同名但 ID 不同的场景可独立选择；场景改名或调整分类保留选择，选中 ID 失效后回退首个场景。入口为 `Tasks/index.tsx`，回归入口为 `Tasks/groupTables.test.tsx`。

- 侧栏“今日任务”红色数字角标与客户视角口径一致：`fetchTodayCustomers(..., "customer")` 查询 `/api/agent/workspace/name-list`，传当前用户 sapId、当天工作任务树全部去重技能 ID 数组与 touched=2，统计该范围内聚合名单总人数（包含已接触客户），为 0 时隐藏。store 独立保存 `todayCustomerCount`，避免经营视角按技能查询影响总数；侧栏首次加载及今日名单刷新时更新。待触达和已完成角标分别来自对应客户名单长度。入口为 `components/Sidebar.tsx` 和 `store.ts`，回归入口为 `components/Sidebar.test.tsx` 和 `store.test.ts`。

- 今日任务在 `Tasks/index.tsx` 分组后对每个分类的名单稳定排序：`touched === 1` 的已接触客户排后，其余待处理（未接触及未知状态）排前。同类状态保持接口顺序，分类顺序和 store 原始名单不变；客户视角同样适用，待触达/已完成不参与此排序。回归入口为 `Tasks/groupTables.test.tsx`。
- 今日任务、待触达和已完成页均展示“目标客户 / 今日已触达 / 七日内已触达 / 待处理”，使用各自名单响应的后端统计，搜索和筛选不重算汇总；已完成页不展示“累计已完成 / 今日已完成”，也不读取今日任务缓存统计。触达日期与人数由外部接口计算。
- Console 直接请求 `POST /api/agent/workspace/name-list`，由前端 Nginx 转发；前端把外部 `data.list`、统计及字段配置转换为 `items`、四项计数、`skillFieldList`、`allFields`。
- 今日任务、待触达和已完成分别查询客户名单，均传 `sapId` 与 `skillIds` 数组：经营视角只传当前选中场景，客户视角、待触达、已完成及今日人数角标均传当天工作任务树全部去重场景 ID。前端使用统一 `request("/agent/workspace/name-list")` POST `{API base}/api/agent/workspace/name-list`，由部署方前端 Nginx 转发外部服务，不再经过 Python 名单代理。JSON 直接传 `skillIds: ["a", "b"]`、`sapId`、`touched`，并从 iframe 上下文补充 `bbkId`、可选 `subBbkId/posId` 及固定 `platformSource/pageSource`；不显式设置 `credentials`，沿用 fetch 默认的同源 Cookie 行为；鉴权和上下文请求头由统一 request 构建。前端读取外部 `code` 与 `data.list`，转换为页面 `items`；HTTP/业务失败、网络异常或六十秒超时返回空名单。旧 Python 名单代理已移除。当天无场景时前端直接清空名单，不请求接口。触达状态保持今日任务 `2`、待触达 `0`、已完成 `1`，直接使用接口返回的名单。场景切换只重查选中技能，手动刷新与标签切回保持该选择，旧场景响应不覆盖新选择。回归入口为 `api.test.ts`、`store.test.ts`、`Tasks/loading.test.tsx`。
- 客户动态值数组的正式字段名是 `fieldList`；属性仍为 `filedName / filedNameCn / filedValue`。数组名拼错会导致模型丢弃明细，前端即使收到 `allFields` 也显示“暂无指标”。
- 指标丢失先检查外部名单服务是否返回客户明细及顶层字段配置，再检查 `console/src/pages/WealthWorkbench/api.ts` 的映射。
- `allFields` 提供中文名称；经营视角按技能的 `fields` 顺序与客户实际字段取交集，客户视角按全局字段顺序展示客户实际字段。不为其他客户的字段生成占位。
- 只有今日任务经营视角使用 `groupField`。组合值完全一致才合并，空数组不分组；把客户姓名配置为分组字段可能导致每人一组。每个分类对应独立表格，分类标题、人数和折叠按钮放在表格上方；多个字段以 ` / ` 分隔。
- `touched=1` 标记客户已触达；客户字段缺失不展示、返回空值显示“未提供”、数值 `0` 正常展示。
- 今日任务的经营/客户双视角在客户指标之后展示“接触状态”：保留接口原始 `Customer.touched`，`0` 显示灰色“未接触”，`1` 显示绿色“已接触”，其余或缺失显示灰色 `--`。顶部汇总使用后端独立计数字段，不按明细 `touched` 或 `done` 计算；不要由 `done` 布尔值反推接触状态 `0`。今日任务表格的状态列宽 110px，标题及筛选图标保持单行，指标空间和执行列保持可用；回归入口为 `api.test.ts`、`Tasks/groupTables.test.tsx`。
- 接触状态表头通过 `Tasks/ContactStatusFilter.tsx` 单选筛选“全部 / 未接触 / 已接触 / --”，复用重点标签浮层和已选条件的样式，支持一键清除、方向键、Escape、外部点击及滚动/缩放关闭。选项人数基于其他筛选条件下的名单（排除接触状态自身），筛选与经营任务/重点标签叠加，表格和分类人数同步更新，顶部四项后端汇总保持不变。状态列需容纳筛选图标，长筛选值不得反向撑宽表格。回归入口增加 `Tasks/ContactStatusFilter.test.tsx`。
- 指标交互入口为 `components/CustomerMetrics.tsx`；分组入口为 `Tasks/customerGroups.ts`。大量指标先查展开状态；长字段先查悬停或聚焦提示，不应依靠增加全部动态表头排查。
- 工作任务树的技能名称单行省略；统一 Tooltip 在悬停或键盘聚焦时展示完整技能名称与来源规划，移除按钮原生 `title`，避免重复提示。
- 视角切换时统计数量先增后减，先查 `Tasks/index.tsx` 是否把旧经营名单直接用于客户视角：经营名单按技能与客户组合去重，客户名单由接口聚合，计数口径不同。名单查询在绘制前进入加载状态；加载时保留缓存，但不渲染旧名单，四个汇总及总记录数显示 `--`。使用延迟接口响应验证首个客户视角绘制帧，不能只检查请求完成后的数量。
- 已完成列表不重复展示“经营任务”，技能名称统一在“经营场景”展示。客户清单使用自动表格布局，常规最小宽度 1000px、今日客户视角 1100px、今日经营视角 800px；不再通过固定指标列及大量屏宽断点抬高最小表宽。长姓名允许换行，最近接触时间保留单行与完整值 title。今日任务和待触达的最近接触时间、经营方案、执行依次吸附右侧，滚动时不得互相遮挡。经营方案列宽120px，位于执行列左侧；最近接触时间的right偏移为经营方案与执行列宽度之和。
- 客户指标通过 `components/CustomerMetrics.tsx` 展示，每项固定125px、横向间距10px；整列使用auto宽度，最小286px以预留两项指标及单元格内边距。内容区最小260px、最大395px，采用Flex自动换行，指标足够时每行2–3项；取消inline-size隔离，让指标数量参与表格固有列宽计算。经营场景通过 `components/CustomerScenes.tsx` 每行展示一个固定140px宽的背景项，短名称也保持相同宽度，默认两项；列宽由自动表格布局随内容尺寸收紧。两者超宽均省略，支持悬停或键盘聚焦查看全文、展开更多。经营机会内容不设固定最小、最大宽度，使用单元格可用空间，最多三行并支持Tooltip。样式入口为 `index.module.less` 的 `.customerMetrics`、`.metricGrid`、`.metricItem`、`.customerScenes`、`.opportunityPreview`。验证至少包含空指标、单项、六项以上指标及展开、长场景名称、1280/1440/1920px嵌入和普通视图；较窄容器允许表内滚动，不得产生页面横向溢出。
- 今日任务进入页面、切回经营视角或点击经营视角的刷新图标时，经 `store.ts` 的 `refreshTodayCustomers` 先查询最新规划，再用新规划查询当前视角名单。页内切到客户视角只调用 `loadTodayCustomers` 查询聚合名单，不重复查询规划。刷新期间禁用重复刷新和视角切换；已失效的选中任务回退到首个任务。不自动轮询，发布尚未完成时需完成后手动刷新。
- 浏览器标签页从隐藏切回可见时，工作台入口的 `useVisibilityRefresh` 调用 `refreshTaskLists`，每次直接后台更新今日任务（保持最近查询视角）、待触达和已完成名单及侧栏数量，不再先查询或比较客户数量；人数相同但客户或明细变化时也会同步。刷新期间保留当前名单和统计，不展示列表加载占位。仅客户经理且初始化/规划加载完成后生效，刷新期间忽略重复切回请求，已有名单请求进行中时不重复发起。iframe 内同样适用；宿主内部业务页签仅通过 CSS 隐藏 iframe 时不会触发该事件。回归入口：`useVisibilityRefresh.test.tsx`、`store.test.ts` 和 `Tasks/loading.test.tsx`。

### 财富规划分发目标

- 角色分流入口：`components/TargetPicker.tsx`。支行行长沿用原有平铺卡片和 `distributeTargets.ts::fetchDistributeTargets`，POST `/zt/wealth/wmadm/getUserPostionByBrn`，参数取登录机构 `orgCode/orgLvl`。本次机构树改造仅适用于分行中台。
- 中台勾选机构时同步切换人员视图，连续勾选展示最后一个机构，缓存命中直接展示；点击名称仅查看，不改变选择。取消全选或待选保留当前视图及搜索，待选取消后如仍需查看该机构则继续查询，但返回结果不重新选中。勾选切换机构会清空原人员搜索，并移除尚未发送且不再需要的旧纯查看请求，仍遵循同一批量队列。
- 零人机构的明确选择通过 `DistributionSession.emptyInstitutions` 保存，与人员 ID 分开；统一用 `isInstitutionSelected` 判定勾选状态。成功空名单勾选后显示“全部 0 人”，纯查看不选中，取消待选后的空响应不恢复勾选；取消、移除、清空及表单重建清理相应状态。发布只发送实际人员，不增加虚假目标；新上级名单使该机构变为非空时，清理零人标记且不隐式选中新增人员。
- 中台界面：`components/MiddleTargetPicker.tsx` 和同名 Less；左侧完整机构树不显示人数，右侧紧凑人员卡片，下方按机构汇总，父级覆盖子级时合并卡片。卡片最多每行五列，按容器宽度减少列数；人员区域使用 `@tanstack/react-virtual` 按行虚拟滚动，搜索和全选基于完整名单。方向键、Home/End 可在虚拟卡片之间移动焦点；边界位置不登记延迟聚焦，离开列表或搜索名单变化时清空待聚焦，防止搜索输入被卡片抢走焦点。
- 机构栏宽度在 `MiddleTargetPicker.module.less::.orgPane` 调整：桌面 300px，760px 及以下 220px，540px 及以下纵向占满宽度。人员列数由 `useCardColumns` 根据右侧实际容器宽度计算，不必为左侧加宽另写固定列数；布局验收需同时检查普通工作台和 `hideMenu=true&hideLogo=true` 嵌入页面。
- 人员卡片的 Tab 入口：原焦点卡片仍在渲染范围内时保留原入口；滚动卸载该卡片后，使用实际可见行的首张卡片作为唯一入口，不使用顶部缓冲行、不主动移动焦点。排查滚动后无法用键盘选择人员时，检查可见区域是否存在 `tabIndex=0` 的卡片，并验证 Tab、方向键、空格选择及返回顶部后的入口恢复。回归入口为 `MiddleTargetPicker.keyboard.test.tsx`，实际虚拟滚动还需浏览器验证。
- 新接口适配：`distribution/api.ts`，统一 request POST `/zt/wealth/wmadm/bbkTree`（完整树）和 `/zt/wealth/wmadm/orgSaps`（人员），最终路径为 `{API base}/api/zt/wealth/wmadm/*`；复用统一鉴权、上下文请求头、401 刷新和 60 秒取消超时。前端不提供服务凭证，不再调用 Python `/api/user-info/*/by-branch` 代理。
- 机构请求：`mmOrgCode=当前业务分行编码`、`mmOrgName=BBK_ID_MAP 对应名称`、`mmOrgLvl=Number(orgLvl)`、`sysId=登录上下文 sysId`。仓库分行映射仅为示例，部署环境需要完整映射。普通模式沿用登录上下文机构级别，运维模式根级别传数字 30，编码和名称均使用所选目标分行。
- 机构成功响应 `code="200"`，`data` 是根机构对象，以 `children` 递归展开；`mOrgLvl` 兼容数字与数字字符串。校验返回根编码与请求分行一致，末级依据 `children`，不依赖 `leaf`，中台不再使用 `121516` 例外或按“停用/私人银行/信贷部”名称过滤，由后端过滤可选机构。
- 人员请求：`{orgList:[{bbkOrgId,mmOrgCode,mmOrgLvl}]}`；`distribution/api.ts` 将每个未缓存的查询范围展开为本级和全部后代，并按机构编码去重，不能只传父机构。`bbkOrgId` 为当前业务分行，其他字段取各节点自身；分行根查询包含整棵树，父级查询包含完整子树，均为一次 POST、完整联合名单、无分页。`code="200"` 表示整批成功，任一机构失败则整批失败；非数组响应不能冒充空名单。
- 人员字段：`sapID` 保留前导零，`userName` 为姓名，`subBranchName` 优先于 `branchName`；`branchID` 为分行，实际机构优先取 `subBranchID`，无支行编码的分行本级人员使用 `branchID`。不额外按岗位或本地租户过滤，发布仍使用既有人员 targets 入参，最终去重由后端负责。
- 机构预加载与缓存：工作台 `index.tsx` 的 `useInstitutionPreload` 在身份就绪且角色为中台时异步加载，机构缓存可跨计划复用；缓存标识包含 `contextVersion`，即使同一批更新发生 A→B→A，也会重新加载，不会因为最终分行编码相同而复用旧树。新建计划只变更表单代次，不改变此缓存标识。运维模式必须先选分行；切分行、身份或上下文变化立即清空旧树、人员缓存、选择与在途请求回写权限；切回原分行也重新查询，不能显示旧结果兜底。
- 人员队列与缓存：`distribution/session.ts` 每份新建/编辑计划独立维护。已有完整上级缓存时通过 `distribution/tree.ts` 的机构索引筛出下级，不能反向把下级缓存当作完整上级。成功空名单也缓存，失败不缓存；切换计划销毁队列和人员缓存，但保留同身份分行的机构树。
- 人员查询成功不能重放已完成的全选意图：`pendingSelections` 只跟踪尚未完成的选择，纯查看结果仅更新缓存和已选人员信息，不扩大发布名单。上级完整名单成功返回后，先清除其本级与下级旧缓存，再写入新名单；下级按需从新上级缓存重新派生，避免“分行已查到新员工、支行全选仍漏人”。
- 批量合并：500 毫秒固定窗口，后续点击不延长；同一时间只发一个人员请求，新增查询排队。下一批的排队时间计入窗口，前一请求结束后已满 500 毫秒则直接发送；发送前移除被完整范围缓存覆盖或已取消的需求，合并父子重叠范围后仍由接口层展开完整子树，`orgList` 包含所有相应节点而非只有祖先。
- 父子勾选联动覆盖零人子树：完整名单成功后按最新选择意图标记所有空后代；加载期间对子级取消会排除该子树，响应不补回。取消零人子级也使祖先半选，补全父级可从缓存恢复，纯查看新名单不隐式全选。全选状态同时检查人员和零人后代，汇总在人数相同但机构未全选时显示“部分选择”。
- 机构汇总只记录实际完成的选择，等待或失败的全选不合并已有支行卡片；取消待选保留原分组，逐人或按搜索结果清空机构后清理对应分组。未匹配历史人员独立展示，不维持已清空机构的分组记录。
- 选择和失败：未选或半选点击补全，全选点击清空；全选失败后的半选点击重新排队查询，保留原有已选人员。等待或加载期间再次点击取消待选，保留已有选择，取消待选不会被旧响应重新勾上。某批失败不阻塞后续批次，失败机构支持手动重试或取消待选操作，保留已有选择。`distributionBlocked` 同时约束创建页发布按钮和 store 发布入口：等待、查询中及未解决的全选失败都阻止发布，单纯查看失败不永久阻止发布。
- 编辑回显：保留历史人员及机构编码；完整名单尚未加载前显示“已选 N 人”和半选，查询后再判断全部/部分。不在当前机构树的历史人员归入“未匹配机构”，只能由用户明确移除，不能自动丢弃。
- 旧的 `branchTreeInfo`、`fetchDistributeBranches` 及 Python 代理仅保留兼容，不用于新中台组件。部署网关需将两个新路径转发到原机构人员服务，服务端凭证和 Cookie 处理沿用既有部署约定。
- 回归入口：`distribution/api.test.ts`、`distribution/session.test.ts`、`TargetPicker.branches.test.tsx`、`TargetPicker.edit.test.tsx`、`Create/draftLifecycle.test.tsx`、`operations.switching.test.tsx` 和 `store.test.ts`。完整设计约定见 [分发目标选择器设计](../../docs/superpowers/specs/2026-11-03-wealth-distribution-target-picker-design.md)。

### Excel 名单分发

- 独立前端白名单 `wealthExcelDistributionUsers` 默认关闭，匹配真实登录 SAP 用户 ID。排查入口缺失时检查白名单和当前角色的选人权限，运维白名单不授予 Excel 分发权限。
- `distribution/api.ts::importExcelPeople` 通过统一请求上传原始 Excel，外部智拓服务解析并返回人员及异常行。前端校验 5 MB 文件上限、映射人员信息并按 ID 处理追加去重。联调时核对路径、上传字段、响应和授权规则。
- `Create/index.tsx` 隔离 Excel 和机构选人，发布时只取当前方式。外部服务的白名单授权和跨分行下发需要实测后启用。回归入口为 `Create/draftLifecycle.test.tsx` 和 `distribution/api.test.ts`。

## 财富任务列表后台刷新显示

- 浏览器标签页切回引发的自动刷新由 `taskListsRefreshing` 标记。任务页在此期间保留现有名单、分类和统计，不清空为加载占位；最新名单返回后更新。首次进入页面、切换任务视角/经营场景和手动刷新时，保留表头并使用 `Tasks/CustomerListSkeleton.tsx` 展示五行与当前列对应的骨架，统计保持 `--`；本地筛选不触发骨架。骨架行对辅助技术隐藏，由独立状态文案提示加载，减少动画偏好下停止闪动。入口为 `Tasks/index.tsx`，回归入口为 `Tasks/loading.test.tsx`，覆盖今日任务双视角、待触达和已完成。

工作台任务页顶部汇总随客户名单响应读取 `targetCusCount`、`todayTouchCount`、`sevenDayTouchCount`、`unTouchedCount`（外部响应 `data` 下，由前端适配到响应顶层）。今日任务经营视角使用所选当天技能的统计，其余视角/模块使用当天全部技能；搜索、标签和接触状态筛选只过滤表格，不重算后端汇总。各模块独立保存名单及统计并同步更新；首次加载显示 `--`，后台刷新保留旧值，缺失统计显示 `--`，当天无场景时显示 0 且不查询名单。列表分页条数仍按当前筛选结果展示。

今日任务经营视角原始客户名单为空且查询完成时，在当前任务标题旁展示执行时间；使用 `plans.items[].scenes[].cron_expr` 经 `mapSceneItem` 解析的排程，由任务树节点透传 `schedule` 并复用 `scheduleLabel`。日/周排程显示具体时分，小时排程显示“每小时整点”，自定义排程显示原表达式。搜索或接触状态筛选导致的空列表不触发展示；客户视角、待触达、已完成不显示。`plans` 不返回实际执行状态，空名单不能严格证明任务尚未执行。

## Stop 追加消息仅最后一条实时可见

- 入口：`src/swe/app/runner/stop_append.py::stream_stop_appended_messages`。每条追加消息必须使用结构化文本块 `[{"type": "text", "text": ...}]`，不能直接传字符串。
- AgentScope 实时流适配器会将裸字符串缓存到源生成器结束，新消息覆盖旧缓存；因此多条追加可能只显示最后一条，后续持久化失败时甚至一条也不显示，而历史中仍能恢复已提交消息。
- 回归入口：`tests/unit/app/test_runner_stop_append.py::test_live_adapter_publishes_each_append_before_next_save`，通过真实适配器验证多条成功追加逐条发送，以及第二条保存失败时第一条已送达。

## 保险核心跟踪入口与接口接入

- 名单加载时保留九列表头、colgroup 列宽和横向滚动容器，tbody 展示 `InsuranceTracking/InsuranceListSkeleton.tsx` 的五行九列占位，复用其他任务列表的 `CustomerListSkeleton.module.less`（包括减少动画偏好）。独立状态文案位于 `aria-busy` 容器外，骨架行对辅助技术隐藏；记录总数显示 `--`。加载结束后恢复客户行；真实空名单／错误重试／筛选空态保持独立，不在加载时提前出现。回归入口：`InsuranceTracking/index.test.tsx`。

- 跟进弹窗的客户姓名标签／姓名、经营场景标签／下拉框在同一行按两列等宽分配（扣除列间距后各 50%），垂直居中；加载／失败提示独占信息区下一行，不参与控件对齐，字段校验信息也不推动姓名和场景标签偏移；窄屏改为单列；“经营场景”为必选下拉，通过现有 `fetchSceneSkills("insurance", signal)` 调用 `/agent/workspace/skill-config/list`，按 skillId / senceName 构建保险场景选项；新增默认主记录 skillId/skillName，编辑默认单条 traceSkillId/traceSkillName，失败草稿优先。默认值未匹配时补入选项，异步加载／重试不覆盖用户选择；查询失败可重试，已知默认场景仍可保留提交。保存用户最终选择到 traceSkillId/traceSkillName，不改变主记录锁定的 skillId。

- “最后跟进记录”右侧为“触达结果”列，直接读取主表 touched（0 待跟进／1 已完成），支持全部／单个结果筛选，可组合意向度、搜索和排序，不从最新跟进方式推断。主表缺失结果显示占位且不匹配具体结果；触达方式仍在记录内容及编辑弹窗中保留。跟进保存乐观更新主表结果，写入失败同步回滚，回查成功同时回填记录与主表 touched。接触计划和跟进记录列的基础宽度分别为 180px、185px，首行以 14px 展示到分钟的时间与触达方式；保险计划列基础宽度为 100px，经营方案列为 95px。接触计划／经营方案单元格左右留白分别为 6px／4px，收窄后仍完整展示时间与方式／方案链接。执行列默认 180px，视口至少 1800px 时为 264px；表格最小宽度随执行列同步计算，分别为 1100px / 1184px，窄屏保留横向滚动；执行按钮左右留白固定 12px、按钮间隔 6px，局部样式覆盖全局宽屏内边距，保证宽屏 264px 列内三个按钮同排；新增、编辑、移除按钮使用纯文字，记录历史弹窗的编辑入口也保留明确文字；执行列移除按钮使用标准按钮和红色危险态，鼠标点击无 2px 外框，键盘仍保留 1px 内侧焦点提示。
- 日期和触达时间复用工作台 Ant Design DatePicker 样式，统一显式中文 placeholder、日历弹层挂到当前原生 dialog 内；初始缺失时默认本地当前日期／当前分钟，加入跟踪时每条新接触计划也默认当天。清空后仍进行必填校验；编辑已有日期不覆盖。时间组件提交本地 `YYYY-MM-DDTHH:mm`，由 API 边界转成带秒字符串，不转 UTC。

- “保险核心跟踪”入口复用 `treeItem.active` 的背景、左侧选中线及内边距，不覆盖普通技能的选中样式；选中该入口时隐藏经营／客户视角切换，切回普通技能后恢复，待触达和已完成页面的返回按钮不变。

- 客户意向度列头可单选筛选（全部及数值 0–3），与搜索组合且不改全量统计；筛选无结果保留表头和清除入口。资产与接触计划列头支持升序→降序→默认三态，切换列只保留一个排序；资产直接比较后端原始数值（兼容数值字符串），不做万／亿单位换算，接触计划按默认展示的最新一条日期排序，缺失或无法识别的值始终排末尾，同值保留原顺序。排序／筛选为前端全量名单处理，资产显示保留后端原值，字段为原始 aum，不在前端换算单位。

- 查询成功且全量客户为空（包括移除最后一名客户后）时，以“暂未添加跟踪客户”居中引导替代表格；保险分类有当日技能时显示“查看保险任务”，按现有菜单顺序选中第一个技能并加载名单。没有技能则隐藏按钮并显示当前暂无保险任务；搜索无匹配、加载和接口失败分别保留原状态，不显示该引导。

- 执行列提供次要“移除跟踪”按钮，原生弹窗确认客户姓名，在途禁用关闭与重复提交；服务端 code=200 后移除该行、清除该客户失败草稿并扣减跟踪与对应高意向人数，失败保留行和统计、弹窗内重试。POST `/agent/workspace/cust-trace/batch-delete` 携带 `items:[{sapId,bbkId,custUid}]`；bbkId 使用工作台请求侧二级分行，不能使用查询返回的一级分行；不在前端更改原技能名单 traced，按钮状态以后端名单接口返回值为准。

- 保险核心跟踪客户列表不分页，直接展示接口返回的全部客户；搜索仅过滤当前列表，底部保留筛选后的记录总数。顶部统计依次仅展示“跟踪客户”和“高意向客户”，移除重复的“客户数量”。
- “加入保险跟踪”弹窗局部为 input、select、textarea、button 提供 `:focus-visible` 蓝色轮廓，便于键盘定位，不改变控件常态样式。
- 今日任务经营视角在 `console/src/pages/WealthWorkbench/Tasks/index.tsx` 的保险分类首项展示固定入口；当天无保险场景时单独补分组。该入口不是技能，不加入 `buildTaskTree()`、`skillIds` 或原有任务角标统计。
- 保险列表复用工作台 `index.module.less` 的 `customerTable`、`tableWrap`、`taskSummary`、`summaryMetrics`、`contactActions`、`btn` 和 `link`；局部样式只维护列宽、内容折行和编辑入口，避免表头配色、字体、边框及执行按钮另起一套。
- 保险表格“客户姓名”列仅展示姓名，不展示支行；在途“保存中”状态放在执行列。保险计划、接触安排、沟通记录三个文本框统一限制 1000 字，展示实时计数及上限提示；原生输入上限与提交校验采用相同的字符串长度口径，历史超长数据保持原文，必须删减至上限后才能提交。
- 保险名单在“执行”左侧增加“经营方案”列，按钮复用原表格 `link` 样式和任务页同一个 `showScheme` 处理函数。外部 `filename` 在保险适配层映射至 `link`；有地址时使用原 `CustomerSchemePreview` 弹窗、每次打开附加 `_t` 并重新挂载，iframe 继续透传登录上下文；无地址显示“暂无经营方案”，不改为新窗口跳转；cust-trace 查询返回 filename；加入跟踪时从原清单 Customer.link 原样携带到 batch-create.items[].filename，不传预览弹窗临时追加的时间戳。原清单无地址时不构造字段，查询无地址仍保留空态。
- 页面、基础弹窗和接口适配位于 `Tasks/InsuranceTracking/`。`api.ts` 中 `API_PREFIX` 统一定义全部跟踪接口的服务前缀 `/agent/workspace`，`ENDPOINTS` 配置 `/cust-trace/` 下正式路径。`query` 发送当前客户经理及二级分行组合，解包 sapId 分组；跟踪人数为全量客户条数，高意向人数为 custIntention=3 的客户数。主表 bxPlan/custIntention 更新使用 update，仅发送 id 与本次字段。接触计划使用 touchList/touch*，跟进使用 traceList/trace*；跟进触达结果优先读取单条 touched，未返回时按已确认口径带入主表 touched，二者都使用 ext/add 或 ext/update，字段组互斥；时间在接口边界转换为本地 `yyyy-MM-dd HH:mm:ss`，不转 UTC。统一 `request()` 沿用今日任务服务的网关、鉴权和用户机构上下文。
- 客户意向度使用 number：0=待了解、1=低意向、2=中意向、3=高意向；按客户锁定在途提交，乐观调整高意向人数，失败只回滚该客户及对应人数差值。三类弹窗共用工作台原生 `dialog` 的标题、正文、底栏及按钮样式。保险计划使用文本框；接触计划维护接触日期、触达方式和安排；新增跟进展示客户及经营场景，并提交本地触达时间（不转 UTC）、方式、结果（0=待跟进、1=已完成）和必填沟通记录。新增记录不复用上一条内容；保存失败保留草稿，在途锁定该客户的编辑及意向度操作；字段与枚举按 cust-trace 文档适配。
- 跟进记录的触达结果使用 number：0=待跟进、1=已完成；触达方式使用 number：0=企微、1=招乎、2=电访、3=面访，新增默认企微（0）和已完成（1）。原生 select 的 change 事件在表单入口转为 number，意向度选项同样使用数值，列表渲染统一通过枚举映射中文，零值不按空值处理；接触计划与跟进记录统一使用“触达方式”名称及同一套 number 枚举。三类弹窗校验通过后立即关闭并按客户 ID 乐观更新对应字段，行内显示“保存中”；接触计划、跟进记录分别维护 `contactPlans` / `followUps` 全量数组（每条有稳定 ID），首次加载沿用接口返回顺序；默认各显示数组首条，正文优先展示，日期和方式为次要信息，正文与右侧 22px PencilLine 图标按钮顶部对齐，按钮高度保留 26px、左右内边距为 3px；编辑按钮保留悬停／聚焦提示及明确的 aria-label，与正文间距为 4px，正文占用剩余空间；保险计划与每条接触计划、跟进记录采用相同布局，日期和方式另起一行。底部左侧为无边框蓝色“＋新增”文字按钮，右侧将展开／收起与查看全部归为一组次要文字操作；展开／收起带对应方向的箭头，保持同一行左右对齐。单元格不设人为最小高度，操作跟随内容自然排列，与正文区保持 12px 间距，空态同样紧凑。展开显示最近三条，超过三条可通过“全部 N 条”在原生弹窗内阅读完整内容、编辑单条或新增；单条正文预览两行省略，悬停或聚焦可查看全文。新增在数组插入、编辑按记录 ID 替换，按实际记录时间倒序重排（分钟与零秒视为同一时刻），同时间优先展示本次新增／编辑记录；即时展示、单条保存回填及全量记录回查均遵循该规则，全量回查中新增记录按相对保存前数组新出现的 ID 识别。首次加载／手动刷新仍沿用接口返回顺序；ext/add 成功返回 null 后回查 query，取得正式记录数组及 ID 并只替换本客户的对应字段，避免覆盖其他在途修改；接触计划 ext/update 成功返回 null 后按已有 ID 保留本次值；跟进 ext/update 成功后同样回查并只同步该客户的跟进列表，及时更新从主表 touched 回填的结果，避免编辑其他记录时把旧状态写回。触达结果弹窗默认带入当前列表值，允许用户修改；记录保存不改动人数统计。失败只恢复该数组旧值，按客户、类型及目标记录（或新增）保留草稿；页面提供“重试保存”和“修改后重试”，正常编辑入口也会回填对应失败草稿，其他客户及已成功字段不受影响。失败提示使用单行布局，正文省略且悬停或键盘聚焦可查看完整原因，重试操作保持可见。失败草稿保留在当前页内存中。意向度继续使用独立的乐观更新逻辑。记录保存成功但回查失败属于同步失败，不回滚乐观内容，也不提供再次保存；标记该客户为已保存待刷新，禁止继续编辑，手动刷新成功后解除。API 不支持 requestId 幂等键；前端 UUID 仅作为临时记录标识，不传接口。网络响应失败后的新增重试不承诺服务端幂等，失败草稿仍按既有交互保留。
- 回归入口为 `Tasks/loading.test.tsx` 和 `Tasks/InsuranceTracking/index.test.tsx`。真实服务未联调前，页面可见及测试通过不代表服务已接入。

## 保险技能清单加入跟踪

- `Customer.traced` 原样接收名单接口的数值状态，在经营视角的保险分类技能中显示加入入口；0 可加入，1 显示禁用的“已加入跟踪”，缺失或未知状态不按 0 处理。其他产品、客户视角和完成清单不增加该操作。
- `InsuranceTracking/JoinTracking.tsx` 按客户姓名展示加入弹窗，资产从客户指标的资产／AUM 字段读取，缺失显示占位；客户姓名与资产各自标签和值同一行，超长省略并提供完整 title；各表单标题加粗，保险计划隐藏重复的“最多可输入1000字”默认提示但保留计数及超限校验，接触计划无“可按需添加”说明；弹窗挂载到工作台根节点，避免执行列的紧凑按钮样式穿透。
- `ContactPlansEditor.tsx` 独立维护多条接触计划，使用 Form.List 稳定 key 支持新增、删除与单条校验；每条按序号／删除、日期／触达方式、安排／文本框三层排列，各标签与控件横排，窄屏再换行；安排输入区随内容增高，保持 1000 字校验与计数；“触达方式”统一为 0 企微、1 招乎、2 电访、3 面访。保险计划必填，接触计划可按需添加；已添加的计划日期、方式和安排完整校验，所有文本域最多 1000 字。
- 加入使用 POST `/agent/workspace/cust-trace/batch-create`，`items` 单条携带 sapId、请求二级 bbkId、custUid、客户归属一级 custBbkOrgId（从原名单 bbkOrgId 直传）、custName、原始 aum、skillId、skillName、原清单经营方案 filename、bxPlan、数值 custIntention、原清单 touched（缺失默认 0），以及 type=0 的 extList（touchPlan/touchType/touchDate）。code=200 后检查 successCount/skipCount；已存在时提示已在跟踪中，不伪造新条目。
- 仅服务端 code=200 后标记该客户各保险技能的 traced=1，并同步当前／待触达／已完成名单中的保险条目，其他客户和产品不变；保险核心跟踪通过刷新键及再次进入时重新查询真实列表，不在前端伪造新记录或统计。
- 在途禁止重复提交和关闭，卸载中止请求；失败保留填写内容。不发送文档未定义的 requestId、posId、subBbkId 或来源字段；鉴权继续由统一 request 请求头承担。回归入口：JoinTracking.test.tsx、JoinTracking.api.test.ts、Tasks/loading.test.tsx、api.test.ts 和 store.test.ts。

## 财富工作台嵌入头部控制

- 父应用通过 iframe URL 的 `hideLogo=true` 同时隐藏整个财富工作台头部（品牌及标题）、信息播报与侧栏，例如 `/wealth/board?hideLogo=true`；未传或传 `false` 时正常显示。
- 入口与参数延续逻辑位于 `console/src/pages/WealthWorkbench/index.tsx`。内部导航省略参数时补回入口隐藏设置并保留其他查询参数，支持导航后刷新；不写入 sessionStorage，不影响独立打开的工作台。
- `.root.withoutHeader` 位于同目录 `index.module.less`，同步消除头部和播报占位，调整侧栏、正文与表单锚点偏移；全局 `MainLayout` 的 `hideMenu` 不控制该独立骨架。

- `hideLogo=true` 对所有角色统一隐藏整个侧栏（包含规划入口及任务导航）并移除占位，正文填满可用宽度；不带该参数时侧栏行为保持不变。内部导航仅延续隐藏设置，不额外补回 `origin=Y`。
- 父应用直达：`/wealth/board?origin=Y&hideLogo=true` 为看板，`/wealth/create?origin=Y&hideLogo=true` 为新建。部署在 `/console` 下时各地址加此前缀。
- `origin=Y` 初始化时若 URL 的 `hideLogo` 严格为 `true`，沿用前面已设置的初始上下文和导航参数，仅标记初始化完成后返回，跳过 `initFromUrlParams`：不查询客户信息、不初始化 Agent、不调用 `/auth/cron-auth`，也不启动客户信息刷新定时器。其他参数值沿用原流程；实现及回归位于 `console/src/utils/iframeMessage.ts` 和同名测试文件。
- 编辑仅由工作台内部触发：看板列表或日历操作调用 `editPlan(id)` 回填草稿和分发目标，再跳转 `/wealth/create`。父应用不提供编辑链接，不引入 `planId` URL 参数；编辑态沿用原有内存机制。
- 新建与修改共用页面，但侧栏「创建计划」先调用 `resetPlanForm()` 清空名称、场景、排程及分发目标，再跳转；即使已在修改页也执行重置。离开表单页同样清空状态，避免取消、发布或切换页面后再次新建沿用旧内容；修改入口仍由 `editPlan(id)` 回填。回归入口为 `Create/draftLifecycle.test.tsx`。
- 同路由新建也会更新 `planFormVersion`，重新挂载分发目标选择器，避免行长人员列表清空后不再查询，或中台保留旧支行路径/搜索条件。旧人员请求通过序号失效；发布返回仅在表单代次相同时回填状态并允许跳转，已切换表单时只更新规划列表，发布失败保留当前草稿供重试。回归入口还包括 `planFormIsolation.test.ts`。

## 财富规划经营方向输入框边界

- 创建计划第 2 步的输入框位于 `console/src/pages/WealthWorkbench/Create/index.tsx`，样式为同级工作台 `index.module.less` 的 `.direction input`。默认使用常显边框和白色底色，悬停、聚焦时加强边框；排查输入框难以识别时，先检查默认边框是否被设为透明，以及覆盖样式是否影响键盘焦点轮廓。

## 财富规划 Java 网关迁移

- 2026-10-20 浏览器规划路径统一增加 `/zt` 前缀；需同步网关 location 和测试夹具。Java 上游仍是 `/wealth/plans` 时由网关剥离 `/zt`；Java 同时有此前缀时原样转发。不修改工作台页面路由或其他接口前缀。

- 规划请求在 `console/src/pages/WealthWorkbench/api.ts` 使用统一 `request("/zt/wealth/plans")`，由全局配置拼接为 `{API base}/api/zt/wealth/plans`；规划业务码仍在适配层解包。网关负责转 Java；创建后的任务编排、资源处理与状态回写由 Java 负责。
- 列表通过 Query 传 sapId/role/bbkId；创建/更新通过 camelCase JSON 传身份和业务数据；删除通过 DELETE Body 传 sapId。响应解包 code/message/data，HTTP 200 的非 200 业务码也上抛。
- 场景、名单、统计、机构人员和支行树同样通过统一 request 拼接 API base 和 /api 前缀；仅 `/expert/get-sign` 按接入约定保留原生 fetch 与原路径；不回退 Python 工作台 handler。网关必须优先匹配规划接口并透传 DELETE Body，避免 SPA 返回 HTML。具体评估与配置见 [Java 网关接入](../../docs/api/wealth-plans-java-gateway.md)，回归入口为 `planGateway.test.ts`、`store.test.ts`。

### 财富工作台多渠道 iframe 消息入口

- 同一父页面标签页内，同源 CoPaw iframe 会共用 `sessionStorage["swe-iframe-context"]`。排查菜单 A 的 USER_DATA 参数残留到菜单 B 时，先检查 B 是否走 `origin=Y`：`main.tsx` 必须在注册消息监听器前调用 `resetIframeContextForEntry()`，通过 `clearContext()` 重置内存及持久化字段，再应用当前 URL/Cookie 身份与来源；即使用户相同或身份 Cookie 缺失，也不能沿用旧消息中的权限、自定义请求头、显示开关及来源。不能在等待外部 token 后的 `handleUrlOriginParam()` 中再次清理，否则会误删本次页面已收到的新消息；来源继续遵守 URL > 本次消息 > origin 默认值的优先级。清理只发生在入口初始化，内部路由导航保留当前上下文。回归入口：`console/src/utils/iframeMessage.test.ts`。
- 父应用 iframe 地址为 `/wealth/board` 或 `/wealth/create`（部署在 `/console` 下时加此前缀），不带 `origin=Y`。父应用在每次 iframe `load` 后发送 `USER_DATA`，使用子页面的明确 origin 作为 `targetOrigin`；不新增 READY 握手。
- 消息 `data.source` 必须为非空渠道字符串（本次为 `RCZT`，后续渠道直接传自身标识，无需修改子页面），`sapId`（兼容 `userId`）传非空字符串以保留前导零；传入 `userName`、`bbkId`（兼容 `bbkOrgId`）、`positionId` 以及业务需要的 `orgCode`、`subBranchId`、`orgLvl`、`sysId`。岗位缺失或未识别仍按原权限矩阵显示暂无访问权限。
- `hideLogo: true`（兼容字符串 `"true"`）隐藏头部、播报与侧栏，内部导航持续生效，不补 URL 参数。身份和布局由消息提供；接口鉴权仍使用现有登录 Cookie，消息传参并不替代登录态。
- 子页面在注册监听器时清除该入口残留的 iframe 上下文，收到父窗口的有效 USER_DATA 消息后才挂载工作台并加载业务数据。不查询用户信息、不初始化 Agent、不同步 cron-auth、不启动客户信息定时刷新。未收到有效消息时保持等待；每次 iframe 加载后由父应用发送一次初始化消息；切换用户或岗位时重新加载 iframe 后再发送。
- 规划创建/更新请求体的 `sourceId` 使用消息传入的渠道（例如 `RCZT`）。原 `origin=Y` 财富入口继续使用 Cookie 初始化及 `RMASSIST` 来源，其他已有页面的 USER_DATA 行为不变。
- 代码入口：`console/src/utils/iframeMessage.ts`、`console/src/main.tsx`、`console/src/pages/WealthWorkbench/index.tsx`。回归入口：`iframeMessage.test.ts`、财富 `index.test.tsx`、`planGateway.test.ts`。

父应用示例（监听器先注册，再设置 iframe 地址）：

```js
const frame = document.createElement("iframe");
const childOrigin = "https://实际子应用域名";
frame.addEventListener("load", () => {
  frame.contentWindow.postMessage({
    type: "USER_DATA",
    data: {
      sapId: "00800001",
      source: "RCZT",
      hideLogo: true,
      userName: "张三",
      bbkId: "100",
      positionId: "RB0101",
      orgCode: "ORG001",
      subBranchId: "SUB001",
    },
  }, childOrigin);
});
frame.src = `${childOrigin}/wealth/board`;
document.body.appendChild(frame);
```

## 运营看板用户详情聊天历史

- 列表与统计来自 Monitor `/api/monitor/tracing/sessions`，不以会话存档作为统计来源；日期/分行/资源筛选决定入选会话，不截断已选会话的聊天正文。
- 目标用户映射、详情、压缩归档入口在 `src/swe/app/routers/tracing.py`：`GET /api/tracing/chats?user_id=…`、`GET /api/tracing/chats/{chat_id}?user_id=…`、`GET /api/tracing/chats/{chat_id}/history?user_id=…&before=…&limit=20`。详情共用 Chat 的 `_build_chat_history(non_blocking=True)`，归档共用 `_archive_page`；不要改用当前用户的 `/api/chats` 接口。
- 权限沿用外部平台/网关现有运营看板访问控制，SWE 不新增角色或分行授权。部署须让新增归档路径经过与看板详情相同的规则；`user_id` 用于目标工作区定位，详情/归档还校验聊天归属及已有 source/Agent 元信息。
- 前端入口为 `console/src/pages/Analytics/BusinessOverview/components/UserDetailModal/ReadOnlySessionChat.tsx`。复用 Chat 的 useHistoryPreload 和 HistoryLoadStatus：初始显示最近 10 张卡片，先按每批 10 张展开本地历史，再按 20 条读取归档；倒序滚动容器进入顶部 240px 区域或内容不足一屏时自动推进，每轮最多三步，保留“加载更早消息”兜底入口及阅读位置。空当前快照但有归档时也会自动尝试补页。请求/分页状态按 source、目标 user、Agent、session/chat 隔离，只读，不订阅运行中流。
- 初始快照经 Chat 转换后必须由看板调用方补 `history: true`（含归档边界卡片），再交给 `mergeHistoryResponses` 合并。公共转换函数不自动加标记；遗漏时，跨压缩边界的一次回答会拆成两张卡。回归见 `ReadOnlySessionChat.archive.test.tsx`，使用真实共享转换/合并函数覆盖归档工具调用与当前工具结果的拼接。
- 计划历史只读展示：看板回答 renderer 保留原始 planReviewCard 供 PlanReviewSnapshot 展示，但传给 RuntimeResponseCard 的副本移除该字段以禁用交互。独立 PlanInteraction 保持为空避免双份计划；不得修改共享组件或把计划摘要/步骤一并隐藏。当前消息、归档页、任务分组均走该局部配置。
- “有会话卡片但无正文”先核对 Monitor 的 session_id 是否存在对应 ChatSpec，再核对目标 source/user/Agent 工作区和共享存储。映射缺失、详情失败和正常空内容使用不同反馈；归档失败保留当前消息和游标，可重试。Kubernetes 各实例必须访问同一持久化会话与 dialog 存档；已清理的文件不能通过本接口恢复。
- 回归：`tests/unit/routers/test_tracing_chat_history_parity.py` 固定存档对比两个入口的普通/任务/失败/v3 分轮消息和连续归档页；前端 `ReadOnlySessionChat.test.tsx` 覆盖归档、重试、去重和切换隔离。


## 财富工作台运维模式

### 入口与配置

- 前端入口白名单：`console/public/env-grey-control.js` 的 `window.__env__.operationsUsersByBranch`，当前为 `{ V00001: ["80008888"] }`。追加用户保持字符串编号，不转换为数字；修改部署后的配置文件并重新加载页面即可，无需重新构建。缺失、格式错误、空对象或分行的空数组均不开放入口，不支持 `"*"`；代码中没有隐藏的默认名单。
- 同时匹配真实登录分行与用户编号，默认关闭。分行候选来自 `console/src/constants/bbk.ts` 的 `BBK_ID_MAP`，排除总行 `100`；仓库中的列表有意省略了其他分行，不在运维代码中重复维护。
- `hideLogo=true` 不提供运维入口；进入后若头部被隐藏则退出模式。离开工作台、真实身份发生变化或刷新页面也退出模式。
- 白名单用户原岗位未识别时，普通模式仍显示无权限，仅额外提供主动开启运维的入口；其他未知岗位用户保持整页拦截。

### 状态和请求边界

运维状态仅存在工作台内存，不写 Cookie 或 sessionStorage，不覆盖 `iframeStore`。统一 `request()` 及 `buildAuthHeaders()` 保留真实登录身份，包括真实分行、岗位、用户及租户等请求头；只有财富业务参数使用目标分行和中台身份。

- 开启后先选择分行；未选时不挂载业务页面，不请求规划、场景或机构人员。
- 选定后规划列表、场景池和分发机构人员跟随目标分行，按中台角色选择支行与客户经理，来源为“分行关注”。
- `setOperationsMode()` 统一清理工作台状态，递增 `contextVersion` 并重挂载页面，清空统计缓存。列表、初始化、场景及任务请求检查代次，人员选择继续由请求序号隔离。
- 运维草稿不写入普通角色草稿缓存；切换前有草稿、编辑对象或已选人员时提示清空。发布/删除在途时禁止手动切换，身份变化或退出时仍必须清理旧状态，迟到写结果不能回写新环境。
- 切换后的导航立即执行，初始化只补充数据，不在请求结束后跳转，也不覆盖新输入的运维草稿。
- 编辑/删除依赖服务端明确返回 `editable=true`；运维模式中缺失值按只读处理，store 写入口再次检查。不扩大中台规划查询范围，不丢弃冲突校验所需的可见规划。

### Java 联调确认项

本次只改前端，不代表 Java 已支持跨分行授权。完整请求差异见 `docs/api/wealth-plans-java-gateway.md` 的运维模式章节。需要服务端同事确认：

1. 基于可信登录态校验真实分行 V00001、用户 80008888 及允许的目标分行；前端白名单不构成授权。
2. 允许授权用户的业务 role=middle、bbkId=目标分行与真实身份头不同。真实用户编号/姓名仍作为创建人，不能把规划归属写回测试分行，也不能把分发用户当创建人。
3. 列表返回正确的 editable；更新和删除独立校验本人代建归属、目标分行和来源，不能只相信 editable 或请求体。
4. 运维模式分行根的 `bbkOrgId/orgCode` 使用目标分行编码，业务上下文 `orgLvl` 固定为 "30"，根机构和根人员查询的 `mmOrgLvl` 传数字 30；`sysId` 沿用真实上下文。下级机构继续使用服务返回的 mOrgCode/mOrgLvl，普通模式层级取值保持不变。
5. 场景池、机构树、人员与统计服务接受授权的业务范围；鉴权失败沿用现有错误处理，不自动回退真实分行继续发布。

### 验证入口

```bash
cd console
npm run test:run -- src/pages/WealthWorkbench src/App.wealth.test.ts --maxWorkers=2
npm run typecheck
NODE_OPTIONS=--max-old-space-size=8192 npm run build
```

专项用例：`operations.test.ts`、`operations.api.test.ts`、`operations.ui.test.tsx`。真实浏览器使用模拟接口验证，不将其视为 Java 联调；Chromium 108 / Windows 360 仍需目标环境复测。

## 聊天「我的任务」标题省略与提示

- 常驻任务列表 `console/src/pages/Chat/components/ChatTaskList/index.tsx` 和侧栏展开面板 `console/src/pages/Chat/components/ChatSidebar/ExpandablePanel/index.tsx` 共用 `TaskTitle.tsx`。
- 标题保留各自的单行省略样式，仅在 `scrollWidth > clientWidth` 时提供完整内容 Tooltip；初次渲染、内容变化及 ResizeObserver 通知时测量，悬停不重复测量，不能用标题字符数判断是否省略。
- 回归入口：`console/src/pages/Chat/components/TaskTitle.test.tsx`，覆盖完整显示、宽度相等、溢出提示、尺寸及内容变化和点击透传。DOM 测量单测不替代真实 Chromium 108 与嵌入模式验收。

- 跟踪表单下拉统一使用 `InsuranceTracking/TrackingSelect.tsx`（Ant Design Select），选项浮层挂载到当前 dialog，枚举保持 number，经营场景保持字符串 ID。日期浮层宽度随输入框变化；列表保存保留禁用与失败反馈，不展示“保存中”；新增计划／记录使用浅蓝底按钮，执行按钮紧凑单行。弹窗仅保留 1000 字计数与到达上限提示。

- 接触计划与跟进记录的表格编辑图标统一先进入 RecordHistory 管理弹窗（单条也进入），完整查看后选中单条编辑；管理弹窗支持新增，继续复用原编辑及保存流程。

- 接触计划／最后跟进记录的正文不放编辑图标，默认仅显示最新一条；底部新增按钮与“查看详情”同行，详情承接全部记录浏览及单条编辑，不再使用表格内展开入口。

- 跟踪表格接触计划／最后跟进记录使用“时间 · 内容”单行预览，时间保留至分钟、正文超长省略；悬停或聚焦查看完整时间、方式与内容，详情弹窗仍完整展示。

- 跟踪列表单元格垂直居中；两列底部为“+ 新增”和“查看 / 编辑”。执行列使用两列网格，移除跟踪独占第二行；释放列宽给接触计划与跟进记录，表格总最小宽度不增加。

- 跟踪列表两列预览改为上下两行：上行时间（分钟）与触达方式，下行内容单行省略；列名为“跟进记录”，新增与查看／编辑使用工作台主、次按钮样式。

- 两列预览右上角使用与保险计划一致的无边框编辑图标，点击打开全部内容管理弹窗；底部主按钮恢复“+ 新增计划／+ 新增记录”。

- 跟踪列表资产仅在有说明时渲染 small，避免空块撑高；计划正文与触达结果沿用共享表格字号、字重及行高，预览时间继承表格字体；三处编辑入口共用 22px 按钮与 14px 图标。

- 两列操作统一放底部：蓝色新增按钮与白底灰边深灰字的铅笔编辑按钮同行；多条时显示“编辑 (N)”，单条仅显示“编辑”，Tooltip 说明查看全部并逐条编辑，管理弹窗仍保留总数。

- 执行列移除操作使用 UserMinus 图标与“移除”文案，保留红色按钮及确认弹窗；执行按钮以 flex 按实际列宽换行，视口达到 1800px 时通过局部 CSS 变量将执行列从 180px 调整为 240px，空间足够时同行，窄屏自然换行。

- 保险计划的编辑入口同样放在正文下方，复用 recordButton/manageButton 次按钮样式；点击直接编辑保险计划，不进入多记录管理弹窗。

- 保险计划正文固定预留两行高度并最多展示两行，短内容留白；高度包含相邻记录两行之间的 3px 间隔，空记录也保留相同预览高度，使三列底部按钮对齐。

## 智拓入口 JS 加载隔离

### 边界与定位

智拓对应 `/wealth/*`，也支持 `/console/wealth/*`。`console/src/App.tsx`
只在路由匹配后加载财富工作台、独立报告入口或 `ConsoleRoutes`。聊天、管理后台、登录、
会话分享及其 `DynamicRenderProvider` 放在 `console/src/ConsoleRoutes.tsx`，
避免智拓入口静态引用聊天模板引擎、Markdown、Mermaid 和分析图表依赖。
公共主题、Ant Design、路由和身份初始化仍属于共享依赖。

排查分享页浏览器标签标题时，先检查分享链接的 `source` 参数。聊天分享工具栏从
iframe 上下文取来源并写入链接，`BrandThemeContext.tsx` 仅在分享路由上使用
已知来源的 `brandName` 覆盖标签标题。参数缺失或无法识别时沿用原有品牌标题。

新增入口代码时不要在 App 中重新静态导入 Console 页面或聊天模板 Provider。
不要把 ConsoleRoutes 手动合并到首屏 vendor 包；文件拆分后仍须核对实际请求。
本次边界按平台划分，财富工作台内部的看板、创建与任务页仍属于同一加载单元。

### 复核方法

在 `console/` 执行生产构建并临时生成 manifest：

```bash
NODE_OPTIONS=--max-old-space-size=8192 npm run build -- --manifest --outDir /tmp/copaw-wealth-bundle
npm run test:run -- src/App.test.tsx src/utils/iframeMessage.test.ts src/access/consoleAccess.test.ts src/layouts/MainLayout/index.test.tsx src/pages/ChatShare/index.test.tsx
```

从 `.vite/manifest.json` 的 HTML 入口及财富入口递归追踪 `imports`，对文件去重后
累加 JS 大小。`dynamicImports` 不直接计入，另用真实浏览器核对运行时是否加载。
gzip 口径为逐文件压缩后求和，线上传输仍取决于服务器的压缩配置。

2026-11-06 本地结果（十进制 MB）：原主 JS 为 8.008 MB / gzip 2.432 MB；
隔离后的公共入口加财富工作台静态依赖共 3 个 JS，为 1.401 MB / gzip 0.448 MB，
gzip 减少约 82%。不含 CSS、图片、接口响应及运行时配置脚本。
这是本地依赖版本及构建环境下的结果，不保证其他安装环境具有相同 hash 或大小。

本地生产预览使用模拟父窗口 USER_DATA 与空列表 API，检查智拓看板及
`/console/wealth/board?hideMenu=true&hideLogo=true`，确认不请求 ConsoleRoutes。
不以模拟接口替代真实智拓业务联调；实际 Chromium 108 / 360 浏览器仍需现场验收。

验证记录：生产构建、TypeScript 检查、5 个相关测试文件共 65 项测试通过；
Chromium 148 的上述两个 iframe 路径均只请求公共入口、财富入口、共享事件模块
及运行时配置脚本，无页面异常和横向溢出。改动文件格式检查通过；ESLint 仍报告
App 原有的 `bailianTheme as any`（本次未新增）。额外运行的 54 文件财富工作台
全量测试超过两分钟仍未产生完成结果，已中止，不能计为全量通过。

### 经营方案独立入口

`/reportView`（含 `/console/reportView`）由 App 懒加载
`console/src/ReportViewEntry.tsx`，不再经过 ConsoleRoutes 或 MainLayout。
鉴权逻辑原样抽到 `console/src/access/AuthGuard.tsx` 供两类入口复用；
报告入口自行挂载模板 Provider 并调用 `initialize({ skipPreload: true })`。
`main.tsx` 的身份初始化、iframe 监听以及报告目录内部业务代码保持原样。

本次保留 FilePreviewModal 的静态引用，因此报告仍携带其 Markdown 和部分共享聊天依赖，
不能把“不加载 ConsoleRoutes”理解成“完全不加载任何聊天目录代码”。
Vite 构建的公共入口与报告静态依赖合计约 4.18 MB / gzip 1.28 MB，
模块清单确认不包含 MainLayout；这不是通过完整 TypeScript 校验的发布产物。
同步报告代码现有的 4 个类型错误仍阻塞标准构建，本次未修改这些业务文件。
入口隔离、鉴权、模板初始化及既有 iframe 等 6 个相关测试文件共 71 项测试通过。
Chromium 148 本地生产预览使用模拟静态报告接口和父窗口消息，两个报告路径均展示成功，
并收到 REPORT_HTML、REPORT_READY、REPORT_URL；未请求 ConsoleRoutes，未见页面异常或
横向溢出。真实动态报告、实际父平台与 Chromium 108 尚未验收。

后续按调用方要求对 SnapHtmlPreview 仅补充 Props 类型（包含 ref、docHtml、title、
onLoad、rawRecordData、resultId），保留函数体与现有运行逻辑。该类型声明不等于
React 18 的 ref 转发，也未修复重复页面、自引用或编辑器实现；不要把类型检查通过
当作编辑功能已恢复。模板 Provider 缺少的两个按名称访问方法由后续同步补齐。

报告、财富工作台与 Console 入口在 App 中共用 Suspense 占位。等待分包时立即
显示居中的入口图标、对应文案和细线加载指示。启用减少动态效果时细线保持静止。
