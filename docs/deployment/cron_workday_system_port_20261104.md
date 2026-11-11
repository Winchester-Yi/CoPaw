# 2026-11-04 工作日、通知与系统任务迁入说明

## 范围

迁入本地 v1.0.0 的七项能力：新任务对外查询、完成通知日历过滤、工作日执行与表单、精选案例创建、系统调度、来源设置加载修复、CLI/内置 cron 技能。

在 `codex/cron-task-tracking` 的 `ba57565b6` 基础适配；不是覆盖主工作区文件，也不包含后续业务解耦、聊天只查新任务、Workflow 配置路由拆分、存量接管或统一接口日志。

## 新任务的工作日规则

- 创建、更新时使用 `spec.schedule.run_on_non_workday`，缺省 false。自动普通和批调度会过滤实际非工作日；true 允许普通排程在非工作日执行。
- `nW` 和 `LW` 本身是工作日选日规则，即使开关 true 也不把这些规则改成任意日期。
- 保留五段 Cron、末尾年份六段 Cron；年份支持单值、列表、范围。`0 9 LW * * 2027` 可以经过 Scheduler 定义校验、SWE 准备校验、平台候选注册和未来时间预览。
- 列表、详情、子任务和对外查询的预览同时受年份、工作日、可执行日期段约束；不同日期段不能复用同一份预览结果。
- 保留现有实际北京时间执行区间、父子日期继承、维护接口次日预开、人工停用和队列约束，不拿明天日期提前许可今天执行。
- 批调度按父任务原计划的逻辑日期判断一次；提前执行日不等于业务日，成员不再二次散列或跨日重新判断工作日。已有日期区间门禁仍按当前分支规则执行。
- 手动 `/run` 不增加工作日过滤，但不绕过原有日期、停用等校验。

示例（创建请求的 spec 内）：

```json
{
  "schedule": {
    "type": "cron",
    "cron": "0 9 LW * * 2027",
    "timezone": "Asia/Shanghai",
    "run_on_non_workday": false
  },
  "run_date_range": {
    "start_date": "2027-01-01",
    "end_date": "2027-12-31"
  }
}
```

完整新建/修改请求仍需原有任务正文、版本和请求号，示例不是完整请求体。

## “非工作日不通知”对新任务是否生效

会生效，但这是独立的来源配置，不是任务执行开关：

```json
{
  "cron_notifications": {
    "skip_weekend_zhaohu_enabled": true
  }
}
```

该来源开关默认 false。新版 SWE 内部执行接口沿用已修复的来源配置绑定，按已鉴权 source 加载配置快照；后台执行及结果提交继承该快照，不因 HTTP 已返回而回退默认值。设置变更针对后续取得新快照的执行，不追改已开始执行的快照。

| 场景 | 完成通知行为 |
|---|---|
| 自动 agent/workflow 成功，过滤开，原定通知日为非工作日 | 历史写 `notification_status=not_required`、`notification_due_at=NULL` |
| 自动 agent/workflow 成功，过滤开，原定通知日为工作日/调休工作日 | 写 pending，沿用原定通知时间 |
| 来源过滤关闭 | 不查询通知日历，沿用现有通知规则 |
| 手动 agent/workflow 成功 | 不受工作日过滤，通知到期时间为本次完成时间 |
| text、失败、删除任务 | 不新增成功招呼通知，保持现有范围 |

任务允许非工作日执行，并不代表来源允许非工作日通知。例如任务的 `run_on_non_workday=true`、来源过滤=true 时，假日可以执行成功并留下未读结果，但不进入完成通知队列。

### 实际链路

1. SWE `ExecutionWriter.complete()` 在结果事务外取得通知日历，判定的是原定通知的北京时间日期。
2. 时间计算保留批任务原计划时间、通知延迟、分发提前量和当日失败补跑保存的 `notification_at`，不按提前执行当天误判。
3. 历史、运行状态和未读照常提交；过滤通知不清除成功结果、预览、未读，也不改变业务重试的 `is_final` 更新。
4. Monitor 的异步子任务汇总只更新 `async_status` 和 `need_notification`，不把 `not_required` 改回 pending。
5. Monitor 领取要求成功、异步成功、`need_notification=1`、`notification_status=pending` 且已到期，因此被过滤记录不会因子任务后来结束而重新通知。
6. SWE 通知 worker 仍负责实际发送，新任务从共库加载定义；本轮不迁通知发送服务，也不移除 CronManager 的发送方法。

### 不要误解的边界

- 这是定时任务完成的招呼通知规则，不是聊天输出、任务过程中任意工具消息或全部渠道的统一禁发开关。
- 判定原定通知日期，不是每次实际发送日期。工作日到期但因积压/失败延迟到周末的通知，发送端不重新按周末过滤；本轮不改变这一口径。
- 已有 pending 历史记录不会因为上线或现在打开开关而批量变为 not_required。
- 日历不可用会按普通周末规则降级，不能保证降级期间识别法定节假日和调休；结果应观察 fallback 标记。
- 没执行的任务本来就没有成功通知。验收通知过滤时，需要隔离执行开关，不能把“任务因假日被跳过”误当成“通知过滤已验证”。

## 部署前置条件

匹配发布 Scheduler、SWE 和重新构建的 Console。本轮无需改 Monitor 业务代码。

共用数据库需具备以下表；由负责人手动执行带中文注释的 SQL，应用不自动建表：

| 表 | SQL |
|---|---|
| `swe_workday_calendar` | `scripts/sql/cron_workday_calendar.sql` |
| `swe_cron_system_jobs` | `scripts/sql/cron_system_tasks.sql` |
| `swe_cron_system_executions` | `scripts/sql/cron_system_tasks.sql` |

核对既有服务互访配置 `SWE_SCHEDULER_API_URL`、`SCHEDULER_SWE_API_BASE_URL` 和平台注册配置；新增的 `SCHEDULER_WORKDAY_API_URL` 模板包含 `{date}`。dev/prd 文件提供默认模板，但必须确认是目标环境真正可达的日历地址；默认值不代表行内生产连通性已验收。不增加必配内部 Token。外部工作日接口另需行内 SM3withSM2 鉴权配置和 SDK，见 [工作日接口鉴权部署说明](cron_workday_api_auth.md)。

系统任务上线需停用旧心跳、梦境、清理和归档平台定时器，再核对新绑定。旧系统回调返回 410；旧用户任务回调不切换。新系统任务通过工作区启动或保存配置按需登记，不全量扫描补建。系统任务保持原五段平台排程，不套用用户实际工作日日历；原 W/LW 字段保留平台语义，系统接口不接受末尾年份，避免静默丢年。

CLI 只操作 Scheduler 新任务；已有工作区的内置 cron 技能需要通过原升级机制更新受管副本，不覆盖用户改过的技能。旧任务不会因本次发布自动迁入。

## 预发验证与观察

由部署负责人完成匹配发布后的工作日与非工作日两组验证，涉及真实平台的调用只在测试任务上进行：

1. 新管理页创建和编辑普通五段、年份六段、W/LW，反复开关非工作日执行，确认年份和日期区间不丢失；保留原基本配置/调度设置页签及父子日期限制。
2. 用 `POST /api/scheduler/cron/calendar/resolve` 查询实际节假日和调休日，检查 source/preview_source 与 next_run_times；列表、详情和新对外查询核对相同定义的区间边界。
3. 选允许非工作日执行的测试任务，来源通知过滤设 true，通过真实自动调度或合法已注册的回调入口执行，确认成功、未读仍在，历史通知为 not_required。不要用手动 `/run` 验证这个拦截，因为手动执行有意豁免。
4. 关闭来源通知过滤后，以新执行验证 pending；打开过滤后在调休工作日验证可通知。已有 pending 记录不作为本次开关验证对象。
5. 批任务验证逻辑日期、提前量及通知延迟跨日场景；异常补跑保留原通知日期，不重新散列内部成员。
6. 系统配置保存后观察同步状态、失败重试、新平台回调及 SWE 共库终态；确认旧系统入口不再触发，新系统任务不混入用户任务列表，原业务结果看板仍可读。
7. 验证精选案例只走 Scheduler 创建，CLI 能查询操作回执、保留绑定和未改字段，不回退 SWE 旧写接口。

可以只读查看测试任务历史：

```sql
SELECT id, job_id, status, async_status, need_notification,
       notification_status, notification_due_at, is_read, is_final
FROM swe_cron_executions
WHERE job_id = '<测试任务ID>'
ORDER BY id DESC
LIMIT 10;
```

健康信号：无非法年份错误、工作日查询符合受控日历、结果提交成功、被过滤历史不被领取、系统同步完成且只有新入口实际触发。失败信号：持续 fallback、SWE prepare 422、系统同步失败、旧新定时器重复触发。发现系统切换异常先停用对应新测试入口并核查在途执行，不能把 unknown 直接改成功；回退保留表和审计，不自动恢复旧定时器。

真实行内日历/平台、网关和 Chromium 108 验收与本地验证分开，不能以本机测试代替。
