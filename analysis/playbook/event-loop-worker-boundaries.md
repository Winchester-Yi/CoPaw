# 事件循环与 worker 执行边界

适用于工具输出、checkpoint、Provider 配置和 S3 备份相关的延迟排查。阶段 A 设计见 [专项方案](../../docs/superpowers/specs/2026-10-13-event-loop-hotspots-design.md)。

## 当前入口

| 工作 | 异步入口 | 执行位置 |
|------|----------|----------|
| 即时及历史工具输出压缩 | `compact_tool_result_messages_async` | 独立输出 executor，当前进程级最多 8 个已提交任务（用户调整） |
| Shell 流式落盘、完整输出解码及格式化 | `run_shell_spool_work` / `run_output_work` | 与压缩共用输出 executor；Unix pipe 每 4 KiB 读流仍在 loop |
| 前台 Shell 命令、环境、路径守卫与进程限制准备 | `_prepared_shell_execution` | runtime-state worker 准备并清理守卫；租户执行许可及子进程启动留在 loop |
| 请求内存绑定 | `ReMeLightMemoryManager.prepare_request_memory` | 独立 runtime-state executor，最多 8 个已提交任务，Chat 锁内仅读 epoch |
| Query 和辅助模型配置/TLS 准备 | `prepare_model_config_async` / `create_model_and_formatter_async` | Provider executor；SDK 客户端仍在 loop 构造 |
| Query 配置、Prompt 文件与来源工具目录 | `_create_agent_for_query_async` / `rebuild_sys_prompt_async` | runtime-state worker；Agent/Toolkit/在线消息在 loop 安装 |
| S3 构造、分页及选择 | `run_backup_io` | 独立备份 executor，进程级最多 4 个已提交任务 |

## 取消与并发规则

- 已提交的线程任务不能由协程取消停止。必须在线程真实完成时释放并发许可；提前释放会使运行任务数超过预算。
- 提交前等待容量，避免 ThreadPoolExecutor 的内部队列无限积累。等待协程数量仍受请求并发影响。
- 通过 `copy_context()` 传播请求的租户、来源和 tracing 上下文。
- shield 后的 Future 要消费异常，否则请求取消后工作失败会产生 `Future exception was never retrieved`；正常 await 仍应收到原异常。
- 输出压缩在 worker 内复制内容，返回 loop 后应用。取消不应用结果；没有变化的内容保持原对象。请求应独占消息内容，不能在 worker 执行时原地修改同一内容。
- 工具收到 chunk 后，必须在 await 压缩之前更新待持久化消息，避免取消时保存空输出或上一块；压缩仍使用独立临时消息，只有成功返回才替换在线输出。
- Unix Shell 启用 spool 时，正常结束和命令超时后都必须等待 stdout/stderr reader 及其 append worker 完成，再解码、格式化和关闭文件。进程结束后的 pipe 读取使用累计 1 秒预算，不计入 append worker 和实时回调等待；不能用整个 reader 的超时取消慢写入，也不能无限等待继承 pipe 的脱离进程组子进程。命令执行超时与输出落盘等待是不同阶段。
- Shell 命令可能在 spooling 开始后将 `tool_result` 目录改成普通文件。artifact 失败清理需逐个容错；`unlink(missing_ok=True)` 不会吞掉 `NotADirectoryError`，清理失败也必须报告 `shell_output_storage_failed`，不得读回完整输出。
- 备份初始化 await 放在任务互斥锁内、409 检查之后；保存任务到启动任务之间没有 await，避免初始化取消留下未启动的 PENDING 任务。TaskStore 原有判忙范围仍是 RUNNING。

## 验证与解释结果

线程边界测试应记录线程 ID，并用 `threading.Event` 控制阻塞、取消与完成顺序。Provider 测试同时验证客户端在 loop 构造、构造时不重新读取配置；checkpoint 测试验证绑定和工具注册复用同一 epoch。

扩大测试失败时，在相同虚拟环境中运行基线源码，区分已有失败与本次回归。2026-10-13 分支基线存在 10 个 Shell/安全规则测试失败，涉及引号断言、日志捕获和网络传输规则；本次保留原语义。

worker 可以降低同步 I/O 对 loop 的阻塞，但线程中的 Python CPU 工作仍可能竞争 GIL。应测量 loop 延迟、排队时间、运行时间及 RSS，不能仅凭线程 ID 宣称吞吐提升。

历史 artifact 阶段 B 已采用有限头尾读取与展示缓存，容量常量位于 `tool_output_compaction.py`：`_DISPLAY_CACHE_MAX_ENTRIES=128`、`_DISPLAY_CACHE_MAX_BYTES=4*1024*1024`。文件 metadata 与恢复引用参与缓存键，缓存之前必须验证 artifact 仍位于 resolved workspace 内；只检查 resolved tool_result 父目录不足以拒绝整个目录指向外部的 symlink。

清理常量为 `_CLEANUP_INTERVAL_SECONDS=300`、`_CLEANUP_MAX_ENTRIES=128`，按目录与 retention_days 隔离；超出记录容量的冷目录可能提前重扫。显式同步清理不节流。

Unix Shell 在启用工具压缩的调用上下文使用有限缓冲与流式 spool，完整格式化 artifact 复用 canonical 截断引用。每条 pipe 原始缓冲最多 budget，另计 pipe、64 KiB 解码缓冲和其他对象开销；Windows、未启用压缩仍为全输出；已启用 Unix spool 的存储失败按下述严格策略处理。取消后必须等待真实 spool worker 结束再关闭文件，重复取消也遵守此规则。

checkpoint 绑定在 Chat 锁内只读 epoch；record/events 的损坏校验由完整状态消费者执行，写入/fsync 和持久化保证保留。runtime-state 使用独立 8 线程预算，避免默认 executor 饱和阻塞状态读取。SSE 已由下述共享日志替代历史入队，消费每 64 条让出 loop。

`runtime_diagnostic` 日志包含 loop p95/p99、样本数、RSS 采样峰值和 `performance_metrics`。worker 排队/执行分位数来自最近 2048 次真实完成任务；完成次数、checkpoint 读取字节/调用、S3 分页尝试次数是本进程累计。loop/RSS 则是两次日志之间的窗口；原诊断采样周期 10 秒，采样峰值不等于瞬时峰值。checkpoint 计量限 active-state epoch/record/events，S3 计量限 list_backups 的 list_objects_v2 尝试（不含 SDK 内部重试）。无租户/路径标签，Pod 之间的分位数不能直接平均。

专项脚本与原始结果见 [性能记录](../../docs/superpowers/benchmarks/2026-10-14-event-loop-followups.md)。恢复计划复用由用户明确排除本轮。Provider 副本捕获后稳定；既有 builtin Provider 逐字段刷新仍不提供捕获时的事务快照。

## 2026-10-14 Query 与辅助入口清理

Query preflight 和 runtime 配置回退使用 runtime-state worker；初次 Prompt 文件读取、frontmatter/heartbeat 处理和 stat 预先准备，只在同步 Agent 安装期间绑定，结束或抛错均 reset。`source_tool_versions` 的 `catalogue.json` 加载也在 worker。每次 Query attempt 的 Prompt 刷新由 worker 计算，在 await 成功后写回；取消后不改变在线 Prompt 和 memory。构造阶段未发现创建后台任务，watchdog 在后续执行阶段启动。

TLS 实测问题为 OpenAI 客户端构造在 loop 调用三次 `ssl.create_default_context`（本地探针约 47.7 ms 总构造时间，其中三次证书加载约 9.4 ms；无网络）。现在正常异步准备在 Provider worker 生成独立 SSLContext，OpenAI/Anthropic/Ollama 使用 SDK 默认 HTTP 客户端参数构造，保留环境代理和证书校验。TLS 准备失败使用原 SDK 构造/备用模型行为；该异常路径仍可能阻塞 loop。客户端保留 SDK 原有 best-effort asyncio 清理，明确 close/aclose 仍有效。这是线程边界验证，不能据此推断生产吞吐改善。

Prompt Hook、工具摘要、建议、技能优化、内存 compact/summary/dream 已接异步模型准备。摘要/建议配置准备在缓存锁之前，reset 代次阻止迟到快照重入缓存；请求可继续使用自己捕获的配置。内存三流程的同步 Agent 配置读取也迁移到 runtime-state。

Goal finalization/completion judge 也准备冻结模型配置后在 loop 安装 Agent。选择专家的 TOML 读取、依赖目录初始化/复制和 session save-path 的 mkdir 在请求上下文 worker 内执行；返回的上下文仅在 await 成功后安装。内存 summary 的时区配置读取也在 worker。

运行时准备阶段若已连接 MCP 却在 Agent 准备期间取消，会等待独立 cleanup task 完成；重复取消不打断清理，清理失败记录日志并保留原 CancelledError，避免连接尚未移交给 Query runtime 时泄漏。

Provider 主 Query 初始化与 `refresh_if_due` 已在 Provider executor，未改刷新策略。主 Query 技能注册使用工作区快照；同步 CLI、直接 SWEAgent 构造、无快照兼容路径仍保留同步 API。工具实时回调每 4 KiB chunk 进行编码/行处理与有界 JSON frame 入队，未发现同步文件写入。广播成本仍随订阅者数增长，需根据 loop p95/p99 和生产 trace 判断是否需要进一步批量处理。

不宣称所有 CPU 工作已移出 loop：Toolkit 注册、模型包装、实时 JSON 编码仍有同步 CPU；线程工作仍竞争 GIL。Gemini 等未使用这两类 SDK 的 Provider 保持原构造方式。未实施 S3 恢复计划复用。

本轮扩大回归 499 passed、1 deselected；排除的 `test_get_in_memory_memory_uses_instance_tenant_id` 在 HEAD 2311b7506 同样因字符串 fake memory 不支持 add 而失败。Goal 生命周期测试文件的 mypy 1 项、pylint 9 项原有诊断也已在该 HEAD 逐项核对，当前无新增；其他修改文件静态检查通过。提交钩子对这两项使用基线例外，其余钩子正常执行。

## 兼容构造入口复查

不能仅凭同步工厂调用判断 loop 上存在 I/O：AgentRuntimeBuilder、工具摘要、建议和 Runner 已可消费准备结果，ReMe 和技能优化的异步生产入口也已迁移。同步 API 保留给脚本和旧调用方；CLI 目录当前没有直接构造 SWEAgent/模型的调用。

真正发现的补漏点：`prepare_model_config_async` 的隐式 Agent ID 解析可能读取配置，必须连同模型配置一起在 Provider worker 执行；共享 `get_agent_and_config_for_request` 在取得工作区后也必须把配置加载交给 runtime-state worker。来源工具调试需先准备模型、Prompt 与技能快照再安装 Agent。构造日志直接使用已解析 model slot，避免仅为日志触发 ProviderManager 冷初始化。测试应同时验证真实线程、ContextVar、准备结果注入和 Prompt 绑定 reset。

## Shell 批次写入

Unix pipe reader 每次仍读取最多 4096 字节、照常发送实时输出；spool 写入按每 pipe 64 KiB 聚合，并在 reader finally flush 尾批次。跨阈值读取块的剩余字节必须在 await 批次写入之前保存，否则取消会丢失尾部。同步 spool append 接口不变，线程完成后释放许可和文件的规则不变。测试不能再用第几个 4 KiB append 的固定偏移假设 worker 执行时机。

每 pipe 新增聚合缓冲及转换瞬间副本，不能将展示预算作为整个请求的内存上限。12 次批次对比显示任务数/总耗时下降，但短窗口 p99 有尖峰；数据、测试口径和限制见 [批次测量](../../docs/superpowers/benchmarks/2026-10-15-shell-batching.md)。

## SSE 共享日志与恢复

`sse_replay.py` 为每轮保存共享临时日志，订阅只持有游标和一个有界读取批次；TaskTracker 全局锁不等待日志 I/O。`run_stream_io` 使用独立 2 线程/4 个提交许可，许可在线程真正结束后释放，取消发布仍等待已提交写入完成。单帧 1 MiB、每轮 64 MiB/65536 条、进程日志与索引 512 MiB；进程最多 128 个日志/256 个订阅，每轮 8 个订阅，读取批次对象另有 64 MiB 总预算。限制与 TTL 常量集中在该模块。

Console 以 cursor-v1 协商游标协议，只有协商成功的订阅启用慢消费者主动恢复和 30 秒 socket 发送超时。前端应用事件后才推进 turn-bound cursor，重连沿用同一 owner/reducer，重复跳过、缺口报错；非终态 EOF 不表示业务成功。网络断开仅释放订阅，不取消后台 Agent。终态已有持久化 snapshot 可恢复，临时日志不支持跨进程/Pod 恢复。

`runtime_diagnostic.performance_metrics.sse_replay` 提供占用/配额/读写/恢复计数，worker 指标新增 stream。磁盘失败、配额不足、过期有明确控制帧；不要在满 payload 队列里等待 sentinel。压测应同时观察 RSS、容器内存、临时目录容量及 worker 排队；[本地测量](../../docs/superpowers/benchmarks/2026-10-15-sse-replay.md) 显示内存下降和逐事件落盘的吞吐代价，不代表生产 p99 改善。

## SSE 批量写入与 Shell 最终处理

SSE 已增加连续区间读取与有界 writer：32条/约64KiB/首事件5ms触发，pending+in-flight最多64条/普通256KiB，大帧独占；进程输入引用预算32MiB。兼容 append 仍等待提交，end/close 排空后才结束和移除运行记录；启动前取消和无文案异常也必须唤醒等待者。`pending_memory_bytes`、`write_batches`、`written_records` 可观察输入引用和批量写入。

TaskTracker 按用户决定默认启用 offer 聚合，`SWE_SSE_BATCH_WRITES=0` 可关闭（每轮启动时捕获，已运行轮次不变）。本地突发 worker 减少约96%、完成时间改善，但稀疏交付增加约5–6ms，16租户事件p99也上升。Linux/Kubernetes 固定时长延迟/RSS准入尚未通过，默认开关调整不改变这些测量限制。见 [方案](../../docs/superpowers/specs/2026-10-15-sse-batching-design.md) 和 [原始测量](../../docs/superpowers/benchmarks/2026-10-15-sse-batching.md)。

Shell 每流终态边界现只扫描一次，本地全换行8/64MiB最终处理约减半。乐观单遍 UTF-8 候选的非法尾部回退增加约25%～101%，超过20%门槛，已撤回候选并保留原双遍预校验与持久化。正常 spool 已分块生成 artifact；完整字符串回退已由下述严格存储失败策略取代，仍不能把展示预算作为整个请求的内存上限。见 [方案](../../docs/superpowers/specs/2026-10-15-shell-finalization-design.md) 和 [测量](../../docs/superpowers/benchmarks/2026-10-15-shell-finalization.md)。

## SSE 分阶段延迟指标

`runtime_diagnostic` 日志的 `performance_metrics.stream_stages` 输出以下固定阶段，单位为毫秒：

| 阶段 | 计量口径 |
|---|---|
| `admission` | offer 进入至成功接纳，每事件，包含容量等待；拒绝或接纳前取消不采样 |
| `batch_wait` | 接纳至批次提交，每事件，包含聚合及前批占用，并非纯定时器等待 |
| `accepted_to_committed` | 接纳至 loop 公布成功提交，每成功事件，包含写入和 loop 恢复 |
| `write_queue` / `read_queue` | I/O 入口至线程开始，每物理批次，包含提交许可等待和 executor 排队 |
| `write_execution` / `read_execution` | 线程开始至操作退出，每物理批次，失败和取消后的真实完成也计入 |
| `read_total` | read 进入至返回或异常，每读取批次，包含排队、执行及 loop 恢复 |

每阶段最多保留最近 2048 个浮点样本，输出 `sample_count`、`duration_avg_ms`、`duration_p95_ms`、`duration_p99_ms`、`duration_max_ms`；`observed_count` 是本进程累计次数。空样本的延迟字段为 null，诊断窗口轮换不清空这些样本和累计次数。无租户、路径或事件文本标签。

取消写入即使最终物理写入成功，也不补记成功提交样本；close 不计为读写批次。阶段相互嵌套，分位数不能相加，也不能直接平均多个 Pod 的分位数。这些指标不表示客户端收到、HTTP 发送完成或 fsync 完成。

本轮按用户要求暂不压测，仅验证计量边界、失败/取消、配额释放和协议回归；不据此宣称吞吐、RSS 或生产 p99 改善。实施记录见 [计划](../../docs/superpowers/plans/2026-10-15-sse-stage-metrics.md)。

## OpenAI 异常构造与剩余 loop CPU

OpenAI 的异步 TLS 准备失败后，`PreparedTLSFailure` 保存没有 traceback 的异常副本；主/备用 OpenAI 构造重新抛出独立副本，由原模型 fallback 逻辑处理，不在 loop 再次读取同一证书。错误类型、参数及 filename 保留，非 OpenAI fallback 仍可成功。该失败只属于本次冻结准备，下一次请求重新准备；同步未准备入口保留 SDK 原行为。其他 SDK 未纳入本轮失败路径优化。

OpenAI 连接检查、模型列表/单模型检查、图片/视频探测使用 `_client_async`，在有界 runtime-state worker 准备证书，再在 loop 调用原同步 `_client` 安装 SDK。保留原超时值和异常处理位置；准备期间取消不会在 worker 完成后安装迟到客户端。同步 `_client` 仍可供兼容调用。

`builtin_tool_schemas.py` 仅允许固定的 11 个内置工具函数共享 Schema 模板，缓存最多 32 项，每次注册深拷贝。主 Query 在已有 `_prepare_query_agent_context` worker 中预热；新 Toolkit、RegisteredToolFunction、在线 Agent 仍在 loop 构造。动态请求闭包、绑定方法、技能和来源工具继续使用原解析方式，不缓存请求数据；直接同步构造的首次冷解析仍可能在调用线程执行。

本机单次 cProfile 显示 10 个基础工具注册约 15ms，主要在 SDK 签名/文档解析和 Pydantic Schema 生成。这只是定位探针，无网络，使用测试 tracing fake；没有并发压测或生产 p99 收益结论。模型 token/tracing/retry 包装的构造只做字段绑定，没有发现构造期间配置或网络 I/O，因此保持 loop 安装。

TaskTracker 的普通 SSE 与工具实时输出共用生产者计数，每连续发布 64 条主动让出 loop；容量背压仍单独生效，顺序不变。实时 Shell 仍按最多 4KiB pipe chunk 处理，展示帧受字节/行预算限制；小帧 JSON 编码保留在 loop，避免每帧 worker 切换。共享日志只在批次完成时通知最多 8 个订阅者，没有按订阅者复制 payload。本轮不扩大到其他 Provider、Windows 或压测。实施记录见 [计划](../../docs/superpowers/plans/2026-10-15-openai-loop-followups.md)。

## Shell 严格存储失败策略

已启用 spool 的 Unix/Linux 调用禁止完整输出内存回退，错误类型统一为 `shell_output_storage_failed`。初始化会在命令启动前检查目录并试建临时文件；失败时命令未运行。写入失败（含短写）后 stream 进入失败终态，拒绝追加，立即终止进程组，等待 reader 和真实 worker 完成。失败 reader 的剩余管道数据按固定块丢弃、限时排空，避免未读管道阻止进程资源释放；重复取消仍等待清理。

最终 artifact 创建、写入、flush/fsync、rename 或展示读取失败时保留命令真实退出码，逐个清理本次不完整文件，再报告存储失败。错误只带有限的最佳努力预览，不读取完整输出，不返回无效 artifact 引用，不重跑命令，也不撤销命令已经发生的副作用。预览内容按展示预算在 stdout/stderr 间分配，另有固定错误文案开销；预览读取或解码失败可省略。

小输出仍使用有限内存；Windows 和未绑定 spool 的路径维持原行为。初始化检查不能保证后续磁盘仍可写，所以执行期和最终阶段都必须保留失败处理。实现与验证见 [实施记录](../../docs/superpowers/plans/2026-10-15-strict-shell-output-storage.md)。

## Shell 执行前准备

前台 `execute_shell_command` 的命令拦截、cwd/路径校验、Python 脚本扫描、环境加载、Python 路径守卫临时文件创建与进程限制配置读取，统一在 `run_runtime_state_work` 中执行。复用进程级 8 个提交许可与 ContextVar 传播，不使用默认 executor，也不缓存租户准备结果。返回后在 loop 构造 preexec 闭包、取得租户执行许可并启动子进程；原校验与错误分类不变。

`_prepared_shell_execution` 用 ExitStack 持有守卫，通过独立 task 等待准备完成。取消准备不会启动命令，也不会提前关闭仍被 worker 使用的资源；正常结束、策略读取失败、spool 初始化失败、执行许可等待取消及子进程启动失败都会在准备真正结束后由 worker 关闭守卫。清理使用重复取消屏障，文件删除也不在 loop。同步 `prepare_shell_command` 保留，后台进程同步启动路径不变。实施记录见 [计划](../../docs/superpowers/plans/2026-10-15-shell-preparation-worker.md)。
