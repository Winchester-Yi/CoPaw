# 系统任务迁移：部署与验证

覆盖心跳、梦境、任务会话清理和归档维护。Scheduler 管调度、平台绑定及执行登记，
SWE 保留配置入口和实际业务动作，双方直接读写同一数据库，不回传最终结果。
不包含已取消的用户任务错误详情增强、来源设置页原有加载竞态修复或旧用户任务迁移。

## 数据和配置

由环境负责人在 SWE、Scheduler、Monitor 共用数据库人工执行
`scripts/sql/cron_system_tasks.sql`，新增：

- `swe_cron_system_jobs`：稳定身份、期望/生效配置摘要、平台绑定、同步状态和在途执行引用。
- `swe_cron_system_executions`：每次触发、SWE 受理和执行结果、围栏及人工核实审计。

SQL 带中文表列注释；应用只检查表结构，不自动 DDL。`CREATE IF NOT EXISTS` 不升级草案表，
已有表需对照字段及索引核对，不得 DROP 重建。旧表、绑定、业务记录和文件不自动删除。

配置沿用 `SWE_SCHEDULER_API_URL`（含 API 前缀）、`SCHEDULER_SWE_API_BASE_URL`
及既有外部平台地址、凭据、分组和回调配置。生产地址不能误用 localhost。
不增加必配内部 Token 或系统管理开关。业务配置仍从原 Agent/来源配置读取，非全量搬入新表。
SWE 会话/归档文件仍按原部署共享，原业务看板、查询接口和业务记录写入链路保留。

心跳、梦境按 tenant/source/Agent 独立；清理、归档按 source 独立，逻辑 tenant/Agent
固定空字符串，不能按最后修改配置的管理员拆分。系统任务不混入新旧用户任务管理列表。

## 上线切换

1. 核对两表、共库、共享文件权限和服务互访，准备匹配的 SWE、Scheduler、Console。
2. 运维记录并停用旧心跳、梦境、来源会话清理及归档平台定时器。
3. 配套发布新链路并重新构建 Console。新 SWE 对旧系统回调返回 410；旧用户回调保持原状。
4. 工作区启动或保存相关配置时按需提交新版刷新，不接管旧平台 ID/绑定，不全量扫描补建。
5. 验证配置保存、异步平台同步、实际触发、SWE 执行和共库终态。

冷工作区未启动且未保存配置时没有新系统任务，是预期按需行为。
Scheduler 后台只推进已登记意图及实际收到的触发，不自行补造调度周期。
平台注册名称带 `[Scheduler][系统任务]`，回调协议 `scheduler_system_v1`。

首次注册没有取得平台 ID 时如实 failed/未确认；平台无查询补偿，重试前人工检查孤立定时器，
不能承诺绝对仅注册一次。已知绑定更新失败可能仍以原生效配置触发，页面区分期望与生效事实。
保存关闭配置后，SWE 在业务开始前复核；即使平台暂停失败也不开始新动作，已开始动作继续。

## Postman 接口

管理请求沿用可信网关的用户、租户、来源、Agent 与管理员权限 Header。
系统平台回调使用注册时的固定 Body，不要求外部平台额外传 Header、流水号或原计划时间。
不要将 source 逻辑空身份替代平台注册 Body 中的路由身份。

```text
POST /api/scheduler/cron/system/jobs/refresh
GET  /api/scheduler/cron/system/jobs?scope_kind=agent
GET  /api/scheduler/cron/system/jobs?scope_kind=source
GET  /api/scheduler/cron/system/jobs/{job_id}
POST /api/scheduler/cron/system/jobs/{job_id}/retry
POST /api/scheduler/cron/system/callback
GET  /api/scheduler/cron/system/jobs/{job_id}/executions
POST /api/scheduler/cron/system/jobs/{job_id}/executions/{execution_id}/confirm-ended
```

刷新 Body 示例：`{"task_type":"heartbeat","request_id":"uat-system-refresh-001"}`。
类型依次验证 `heartbeat`、`dream`、`task_session_cleanup`、`archive_maintenance`。
202 仅表示持久受理，继续查询 `sync_status`、`desired_config`、`effective_config`、绑定和错误码。
同步失败重试传最新 job.version 作为 expected_version，加新的 request_id；网络未知续发原请求。
同步重试不会重新执行业务，也不会解除 unknown。

内部入口必须由网关/内网隔离，不向普通外部调用方开放：

```text
GET  /api/internal/cron/system/config
POST /api/internal/cron/system/execute
POST /api/scheduler/internal/cron/system/workspaces/refresh
```

业务 Header 和操作 ID 不能替代可信入口访问控制。

## 执行与人工核实

每次合法固定 Body 独立登记；同身份已有待派发、受理、运行或待核实时，后续触发 skipped，
不排队、不取消在途。传输重试复用 execution_id，SWE 获得数据库开始围栏后才能执行业务。
系统任务不使用用户批任务模型池或用户假期执行开关。

失联、运行中取消或超时不能解释为旧动作已结束，进入 unknown 并阻止同身份后续业务。
管理员实际核实旧进程及动作结束后调用 confirm-ended，Body 示例：

```json
{
  "expected_version": 4,
  "request_id": "uat-confirm-ended-001",
  "confirmed_ended": true,
  "reason": "已核实旧实例停止且业务动作结束"
}
```

这里版本取 execution.version，不是 job.version。解除仅允许未来正常触发，不伪造成功，
不重跑旧业务；迟到旧结果不能清除新的在途引用。禁止清表、批量改成功解除阻塞。
原设置页显示同步状态、刷新和重试；执行历史和人工核实使用独立接口，不改原业务结果看板。

## 维护动作

任务会话清理保留原来源维护范围，并补充读取新版数据库会话绑定，按原保留期裁剪会话文件。
不删除任务定义或 SQL 执行历史，不补写 jobs.json。必要时条件清除过期预览和未读，
通过会话锁、状态版本及日期条件保护并发产生的新结果。

新系统梦境不再顺带归档；归档作为独立来源任务执行，沿用原范围、文件数量和耗时限制，
不增加永久删除，不为归档初始化 Agent runtime，继续写原归档业务读模型。
部分维护失败记 partial，不冒充整体成功。维护仍枚举实际目标，不等于全量补建系统定时器。

## 目标环境验收与回退

验证保存成功但同步失败、已知绑定更新失败、关闭但平台暂停失败、重复触发/同 ID 重投、
SWE 共库终态、unknown 阻塞、人工解除及迟到结果保护。分别验收四种原业务动作和原看板。
验证清理保留近期结果及 SQL 历史；验证梦境不再附带归档，独立归档仍产生原看板数据。

本地测试及临时 MySQL 不代表行内平台和网关验收。回退先核实在途/unknown 并暂停相关新定时器；
不自动恢复旧注册器或旧定时器，确需恢复由运维按配置显式操作，禁止两套并行，保留所有表和审计。

## 2026-10-22 本地验证记录

- SWE 系统业务、跨服务 HTTP/共库结果和相关兼容回归 297 项通过。
- Scheduler 全目录及独立临时 MySQL 合计 737 项通过、1 项原有失败；
  `test_retry_history_cannot_replace_another_tenant` 报 `cron_service_url_not_configured`，
  在前置提交快照同样复现，未混入模型配置辅助修复。
- 临时 MySQL 两表 DDL、稳定身份、并发刷新、执行围栏、日历与清理 CAS 专项再次 6 项通过，
  与上述目录结果重合，不重复累计；没有连接行内或已有业务数据库。
- 前端相关回归 43 项通过、1 项原有来源切换加载提示失败；该失败在前置提交快照同样复现，
  本提交不包含独立的 pendingLoad 竞态修复，也未放宽原用例超时。
- 类型检查与生产构建通过。构建保留已有大 chunk/混合导入提示，JSDOM 保留伪元素提示。
- 候选快照修正测试替身签名、类型和资源关闭写法；旧 run_job 隐式 None 改为显式 None，
  系统传输重写参数名与父类对齐，超时判断改为等价链式比较，不改变既有业务结果。
- 全目录回归不是全绿；目标环境平台、网关、共享文件及旧系统停用仍需按上文验收。
