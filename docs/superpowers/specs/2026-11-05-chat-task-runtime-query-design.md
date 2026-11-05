# 聊天任务运行状态轻量查询设计

日期：2026-11-05，Asia/Shanghai。状态：已按确认方案实施，代码随本工作包提交；自动化验证完成，真实环境验收待进行。详见[实现报告](../reports/2026-11-05-chat-task-runtime-query-implementation.md)。

承接[任务来源共享快照](2026-11-05-chat-task-source-snapshot-design.md)及其[实现记录](../reports/2026-11-05-chat-task-source-snapshot-implementation.md)。此前共享快照工作包与本包一并提交。

## 1. 范围与需求

保持已有 schema、索引、存储格式与原 HTTP/API 契约。将聊天页可见期间每 30 秒的完整任务查询改为已加载任务的轻量批量状态查询；会话翻页继续不查询任务。

外部创建、名称、内容和排程修改在返回聊天页、恢复可见或手动刷新时通过原完整查询感知；本页动作与已读完成后继续立即确认对应来源。运行中、暂停、未读、最新结果、首次结果后的消息刷新，以及未来三次运行时间保持现有表现。

本包不迁移旧 Cron 定义到 MySQL，不重构执行和写入协议，不加入任务推送、Redis、全局快照表或任意 TTL。无现成延迟/吞吐目标，先以实际依赖、请求量、读取量和兼容回归验收，再进行部署测量。

## 2. 核对后的权威数据和入口

| 入口/数据 | 当前路径 | 设计含义 |
| --- | --- | --- |
| 周期刷新 | `chatTaskSourceStore.startLifecycle → refresh(poll) → loadTaskSource` | 目前两来源仍读完整定义；此前只去掉了分页和重复消费者的额外查询 |
| 旧列表 | `crons/api.py::list_jobs` | 全量 `list_jobs`，逐项确认/补齐绑定，计算排程，序列化完整 spec/state/task；有条件写入副作用 |
| 旧单条 state | `get_job_state → get_job → get_state` | 仍校验文件中的任务，且只有 CronJobState，不含完整未读和结果视图，不能循环调用替代批量查询 |
| 旧任务持久字段 | `manager._record_task_success_in_jobs_file`、`_mark_task_read_in_jobs_file`、`task_view.py` | 未读、结果时间/预览、暂停等在 `jobs.json.meta`，不能把数据库投影视为实时权威 |
| 旧即时执行状态 | `CronManager._states` | 仍是实例内存语义；本包不宣称把旧运行中/终态变成多实例共享事实 |
| 旧 repository | `JsonJobRepository.load/get_job` | list 总是重读；get_job 按 mtime/size 缓存且逐次检查，不能据此宣称列表已消除 NAS IO |
| 新列表 | `ManagedCronRepository.page → _rows → list_item/job_view` | 读取 `d.*`、解析 spec_json 并清洗定义，最后生成状态/任务视图 |
| 新任务权威 | `swe_cron_jobs_v2` + `swe_cron_job_states` | 已有完整身份、版本和展示标量，可纯 SQL 查询，不读 jobs.json 或 Chat 正文 |
| 前端消费 | `taskJobs.ts`、`taskMessageRefresh.ts` | 排序、未读、暂停、动作可用性、最后运行时间和新结果刷新均需更新，不能只查询 is_running |

结论：会话元数据迁移到数据库，不代表旧 Cron 的定义和状态也已迁移。Scheduler 可以零 NAS；旧 Cron 要兼容跨实例可见的文件更新，仍需版本检查，文件变化时至少重读现有文件。JSON 没有按记录随机读取格式，返回 200 条不代表只读 200 条的字节。

## 3. 方案比较与推荐

| 方案 | 收益 | 代价/问题 | 选择 |
| --- | --- | --- | --- |
| 定义低频完整查询，运行状态独立批量查询 | 少传定义；Scheduler 纯 SQL；旧 Cron 热命中只查文件版本 | 要区分定义事实和状态事实，补齐文件快照正确性 | 推荐 |
| 原列表短 TTL 或逐条 state 查询 | 改动少 | TTL 难兼顾失效；逐条查询放大 NAS 元数据访问；无法完整刷新未读/结果字段 | 不采用 |
| 两来源全部直接读数据库 | 可统一查询 | 旧状态镜像并非现有完整权威；需要迁移写协议或存储格式，越过已确认范围 | 不采用 |

推荐逐段实施：先补齐旧文件快照正确性和批量投影，再增加两套只读接口，最后切换前端周期刷新。任何来源未就绪都保留原完整查询；不交付空壳轻量接口后在内部调用完整列表。

## 4. 新增接口契约

新增只读 POST，Body 用于批量 ID，不产生业务写入：

- SWE：`POST /api/cron/jobs/-/runtime-statuses`
- Scheduler：`POST /api/scheduler/cron/jobs/-/runtime-statuses`

两者使用独立的两段后缀，避免覆盖合法旧 job ID；注册及测试均检查旧 `-`、`runtime-statuses` 等 ID 仍可通过原路径访问。旧 GET 列表、详情、单条 state 和所有写接口完全保留。接口不需要新 DDL。

请求：`{"job_ids":["job-a","job-b"]}`。每次原始数组长度 1..200，ID 按现有规则校验、最长 128 字符；先限制原始长度再去重，大小写不折叠。不接受跨 tenant/source/agent 的 Body 目标；Header 捕获完整请求身份并沿用可信网关约束。前端无 ID 时不发送请求。

响应按去重后的请求顺序，每个 ID 恰好一个结果；不能漏项、重复或携带未请求 ID：

```json
{
  "items": [
    {
      "id": "job-a",
      "status": "ok",
      "enabled": true,
      "state": {"last_status": "success", "last_run_at": "2026-11-05T01:00:00Z", "last_error": null, "next_run_at": "2026-11-06T01:00:00+00:00", "next_run_times": ["2026-11-06T01:00:00+00:00", "2026-11-07T01:00:00+00:00", "2026-11-08T01:00:00+00:00"]},
      "task": {"visible_in_my_tasks": true, "chat_id": "chat-a", "session_id": "session-a", "is_running": false, "is_paused": false, "pause_reason": null, "auto_paused_at": null, "has_scheduled_result": true, "latest_scheduled_preview": "结果已更新", "last_scheduled_run_at": "2026-11-05T01:00:00Z", "unread_execution_count": 1}
    },
    {"id": "job-b", "status": "unavailable"}
  ]
}
```

Scheduler 的 ok 项另外携带 `definition_version/management_version/state_version/management_status/dispatch_state`；legacy 不伪造这些数据库版本。两来源使用各自 typed item，不返回 text/request/dispatch/model_slot/任意 meta 或完整 spec。定义字段只在服务端用于授权和纯展示计算。

- `ok`：授权存在且所需状态可用；state/task 字段完整，null 有明确值语义。
- `unavailable`：不存在、已删除或在当前列表权限范围内不可见，统一结果不泄露差异。不能凭一次响应直接替换全来源 membership。
- Scheduler `state_missing`：定义授权存在但缺少状态行，只返回 ID、状态标签和 definition_version；不填 ready、0 未读或 false 运行中。
- legacy `binding_missing`：授权定义存在但缺少聊天绑定，只返回 ID 和标签；新接口不修复、不创建 Chat。由前端触发一次原来源完整确认，保留旧接口的绑定补齐语义。
- 422：请求结构或容量非法；400：身份冲突/非法身份；503：数据库、文件或状态无法可靠读取。错误不返回成功空数组或旧缓存冒充本轮成功。

新接口限制 `last_error` 为 1024 个 Unicode 字符、`latest_scheduled_preview` 为 256 个字符，并提供对应 `*_truncated` 布尔标志；旧完整接口内容长度和响应语义不变。列表只消费摘要，查看完整失败详情继续使用旧详情/执行记录接口。

## 5. Scheduler：一个批次一条显式 SELECT

在 managed 查询模块新增 runtime 查询，不复用 `_rows` 的 `d.*`/job_view 路径：

1. 从 `CallerIdentity` 解析身份；当前工作区列表的权限过滤和二进制精确匹配沿用现有实现。manager 仍受工作区/source/agent 限制，普通用户仍受 creator 限制。本接口不是跨工作区的源任务子项管理查询。
2. 定义表按完整身份及参数化 ID 集合过滤，LEFT JOIN 状态表；只选必要标量。正文 JSON、任意 meta 和执行历史都不读；无逐 ID SELECT、COUNT、FOR UPDATE、外部 HTTP 或工作区初始化。
3. 计算 task 视图仍使用当前暂停/日期规则；未来三次排程沿用现有 Cron、时区和日期区间计算，所需 `cron_expr/timezone/run_start_date/run_end_date/enabled/creator_user_id` 都来自已有标量列。禁止退回解析 spec_json。
4. 由同一 SELECT 判断授权存在和缺失状态行，再补全请求 ID 对应的 unavailable 结果；不为查不到的 ID 查询其他租户或旧表。

目标是每批一条业务 SELECT 和零 NAS；连接健康检查不计入业务语句数。不承诺只扫描请求行数，必须用真实 MySQL EXPLAIN 确认现有索引、collation 条件和 IN 谓词的执行计划。

`state_version` 不足以判断响应是否未变化：排程展示和日期暂停可能随时间变化但无写入。版本用于拒绝倒退，不作为跳过状态计算/合并的唯一依据。

## 6. Legacy：校验版本的只读文件快照

新增专用于运行状态投影的 repository 读取能力，不改变原 `load/list_jobs/get_job` 的公共返回契约。将文件持久字段的轻量不可变投影与即时 `_states` 分开：

- 当前 repository/path 只保留最新一份紧凑投影及签名，不缓存认证 Header，不建立无限历史/多身份映射。
- 签名包含文件存在性、device/inode、mtime_ns、ctime_ns、size；普通热命中每个 HTTP 批次一次路径 stat，没有逐 ID stat/read。stat 仍属于 NAS 交互，不能说零 NAS。
- 未命中时后台线程打开文件，核对打开对象及读取前后 fstat，并确认路径仍指向同一版本；验证、解析、生成紧凑投影后才发布。读取中替换最多重试一次，仍不稳定则 503；禁止将旧内容关联到替换后的文件签名。
- 同 repository 的同时 miss 共用一个读取任务；迟到旧加载不能覆盖已发布的新投影。取消等待者不取消其他等待者，开始的线程负责最终关闭 FD；未验证或失败的结果不能更新成功签名。
- 投影只保留授权/任务归属、enabled、Cron/时区、绑定和 task 白名单等标量，不暴露可变 CronJobSpec 或 meta 引用。需要兼容既有 `get_job` 直接返回模型，不能使调用方修改未保存模型就污染状态缓存。
- 先限制/验证请求，再解析并获得目标 manager；初次工作区初始化另计成本。热路径不触发 binding 修复、repo 保存、Chat/NAS 正文读取、模型配置加载或外部平台同步。
- `CronManager.get_state` 的当前状态在每次请求时复制，再纯计算排程；不能把 `_states` 缓存进文件投影，因为运行开始可能没有修改 jobs.json。
- 文件删除按文件 repository 原有“缺失文件为空”语义处理；权限、IO、解析和校验错误均为 503，不能复用陈旧成功投影作为新结果。

文件改变时依然需要读取和校验整个 JSON，最坏 O(文件字节数+任务数)，之后按 ID 索引输出 O(批次大小)。文件不变时只做版本检查和必要投影/排程计算。不同批次仍各自检查版本，但不应重复读取同一未变化版本。

NAS 属性可见性、inode/时间戳能力和原子替换的部署表现需要实际挂载验证；该缓存不承诺文件系统之外的线性一致性。legacy 的实例内运行状态依然不能代表其他实例的全局运行状态，这是现有边界，本包不通过查询 SQL 镜像掩盖它。

## 7. 前端：定义快照和周期状态分开

共享协调器仍是唯一读取所有者：

| 触发 | 查询与更新 |
| --- | --- |
| 首次进入、返回路由、恢复可见、显式刷新 | 原完整来源查询，重建定义、展示和完整 membership |
| 普通会话首屏/下一页 | 仅 summary，不发送任务请求 |
| 可见页面 30 秒周期 | 对已加载且需展示的任务及当前打开任务按来源收集 ID，200 一批，仅 runtime 查询；不做完整遍历 |
| 本页动作/已读结束、创建事件 | 保持上一工作包的失效与来源确认；取消旧 runtime/full 请求 |

每来源一个读取序列，full 与 runtime 不并行竞争；开始 full 会使旧 runtime 失效，full 在途时跳过该轮状态查询。两来源可以各有一个 HTTP 在途，每来源各批顺序执行，避免无界 Promise.all。

完整身份 key、epoch、source revision 继续用于所有响应和下一批启动检查；再捕获本轮已加载 ID 集合。一个来源所有批次完成并通过覆盖性校验后才原子更新该来源的状态；另一来源可独立成功。不把多批次当固定数据库快照，不新增跨页并发一致性要求。

状态成功只合并白名单，保留定义内容、名称、tagging 和 request_id/版本动作机制。相同字段保持原记录及数组引用；日期/排程等实际字段变化也必须更新。状态错误单独记录 freshness/error，不清空定义、未读或正在显示的消息，不把失败变成删除。

runtime 结果不生成或替换全来源 membership。遇到 unavailable、binding_missing 或 Scheduler definition_version 不匹配，本轮聚合后最多请求一次对应来源完整确认；若该确认失败，按既有 unknown 语义保留会话。state_missing 直接显示不可确认的错误，不循环触发不能修复数据的完整查询。Scheduler 版本倒退的项丢弃，不把缓存覆盖回旧版本。

任务结果时间/has_scheduled_result 变化仍触发已有消息刷新和重试；其余状态变化不重读会话正文。无新结果时，状态轮询不调用 summary、详情或 NAS 正文接口。

## 8. 兼容发布与降级

只新增路由/类型/查询方法，旧接口不缩字段、不改变数组与分页、不改写入副作用或状态码。状态字段只用于展示，绝不替代动作的服务端授权、版本核对和事实结算。

允许先部署后端、再部署 Console。新端点在旧服务明确返回 404/405 时，该来源在本次可见生命周期内回退原完整周期查询；返回聊天页/显式刷新后可再探测一次，不每 30 秒先失败再回退。401/403/503、取消、格式错误和部分响应不能解释为旧服务能力缺失，不可用全量重查掩盖错误。

此降级只保障新旧部署组合可用；回退时性能收益不存在，必须记录。切换成功后没有全量查询的周期性“保险轮询”。

## 9. 实施顺序与验收

1. Legacy repository：版本关联正确、同版本合并读取、不可变投影、线程和取消收尾。先测原子替换/相同大小相同 mtime 的替换、坏 JSON、权限错误、外部实例修改、未保存模型变化和并发读取。
2. 两套新接口：先做实际 repository/ASGI 契约测试，再实现路由；验证参数/权限、无写、完整覆盖以及旧路径兼容。Scheduler 用实际 SQLite SQL 关系用例及专用 MySQL 验收，不能只断言 SQL 字符串。
3. 前端适配：先测周期查询成本与字段行为，再切换 poll 分支；保留上一工作包的 full 查询、来源错误、身份隔离和消息恢复回归。
4. 联合验收后按用户指令提交本包及其依赖的 Console 共享来源工作包；不混入 dev.json、历史上线文档或其他无关变更。

| 场景 | 验收条件 |
| --- | --- |
| 0/1/199/200/201/401 个待刷新 ID | 空集合无 HTTP；最多 200/批，来源分别为 0/1/1/1/2/3 次；服务端超限输入在去重前拒绝 |
| 热文件、多个批次 | 同一未变版本只有初次解析，后续每批一次 stat；无逐 ID NAS 调用，无绑定或写入 |
| 冷文件或外部替换 | 已验证的新版本投影；不读错签名，不返回半轮结果，不掩盖全文件读取下界 |
| Scheduler 每批查询 | 一条显式业务 SELECT，无 d.*、spec_json、历史全表聚合、NAS、外部 HTTP 或写锁 |
| 任务定义大、状态小 | HTTP 不搬运 text/request/meta，错误/预览有明确截断标志；保留原详情可读 |
| 状态行缺失、定义不可见 | 区分 state_missing/unavailable，不能填假成功或暴露权限差异 |
| 日期跨天/下一次时间已过 | 版本相同仍正确更新日期暂停与未来三次排程 |
| 一批失败/重复漏项/错身份 | 该来源状态不部分覆盖；其他来源独立；不改变全清单完整性结论 |
| 动作中、A→B→A、隐藏/认证退出 | 旧回包和下一批均不能写入；恢复只启动一轮 |
| 新结果/仅状态变化 | 新结果沿用消息重试；仅状态变化零 summary/正文请求 |
| 新前端+旧服务 | 404/405 来源独立降级；401/403/503 不降级；不重复探测放大成本 |
| 旧客户端、合法旧 job ID | 原列表/详情/state/写操作的路径、字段、分页、错误语义与调用方式保持 |

部署验收另含真实 Chromium 108/360、当前浏览器、hideMenu=true、真实 MySQL EXPLAIN 及 NAS 系统调用/读取字节。自动化实现与验证已完成，但尚未运行这些真实环境验收，不给出时延承诺。

## 10. 自审结果

- 已区分 SQL 状态权威、文件持久字段和实例内 _states，不以镜像取代权威。
- 已区分完整 membership 与按 ID 状态结果；部分批次、错误和缺失状态行均不伪造完整集合。
- 未将 JSON 文件的返回条数误当读取字节上限；没有把签名 stat 宣称为零 NAS。
- 已纳入时间推导字段、缓存可变引用、读取/替换竞态、取消和跨版本部署，不以 TTL 代替一致性规则。
- 已按[实施计划](../plans/2026-11-05-chat-task-runtime-query-plan.md)完成两套新增接口和 Console 周期查询切换；此前共享快照包仍保留。最终复核补齐了需展示/当前打开任务筛选、调用者相关暂停语义和 Unicode 字符边界。
