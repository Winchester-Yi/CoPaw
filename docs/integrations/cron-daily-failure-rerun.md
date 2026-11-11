# 新版定时任务：按来源补跑当日失败

## 部署范围

部署本次 Scheduler 和 SWE 代码，二者连接同一个数据库。Monitor 不改代码，前端不新增页面；旧接口、旧任务和人工立即执行不参与扫描。

1. 人工执行 `scripts/sql/cron_daily_failure_retry.sql`，仅新增一张调用表 `swe_cron_failure_retry_calls`，表名与列均带中文 comment。
2. 核对 `SHOW INDEX FROM swe_cron_execution_runs`，按 `scripts/sql/cron_daily_failure_retry_indexes.sql` 补三个不存在的索引。索引 SQL 不可重复执行；请遵循行内数据库变更窗口。
3. 先部署 SWE，再部署 Scheduler。混合版本期间先不要在外部平台启用本入口，避免旧 SWE 忽略补跑历史关联。
4. 使用已有新版后台及 `SCHEDULER_SWE_API_BASE_URL` 配置，不增加新环境变量、内部密钥、调用凭证或用户 Header。

应用启动不会执行上述建表或加索引语句。未建调用表时，新 POST 返回 503；新后台工作域退避诊断，不阻断已有任务派发。其他新版必需表与既有后台配置仍需就绪。

补跑按 Scheduler 的 `SCHEDULER_CRON_BATCH_MAX_ATTEMPTS` 和批成员当前指针判断自动重试机会是否已用完。该配置包含首次执行，缺省为 3；设置为 1 时关闭自动重试。关系无法确认就跳过。

本接口按确认不加鉴权、角色、来源白名单、限流或业务补跑次数上限。任何能到达接口的调用方均能触发指定来源下全部用户的候选补跑；来源参数不是权限控制。

## 外部平台 / Postman

### 发起一次扫描

`POST {{scheduler_base_url}}/api/scheduler/cron/failure-reruns`

Headers 只需 `Content-Type: application/json`，不需要用户身份 Header。

```json
{"source_id":"RMASSIST"}
```

Body 只接受 `source_id`，必填、最多 128 字符、不允许首尾空白或控制字符。不要加任务正文、用户身份或角色字段。每个被受理的请求都会生成自己的调用记录；重复触发不代表重复执行同一原失败。

成功返回 HTTP 202：

```json
{
  "call": {
    "call_id": "服务生成的UUID",
    "source_id": "RMASSIST",
    "status": "queued",
    "business_date": "2026-10-27",
    "window_start": "2026-10-26T16:00:00+00:00",
    "cutoff_at": "2026-10-27T02:00:00+00:00",
    "scanned_count": 0,
    "queued_count": 0,
    "skipped_count": 0,
    "failed_count": 0,
    "error_code": null
  }
}
```

实际响应还包含创建/开始/结束时间。北京时间当天 00:00 至受理时刻为固定窗口；按主执行失败时间或异步整轮失败确认时间筛选。受理后才入库或确认失败的执行等下一次调用。

外部平台由运维手动注册本 URL 和 Body；Scheduler 不代为注册。调用频率由平台决定，不增加补跑冷却期。

### 查询调用进度与逐项结果

`GET {{scheduler_base_url}}/api/scheduler/cron/failure-reruns/{{call_id}}?source_id=RMASSIST&limit=50`

返回 `call`、`items`、`next_event_order`。来源大小写严格匹配；来源不匹配或调用不存在返回 404。

有下一页时，把 `next_event_order` 传为 `after_event_order`，持续翻页至返回 null。`limit` 范围 1–200。持续观察已受理执行时，重新查询原页；事件游标只分页，不代表状态变更订阅。

调用状态：

| status | 含义 |
| --- | --- |
| queued | 等待后台处理、下一页或故障恢复 |
| running | 某实例正在持租约扫描 |
| completed | 扫描和逐项受理结束，不表示 SWE 业务全部完成 |
| completed_with_errors | 扫描结束，存在逐项处理异常；其他项仍正常处理 |

`scanned_count = queued_count + skipped_count + failed_count`，统计的是窗口内逐条检查的失败执行，不是去重用户数。旧失败被更新记录覆盖时也会计入 skipped。

每个 item 包含完整任务身份、`decision`、`reason`、前一次执行、根执行、独立补跑次数和 `execution`。只有 decision=queued 有当前补跑 execution；其状态从数据库实时查询，不是事件产生时的快照。

```json
{
  "decision": "queued",
  "reason": "cron_daily_retry_queued",
  "previous_execution_id": "原失败执行ID",
  "root_execution_id": "补跑链起点执行ID",
  "retry_count": 1,
  "execution": {
    "execution_id": "新补跑执行ID",
    "status": "success",
    "settled_status": null,
    "error_code": null,
    "settled_error_code": null,
    "history_execution_id": 123
  }
}
```

上例主执行成功但整轮尚未确认完成，不可视为最终成功。最终检查 `settled_status`，异步子任务可能使其变为 error。

## 筛选口径与落库分工

- 每个完整任务身份只评估最新自动调度/补跑尝试。较新的成功、排队、运行或异步未收敛会阻止补旧失败；人工立即执行不参与最新一轮判断。
- 批父/批子只重跑失败成员自身，不重新分发整个批次，不影响成功成员。必须证明当前批关系有效且原生自动重试结束；不能只凭历史 `is_final=1`。
- 排除认证过期 `auth_expired`、`cron auth user_info is expired`、`workflow sapId does not match target user`、`技能任务结果不可用`；英文文本忽略大小写。
- 删除、停用、未读自动暂停、管理中/未就绪以及结果未知的失联执行跳过。仅批入口暂停但成员自身可执行，允许补跑。
- 同一原失败只能受理一条补跑，多次/多实例调用仍各有审计。新补跑明确失败后可再次被扫描，次数不限，跨日按新确认失败日期判断。
- 同一次调用受理的补跑按调用 ID 与任务身份稳定散列，将 `not_before_at` 分散到 `cutoff_at` 起 1 小时内；扫描处理延迟时，已过散列时点的补跑立即进入派发，不延后散列窗口。
- 最新定义生成新运行记录，任务 ID、会话、原逻辑轮次及原通知时间不变。入队不写假历史、不提前改 running 或清空旧结果。
- SWE 实际执行后新增 `swe_cron_executions`，在同事务把同链上一条历史 `is_final` 改为 0、新记录为 1。其他周期不动；新失败仍为 1。SWE 直接写共享 state 和结果，不回调 Scheduler。
- 历史 `meta.daily_failure_retry` 保存 `call_id`、`root_execution_id`、`previous_execution_id`、`retry_count`；运行元数据另保存冻结的原通知时间。补跑次数独立于批成员 `business_attempt`，看板后者仍沿用原业务尝试口径。
- 新调度成功使尚未开始的旧补跑跳过；已开始的执行不打断。沿用现有结果排序、报告、未读计数及自动暂停规则。

调用记录存在新表；逐项决策复用 `swe_cron_managed_dispatch_events`；实际工作、历史、状态分别仍在 `swe_cron_execution_runs`、`swe_cron_executions`、`swe_cron_job_states`，没有第二张明细新表。

## 预发布验证

1. 准备一个该来源当日明确失败的新版普通任务。POST 后确认快速返回 202；GET 最终 queued_count=1，补跑 `not_before_at` 位于本次调用的 `cutoff_at` 至其后 1 小时。任务列表中的定义、会话 ID 和调度时间不变。
2. 连续/并发 POST 两次：两条调用记录，但同一原失败只能产生一个补跑执行；另一调用应跳过。手动成功不能改变自动失败候选口径。
3. 等待 SWE 执行完成，用 GET 验证新 history_execution_id；通过现有历史接口或下列只读 SQL 核对原历史仍保留、同链 is_final 为 0/1、补跑 meta 和 state 更新。
4. 再制造补跑失败，多次调用超过三次仍可受理；变成功后再次调用不再补跑。次日只应扫描次日确认的失败。
5. 准备包含成功/失败成员的批任务：原生重试未结束时跳过；结束后只新增失败成员的补跑，不创建新的批回执、不重跑成功成员。单独暂停批入口仍允许有效失败成员补跑。
6. 分别制造三个排除错误、认证过期、删除/停用/未读自动暂停、运行失联，GET 应返回对应跳过原因、没有新补跑。
7. 主执行成功但异步结果未结束时不补；整轮明确失败后再调用才受理。补跑入队后有新的调度成功，验证旧补跑在 SWE 开始前跳过。
8. 多页扫描中重启 Scheduler，验证游标接续且没有重复补跑。单项处理异常仍处理后续项，completed_with_errors 可定位日志；扫描级异常 queued 并保存 scan_failed，60 秒后恢复。进程直接退出则等待最多 180 秒租约到期接续。
9. 未到原通知时点完成时不提前发送，已过时按既有通知巡检补发；新结果正常产生未读，达到未读阈值仍可暂停。不要用 completed 代替真实执行和通知验收。

只读 SQL 示例（替换占位值）：

```sql
SELECT * FROM swe_cron_failure_retry_calls WHERE call_id = '<call_id>';

SELECT execution_id, tenant_id, source_id, agent_id, job_id, status,
       settled_status, history_execution_id, dispatch_meta_json
FROM swe_cron_execution_runs
WHERE JSON_UNQUOTE(JSON_EXTRACT(dispatch_meta_json,
      '$.daily_failure_retry.call_id')) = '<call_id>';

SELECT id, job_id, tenant_id, status, async_status, is_final,
       is_read, read_at, error_message, meta
FROM swe_cron_executions WHERE id IN (<旧历史ID>, <新历史ID>);

SELECT last_status, last_run_at, last_error, last_scheduled_run_at,
       unread_execution_count, pause_reason
FROM swe_cron_job_states
WHERE tenant_id='<tenant_id>' AND source_id='<source_id>'
  AND agent_id='<agent_id>' AND job_id='<job_id>';
```

## 日志、监控与回退

搜索 `当日失败补跑`、`补跑扫描故障` 和 call_id。正常调用先记录受理，GET 计数持续推进；原有执行派发与容量看板仍能看到新运行。异常日志包含脱敏异常链、错误号和栈位置，不打印认证信息、SQL 参数或任务正文。

重点关注长期 running、queued 不推进、`cron_daily_retry_scan_failed`、`cron_daily_retry_items_failed`、新补跑派发失败、数据库锁等待和重复业务执行。预发布由发布人覆盖至少一轮外部平台触发、业务完成及原通知时点；先低频小来源验证再启用真实来源。

异常时先暂停外部平台的重试定时器，停止新增调用；已受理执行仍按既有队列继续。不要直接删除调用行或改 is_final。需要回退代码时，先停入口并等已受理补跑收敛，或按已有运维流程停用目标任务；避免混跑不识别补跑标记的 SWE。保留审计表及索引供排查，不要求回退时删表。

本地测试使用真实 SQLite 事务适配验证接口、并发、恢复、队列与 SWE 历史链，不代替行内 MySQL 索引计划/行锁或外部平台、模型、通知联调。
