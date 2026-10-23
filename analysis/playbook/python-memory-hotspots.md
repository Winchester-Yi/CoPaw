# Python 后端内存热点与优化优先级

2026-10-16 后续状态核对（`7a70c78a3`）：下述“Tracing 数据库故障积压”应结合现有[有界 writer](tracing-bounded-writer.md)理解。`6d1616139` 已控制接纳、在途与重试占用，不再是旧基线的无界队列；剩余评估重点为超大单条、等待者、副本及故障背压。第三阶段草案已增加对应状态说明。

2026-10-16 当前复核见 [第三阶段与 GC 方案](../../docs/superpowers/specs/2026-10-16-memory-phase3-and-gc-design.md)。第一、二阶段已落地；下文保留为历史基线。尤其第 4 节的无界 SSE buffer/队列已被磁盘回放和多级预算替代，不能用于描述当前实现。当前重点是完整历史/分享的全量读取、Tracing 数据库故障积压、文件与 ZIP 峰值、目录快照体积及 Provider scope 生命周期。目录快照有数量上限，但尚需主动过期和字节预算；媒体下载已有大小/并发限制，不能泛称全部无界。存储 GC 必须以有效根可达性为依据，旧 epoch、已应用 journal 和旧 ledger 链节点不能仅凭年龄删除。

2026-10-15 补充：第一阶段设计见 [memory-phase1-design](../../docs/superpowers/specs/2026-10-15-memory-phase1-design.md)。用户已确认采用进程内修复，Redis 迁移另行设计；方案覆盖 summary Task/Context、SessionExecution 快照解除引用，以及临时文本缓存的主动过期和容量限制。下文是原始分析基线，不能当作当前代码全部路径的最新复核，也不表示优化已经实施。

分析日期：2026-10-11。源码基线：`659a8e3257c4ae63769e5e12c14318600d1d3651`。
GitNexus 绑定 `/Users/shixiangyi/code/Swe`（CoPaw 主工作区），索引提交与上述 HEAD 一致。

本次覆盖 Agent / Runner / Session、工作区与租户缓存、文件及媒体处理、SSE、Tracing 和后台任务的主要持有路径。结论来自调用图、源码及小型合成实验；未启动应用、连接数据库或测量生产 RSS。

排序针对多租户服务长期运行，综合可能的对象体量、持续增长、滞留时长及并发放大。它是优化优先级，不能当作实际内存占比排名；超大文件处理或数据库持续故障可能改变排序。

| 排名 | 优化内容 | 主要增长方式 | 典型表现 |
| --- | --- | --- | --- |
| 1 | 后台 summary Task 完成后仍保留请求上下文 | 不同总结会话数 × 各自会话快照体量 | 请求结束后对象仍存活，活跃工作区的内存基线升高 |
| 2 | 完整用户历史保留于 `turn_states`，会话及 checkpoint 日志全量处理 | 活跃请求各自历史体量之和；事件日志对象及序列化额外峰值 | 长会话越用越重，压缩在线上下文后仍未解决 |
| 3 | 文件、ZIP 和媒体全量读入与编码，缺少端到端预算 | 并发操作数 × 实际文件 / ZIP / 媒体大小 | 上传、导出、多媒体请求的突发峰值 |
| 4 | SSE 完整回放缓冲和无界订阅队列 | 活跃任务累计事件字节数 + 队列引用与容器开销 | 长任务、慢客户端期间内存持续上涨 |
| 5 | 全局临时文本缓存依赖访问时清理，缺少总容量 | 历史未消费 scope / session / chat 数 × 条目体量 | 已离开的用户仍占据进程缓存 |

## 1. 后台 summary Task 的生命周期与请求上下文

证据：

- [BaseMemoryManager](../../src/swe/agents/memory/base_memory_manager.py#L56) 将 Task 按会话 scope 保存在 `_summary_tasks_by_scope`；[创建位置](../../src/swe/agents/memory/base_memory_manager.py#L214) 调用 `asyncio.create_task()` 后加入列表，没有完成回调移除。
- 清理只发生于同 scope 下次创建 summary 时，或显式 [await_summary_tasks](../../src/swe/agents/memory/base_memory_manager.py#L289)。[ReMeLightMemoryManager.close](../../src/swe/agents/memory/reme_light_memory_manager.py#L450) 未回收这些任务。
- [SessionExecution](../../src/swe/app/runner/session.py#L314) 在事务入口读取整个会话状态，并把自身放入 `_ACTIVE_SESSION_EXECUTIONS` ContextVar。退出时[重置父上下文](../../src/swe/app/runner/session.py#L338)，但 `_state` 没有清空。

持有链为：工作区 → memory manager → 已完成 Task → 创建 Task 时复制的 Context → SessionExecution → `_state`。失败 Task 还可能通过异常 traceback 持有消息及模型对象。成功 Task 不必继续持有协程局部变量，就可以通过 Context 保留会话快照。

触发包括 `/compact`、`/new` 和自动压缩的 legacy fallback；并非每次 checkpoint 压缩都会创建 summary。该滞留可以持续到同会话下一次 summary、显式消费任务或相关工作区及对象被回收。活跃工作区处理许多不同会话时，已有 workspace LRU 不能限制其内部会话 Task 数量。

建议：任务完成时消费异常、移除 Task 并删除空 scope。若 `/await_summary` 需要历史结果，另存有界轻量结果。创建后台任务时只传播必要的租户、模型与 trace 上下文，排除会话事务；为在途 summary 增加并发和消息字节预算。

合成验证使用项目 `venv/bin/python`，通过 AST 提取实际 `BaseMemoryManager` 的创建方法，注入无网络 summary 和一个持有 1 KiB 数据的会话对象。Task 完成、父 Context reset 后 weakref 仍存活；清除 Task 引用后 weakref 失效。这确认引用保留机制，不代表已测得应用内存占比。

## 2. 活动会话保存全部用户正文，全量读取历史与事件日志

证据：

- [_user_turn_anchor](../../src/swe/app/runner/session_lifecycle.py#L42) 返回完整 `Msg.to_dict()`；[admit_user_turn](../../src/swe/app/runner/session_lifecycle.py#L183) 为每个 turn 将消息写入 `turn_states`。
- [终态更新](../../src/swe/app/runner/session_lifecycle.py#L66) 仅修改 status；保存普通会话时[保留旧 state](../../src/swe/app/runner/runner.py#L6479)，再替换其中的 `agent`。本次全 `src/swe` 搜索未找到历史 `turn_states` 的裁剪路径。
- [会话读取](../../src/swe/app/runner/session.py#L211) 使用整文件 `read()` 和 `json.loads()`。请求事务持有整个解析结果；序列化写入时又需要完整 JSON 字符串。

在线 Agent 历史压缩后，旧用户正文及消息中实际携带的数据仍留在 `turn_states`。附件如果只保存文件引用，不能按附件原文件大小计算；如果消息内含文本或 base64，则会保留对应内容。第 1 项还可能延长整个会话快照的寿命。

checkpoint 日志另有全量处理：[_read_checkpoint_events](../../src/swe/agents/memory/conversation_archive.py#L1243) 先创建完整事件列表，再[按 epoch / cursor 过滤](../../src/swe/agents/memory/conversation_archive.py#L393)。每次消息追加会多次读取事件日志，[append](../../src/swe/agents/memory/conversation_archive.py#L418) 再全量解析并由 [join](../../src/swe/agents/memory/conversation_archive.py#L1271) 生成完整日志字符串重写。reset 更换 epoch 不删除旧事件，删除 Chat 才移除对应目录。`message_added` 事件保存的主要是 message ID、role 与引用，并非完整正文；它通常是比 `turn_states` 更小的附加峰值和累计读写成本。

建议：终态 turn 的正文迁移到可按 ID / 页查询的归档，活动 session 保留必要状态、归档位置及有界近期记录；历史展示按需加载。checkpoint 日志按 epoch / 大小分段、读取未应用尾段，避免每次追加重建全部历史；改成追加写时必须保留现有锁与崩溃恢复语义。

必须保持现有的用户锚点、Stop、审批恢复、历史展示与共享会话契约。在线压缩[确实删除归档消息](../../src/swe/agents/memory/conversation_archive.py#L1676)，不能将本问题描述为“压缩只标记、不释放”。

## 3. 文件及媒体处理缺少端到端内存预算

证据：

- [工作区 ZIP 导出](../../src/swe/app/routers/workspace.py#L40) 使用 `BytesIO` 制作整个 ZIP，同时 `sorted(root.rglob("*"))` 收集全部路径。制作完成后才[返回 StreamingResponse](../../src/swe/app/routers/workspace.py#L151)，完整 ZIP 在响应期间仍驻留内存。
- [工作区上传](../../src/swe/app/routers/workspace.py#L208) 无上限 `await file.read()`，将全部 bytes 传给后台线程。解压有临时目录清理，但没有解决归档缓冲的内存峰值。
- [聊天附件上传](../../src/swe/app/routers/console.py#L2448) 先全量 `read()`，下一行才检查 10 MiB；拒绝超大附件之前已经分配全部 bytes。
- [Anthropic 本地媒体](../../src/swe/agents/model_factory.py#L122) 和 [OpenAI 本地视频](../../src/swe/agents/model_factory.py#L182) 全量读取并 base64 编码；后者再构造 data URL。D 字节资源形成约 `4D/3` 的 base64 字符串，转换时还有临时 bytes。
- 下载层对超过 10 MiB 本地媒体会报错，但[预处理异常分支](../../src/swe/agents/utils/message_processing.py#L391) 只替换 `file` block，image / audio / video 的原 block 可继续进入 formatter。预处理只扫描顶层媒体，嵌套工具结果还需在 formatter 边界统一检查。

源码已存在媒体下载 10 MiB 上限、4 个工作槽、URL 分块写盘及清理，不应泛称整个媒体下载过程无界。缺口在接口全量读取、失败后的 payload 保留，以及编码阶段缺少统一预算。

建议：工作区导出写临时磁盘 ZIP，使用 `FileResponse` / 固定块读取并在结束时清理；上传使用 spool / 文件流并累计检查字节限额。限制 ZIP 大小、条目数、展开总大小及并发。媒体校验失败即替换 / 移除对应 block；formatter 对顶层及工具结果统一执行单资源、单请求预算，允许时优先使用文件引用或 URL。

一个已经压缩好的大 ZIP 仍可使单次导出持有接近其大小的内存；多并发和慢客户端下载会延长并放大峰值。具体上限应依据实际工作区及业务媒体体积分布设置。

## 4. SSE 完整回放缓冲与无界订阅队列

证据：

- [TaskTracker](../../src/swe/app/runner/task_tracker.py#L34) 每个运行任务持有 `buffer: list[str]`。
- [订阅](../../src/swe/app/runner/task_tracker.py#L131) 和[初始队列](../../src/swe/app/runner/task_tracker.py#L216) 创建没有 `maxsize` 的 `asyncio.Queue()`。
- [广播](../../src/swe/app/runner/task_tracker.py#L228) 将每个 SSE 字符串加入完整 buffer，再 `put_nowait()` 给所有订阅者，没有回放字节上限或慢订阅者处理。
- [finally](../../src/swe/app/runner/task_tracker.py#L252) 删除 run；stream 结束也会 detach。因此当前问题主要发生于活跃长任务和排队输出，并非所有已完成任务永远泄漏。

buffer 与各订阅队列通常持有同一字符串的引用，不能简单计算为“订阅人数 × 全部正文副本”。但完整 buffer 会使已消费的旧事件也保持存活，慢客户端还积累队列容器与引用；断线重连会把全部 buffer 引用放入新队列。

建议：回放按总字节而非仅事件数设置环形窗口，结合序号 / cursor 或磁盘事件日志保持重连契约；订阅队列有界，并定义慢客户端的断开 / 重同步行为。考虑合并高频可替代事件。给跨 chat 的活跃任务设置入口容量，避免状态、buffer 和媒体同时叠加。

合成实验提取实际 `TaskTracker`，注入 200 个约 4 KiB 事件和慢订阅者：buffer 长度与初始队列待消费数均为 200；重连队列与 buffer 的事件字符串是同一对象；producer 完成后 run 被删除。

## 5. 临时文本缓存缺少主动过期回收与全局容量

证据：

- [console_push_store](../../src/swe/app/console_push_store.py#L20) 为模块级 scope 字典，[append](../../src/swe/app/console_push_store.py#L182) 原样保存 text。每 scope 确有限制 500 条，但 60 秒 TTL 只在读取时清理；无全局 scope 数或字节预算。无人轮询的历史 scope 可长期保留最后一批文本。
- [suggestions store](../../src/swe/app/suggestions/store.py#L18) 与 Q&A store 为全局字典。同 session 建议覆盖为单条 entry，但历史 session 总数无上限；Q&A 条目只在相同 chat 再访问时过期回收。
- 明确缺陷：[_prune_expired](../../src/swe/app/suggestions/store.py#L149) 返回新列表，调用方 [peek_suggestions](../../src/swe/app/suggestions/store.py#L143) 和 take 忽略返回值，peek 既不真正过滤返回结果，也不释放过期数据。take 仍会删除整个 session。

Q&A 使用提取后的短文本，已有上游长度限制，不能将其当成完整对话副本。主要风险是历史键数量和 Console 推送文本字节数随服务运行时间增长。

建议：修复过期过滤返回值，主动全局扫描过期项并删除空键；为临时缓存设置总条数 / 总字节预算及 LRU，并对推送大文本采用有界摘要或持久化引用。缓存清理继续保持现有租户隔离键语义。

合成实验提取实际 peek 和过期过滤函数，注入一个已过期 120 秒的建议：peek 仍返回 1 条，原缓存仍保留 1 条，确认该过期路径缺陷。

## 其他会放大内存的路径

| 路径 | 证据与判断 |
| --- | --- |
| 跨 Chat 并发与子 Agent 进程 | [AnswerTurnCoordinator](../../src/swe/app/answer_turn/coordinator.py#L78) 只合并同 Chat 活跃请求，未见工作区总请求 admission。等待 LLM 限流时已加载 session / Agent；子 Agent 配额是每 tenant + agent，独立 Python 进程会叠加 Pod RSS。建议在读取大状态前设置进程级入口容量，子进程原子预留总配额。未核查外部网关限流。 |
| Tracing 数据库持续写入失败 | [_flush_spans](../../src/swe/tracing/manager.py#L1257) 将失败 spans 放回队列，只有成功才删除 `_pending_spans`，没有重试积压容量。默认批量 100、5 秒 flush 是触发阈值，不是硬容量；输出有默认脱敏截断。持续 DB 故障时可能迅速升为最高优先级。建议有界批次、积压字节预算及磁盘暂存，明确审计保留 / 降级契约。 |
| Provider scope 实例缓存 | [ProviderRuntimeCache](../../src/swe/providers/provider_runtime_cache.py#L30) 按 scope 永久保存实例；freshness TTL 控制配置刷新，不淘汰实例。主要是配置与模型清单，没有模型权重持有证据。可加 scope LRU / idle TTL，并清理关联元数据和完成 Future。 |
| checkpoint 日志与 token usage 文件 | 前者见第 2 项。TokenUsageManager 每次记录 / 查询也[全量读取](../../src/swe/token_usage/manager.py#L129)历史聚合文件，但按天 / 模型聚合，通常弱于逐轮完整正文；长期大目录可考虑按日期分区。 |

MultiAgentManager 已有默认 16 个 workspace、1 小时 idle TTL、启动并发 4 和清理循环，不能报告为“无限 workspace 常驻”。本地模型管理入口已[退役](../../src/swe/app/routers/local_models.py#L169)，不将本地模型权重列为当前前五。备份 ZIP 写临时磁盘文件且有并发限制，弱于工作区导出 `BytesIO` 路径。

## 运行时验证与验收建议

已有 [memory-diagnostic](../../src/swe/app/routers/runtime.py#L25) 和 [memory-type-holders](../../src/swe/app/routers/runtime.py#L48) 可观察对象类型及直接持有者；诊断自身会扫描对象，宜低频采样。结合 process RSS、Pod / cgroup 内存及 allocation profile，区分 Python 对象、原生缓冲、子进程和文件页缓存。

1. 固定工作区内持续创建不同会话、触发 summary、等待完成：确认完成 Task 数与 SessionExecution 对象能回到稳定水平，不随历史会话持续增长。
2. 长会话增长后触发压缩：检查 `turn_states` 正文总量、session JSON 大小和每次请求峰值；验证 Stop / 审批 / 历史展示 / 分享仍保持正确。
3. 用可控的合成 ZIP / 媒体测试不同体积及并发：超限输入应在形成完整大 bytes / base64 前被拒绝；处理缓冲应受预算约束。
4. 对长 SSE 人为制造慢订阅与重连：回放字节和队列保持有界，重连行为明确，完成或取消后引用释放。
5. 给不同 scope 写入瞬时消息后停止轮询：等待过期扫描，确认条目与空键回收；模拟 Tracing 写失败验证积压预算。

RSS 不一定在对象释放后立即回落，因此验收应同时看存活对象、引用持有链、分配峰值与长期 RSS 曲线。第一阶段已实现并通过专项回归，尚未进行应用级压力测试或生产 RSS 测量。


## 第一阶段实现记录（2026-10-15）

已按[批准设计](../../docs/superpowers/specs/2026-10-15-memory-phase1-design.md)实现：Summary 完成移除 Task、轻量有界结果、后台 Context allowlist、关闭等待/取消；SessionExecution 退出解除快照引用；推送、suggestions、Q&A 写入预算和主动过期；lifespan 每 60 秒分批清理。Redis 迁移后续单独设计。上文热点证据描述优化前基线，当前以实现和设计参数为准。

- 推送：TTL 60 秒，16 MiB / 10,000 条，单 scope 2 MiB / 500 条，单条 256 KiB，超大完整拒绝。
- suggestions：TTL 60 秒，2 MiB / 2,000 session，单 scope 256 KiB / 200 session，单条最多 10 条建议且 16 KiB。
- Q&A：默认 TTL 120 秒，6 MiB / 5,000 对，单 scope 1 MiB / 500 对，每 chat 50 对，单条 64 KiB。
- Summary：每 manager 完成记录 TTL 300 秒，256 条 / 512 KiB，单 scope 10 条，展示文案 1 KiB；manager 通过弱引用注册。

预算计入保存的键及正文 UTF-8 数据，另用条目数约束容器开销，不等于精确 RSS。淘汰按写入顺序，读取不续期。清理接口与 stats 均提供聚合计数。

联合回归 165 passed、9 skipped；另一个既有租户内存 fake-backend 测试失败在原基线可复现，排除后 165 passed、9 skipped、1 deselected。新增测试验证 1,000 个 scope、引用释放、取消、并行等待、UTF-8 与容量、跨 yield 覆盖、真实清理装配和关闭错误路径。Black/mypy/flake8 通过；完整 pre-commit 的 pylint 仍有原有压缩分支 metadata 参数诊断。

关闭入口失败或 Summary 取消超时仍 pending 时跳过最终 reset；现有上层服务停止错误传播未全面重构。GitNexus 方法查询 UNKNOWN、类关联存在不可靠跨语言数据，detect_changes 的 CRITICAL 提示已记录，未视为空影响。生产验收还需观察 pending/completed、payload_bytes、expired/evicted/rejected_total、cleanup_errors_total 及 Pod 长期内存曲线。


## 第二阶段实现入口

[第二阶段设计与操作说明](../../docs/superpowers/specs/2026-10-15-memory-phase2-design.md)覆盖checkpoint分段追加、session终态turn冷存储及迁移回退。两种新格式默认关闭；已有新格式根继续按自身版本读写。使用`swe memory-storage`预览、显式迁移、完整校验及旧格式导出，不能把迁移前备份当作含新写入的回退结果。

故障检查先看`state-v2.json`及quarantine，session看根revision/generation与`.turn-ledger/<generation>/pending.json`。不要手动删除pending：下一writer根据根提交点恢复；预览不会执行恢复。冷正文和reference缺失会报错，二者同时丢失需完整validate以计数检出。新格式恢复必须使用空目录，旧shell脚本主动拒绝。生产NAS锁/rename/fsync/硬链接及跨Pod退出恢复仍是启用门禁。

`journal export`用于将v2日志导出为旧格式；正常legacy归档会被明确拒绝，源文件保持不变，后续仍可追加。格式判断必须先于v2根完整性读取：缺少`state-v2.json`不等于legacy数据损坏，不能因此写入quarantine。已有隔离标记需先核实原因，不能直接批量删除。


## 第二阶段运行指标

[指标口径与使用说明](../../docs/superpowers/specs/2026-10-15-memory-runtime-metrics-design.md)补齐现有diagnostic_flow的`performance_metrics`：`session_memory`提供已发布v3 session热终态/保护状态的条数及UTF-8字节分布；`storage_operations`提供session/checkpoint提交、恢复检查、迁移及预览的耗时和结果计数；`counters`提供固定操作的`*_failures`。

窗口每类最多2048条数值，无tenant/session/msgid/正文标签。热状态last只表示最近观测的单次提交，不是进程全部会话常驻总量；保护终态同时计入terminal和protected，不能把两者相加。失败累计在进程重启后清零；取消单列，preview失败和实际迁移失败分开。提交耗时在线程真实执行阶段计时，取消等待者不提前终止统计；指标采集异常不覆盖存储业务结果。不据此直接推算RSS。
