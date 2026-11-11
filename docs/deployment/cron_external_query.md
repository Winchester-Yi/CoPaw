# 新旧定时任务对外查询分离

2026-11-04 迁入分支时保留了年份及日期区间：新查询预览同时满足年、工作日和实际执行日期边界；不同日期区间分别计算，详见 [本轮适配说明](cron_workday_system_port_20261104.md)。

本提交只提供 Scheduler 新任务对外查询和所需日历预览底座。
SWE 原查询继续只查旧任务；系统任务迁移、前端、用户执行准入和提醒改造不在本提交内。

| 数据 | 地址 | 数据来源 |
| --- | --- | --- |
| 旧任务 | SWE `GET /api/external/cron/jobs` | 原 jobs.json 与已加载实例状态，原逻辑不变 |
| 新任务 | Scheduler `GET /api/scheduler/external/cron/jobs` | 当前 tenant/source/Agent 下的新版定义、状态表 |

新接口 Header：

- `X-Tenant-Id`、`X-Source-Id` 必填，不能用 viewer 替代 tenant。
- `X-Agent-Id` 可选，不传固定查询 `default`，不读取 SWE active Agent。
- `X-User-Id` 可选，仅影响 `task.visible_in_my_tasks`。
- `X-Scope-Id` 可选，如传入必须与 tenant/source 一致。

返回完整数组，内部每批读取200条；只返回未删除且 `management_owner=scheduler` 的用户任务。
任务字段平铺，状态在 `state`、会话和未读在 `task`，兼容字段在 `meta`。
`meta.pause_reason` 与 `task.pause_reason` 使用同一份有效状态：保留已有暂停原因、人工停用及日期限制的优先级；未到开始日返回 `date_before_start`，过期返回 `date_expired`，未暂停返回 null。`enabled` 仍表示人工启用意图，日期暂停不把它改成 false；查询不回写状态或旧表。
数据库不可用或关键状态缺失返回503；无匹配任务返回空数组，不回退旧文件或内存。
新接口不依赖 SWE、不启动工作区，不写任务定义、运行状态或历史。
身份 Header 依赖可信网关约束，不新增必配内部 Token；不能将新任务ID交给旧文件任务写接口。

## 日历预览底座

复用现有 `swe_cron_jobs_v2`、`swe_cron_job_states`；共享日历表由负责人按
`scripts/sql/cron_workday_calendar.sql` 人工准备，应用不执行启动DDL。
`SCHEDULER_WORKDAY_API_URL` 延续dev/prd JSON与环境变量覆盖，模板需含 `{date}`。
接口正常结果缓存24小时，异常结果按周末规则兜底5分钟；上游2秒超时、刷新租约10秒。
新查询允许刷新日历缓存，其他业务数据不写入；缺表/上游异常会明确标记 `meta.calendar_source=fallback`。

支持LW、nW、非工作日过滤及分发提前量的未来时间预览；缺少 `run_on_non_workday` 时按false。
预览不代表平台已同步，也不表示此提交已经改变自动执行准入；执行迁移另行发布。

## Postman 核对

1. 仅文件有旧任务：SWE旧接口可查，Scheduler新接口不返回它。
2. 仅数据库有新任务：Scheduler新接口可查，SWE旧接口不返回它。
3. 新接口只传tenant/source查询default，显式Agent只查对应记录。
4. 超过200个新任务仍返回完整数组；viewer不改变任务集合。
5. 新接口断库/缺状态返回503，旧接口不依赖新数据库；查询不创建工作区或平台注册。

## 本地验证

候选提交目录中运行新查询、日历底座/配置、旧SWE查询和独立真实MySQL测试。
MySQL只使用测试创建的回环实例和随机数据库，不连接行内数据库；外部日历调用由受控替身提供。
测试、lint和钩子结果以提交时输出为准，不把其他未提交迁移的测试结果当作本提交证据。
