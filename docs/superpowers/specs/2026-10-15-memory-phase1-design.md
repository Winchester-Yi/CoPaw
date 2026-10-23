# Python 内存优化第一阶段设计

日期：2026-10-15。状态：用户已确认并完成第一阶段实现，待部署观察。

源码基线：`59ed9ca65beb757961d43954e2fea284cdd54801`。
GitNexus 绑定 `/Users/shixiangyi/code/Swe`；注册索引提交与 HEAD 一致。但本轮对 summary 相关符号的查询未返回可靠的 Python 调用关系，以下调用约束已用源码复核，不能把查询空结果解释为无调用。实施前仍须逐符号运行 impact，确认调用方并报告风险。

## 1. 目标与边界

第一阶段消除已结束操作留下的大对象引用，并使临时文本缓存具备主动过期和明确容量上限。

包含：

1. summary Task 完成、失败、取消后的回收，以及 `/await_summary` 兼容。
2. summary 后台上下文隔离和 memory manager 关闭清理。
3. SessionExecution 退出及进入失败时解除状态快照引用。
4. Console 推送、suggestions、Q&A 的过期缺陷修复、主动清理、空键回收及容量限制。
5. 必要统计和针对持有链、并发竞态、隔离语义的测试。

用户已确认继续使用进程内缓存；Redis 迁移后续独立设计。本阶段不增加数据库表、不改变多实例路由与消费方式，不改 Provider、技能、锁缓存、SSE、会话归档或文件处理。运行中的 summary 输入本身仍占用内存，本阶段不宣称限制全部在途请求的内存。

## 2. 方案比较与选择

| 方案 | 优点 | 代价与缺口 |
| --- | --- | --- |
| 只修 TTL，加定时扫描 | 修改最少 | 未解除 Task/Context 持有链，也不能限制两次扫描间的突发写入 |
| 完成即释放＋有限结果记录＋写入容量检查＋主动扫描（采用） | 生命周期明确，兼容现有进程内路径，可独立验证 | 需要处理 await 与完成回调竞态，超限行为需要明确 |
| 将临时文本迁至 Redis | 可统一 TTL 和跨实例访问 | 涉及原子消费、投递去重、隔离、故障策略，超出本阶段 |

不引入通用缓存框架。Task 生命周期留在 memory 子系统；文本缓存各自负责条目和预算；一个轻量生命周期服务只负责调度清理。

## 3. Summary Task 生命周期

### 3.1 所有权与完成记录

`BaseMemoryManager` 的 `_summary_tasks_by_scope` 只保留在途 Task。每个任务创建时分配轻量 task ID；完成回调按 Task 身份移除自身，scope 为空时删除键。

完成回调必须消费成功结果、异常或取消状态，且只生成轻量记录：`task_id / scope / status / finished_at / expires_at / bounded_message`。记录不包含 Task、Msg、模型、formatter、异常对象、traceback 或请求对象。异常文案先脱敏再截短；成功结果也截短，只用于命令状态展示，不替代已经保存的业务总结。

完成记录按 manager 保存，有以下固定初始边界：

| 维度 | 初始限制 |
| --- | --- |
| 有效期 | 完成后 300 秒，读取不续期 |
| 单 scope | 最近 10 条 |
| 单 manager | 256 条，记录的键和值总 UTF-8 数据不超过 512 KiB |
| 单条展示文案 | 1 KiB，附明确截短标记 |

超限按完成顺序淘汰最旧记录；不会取消运行任务。记录过期时间使用 monotonic 时钟。manager 通过弱引用注册到清理入口，扫描器不延长 manager 寿命；弱引用失效时相关注册自动消失。256 条和 512 KiB 是每个 manager 的上限，不伪称是进程总上限；进程中 manager 数量及活跃工作区另行统计。

### 3.2 `/await_summary` 契约

`get_summary_task_count()` 表示在途任务数；命令不能再仅凭该值为零就提前返回。增加轻量的可查询记录检查，区分“没有任务”与“任务已完成，有近期记录”。

一次 await 在首次挂起前固定当前 scope 的任务 ID 集合及未消费完成记录集合：

1. 对已完成记录直接生成状态文本。
2. 对快照中的在途 Task 等待完成，收集对应状态。使用 shield，取消等待命令不应连带取消后台 summary。
3. 完成回调与 await 共用幂等结算方法；相同 task ID 只能进入完成记录一次，返回文本也不重复。
4. 只消费本次快照里的记录，不按 scope 整体 `pop()`；等待期间新建任务继续被追踪。
5. 并行 await 可观察相同在途任务，各自返回其快照结果；只对已经完成且未被认领的历史记录做一次性消费。等待者取消时不丢掉尚未返回的历史记录。
6. 完成记录即使在等待期间被容量策略淘汰，当前等待者仍可从它持有的 Task 得到有限状态文本；不为等待者无限保留全局历史。

`/await_summary` 仍按原 scope 隔离。完成记录超过保留期后，命令返回没有待等待或可查询记录，不能继续承诺查询全部历史结果。兼容 `summary_tasks` 属性时不应在只读访问中创建空 scope。

### 3.3 后台 Context

使用全新的 `contextvars.Context` 创建后台任务，只注入实际所需的轻量数据：

- tenant、source、canonical scope、user 标识、工作目录和文件网络选择。
- 环境覆盖值的独立副本；需要的透传 headers 独立副本，不能共享可变字典。
- 脱敏需要的注册 secret 值；不能因为 Context 隔离而丢失日志脱敏能力。
- trace 使用 `capture_current_trace_context()` 的轻量字典显式传递给模型工厂，不传原 TraceContext；已有显式 model/formatter 绑定继续生效。

不传播 SessionExecution、活动事务映射、技能 detector、任务进度 tracker 或其他未知 ContextVar。通过所属模块的小型 helper 获取必要数据，避免 memory 子系统导入 session 私有变量。

项目支持 Python 3.10，因此使用 `context.run(asyncio.create_task, coroutine)`；注册 done callback 也在独立空 Context 中执行，不能依赖仅较新 Python 支持的 `create_task(context=...)`。回调闭包只捕获 manager、scope 和 task ID，不捕获消息或请求。

### 3.4 关闭

manager 开始关闭后拒绝创建 summary。先给现有任务 5 秒完成；之后取消剩余任务，再给 5 秒处理取消；全部结束后才关闭 ReMe 并清空轻量记录。重复关闭幂等，ReMe 未初始化时也必须执行 Task 清理。

如果后端任务不响应取消，不能把它从注册表丢掉后宣称关闭成功，也不能并发关闭它仍在使用的 ReMe。保留未结束任务引用，记录超时并返回关闭失败；上层最终进程退出策略保持现状。测试覆盖正常关闭、取消清理和不响应取消的失败路径。

## 4. SessionExecution 解除快照引用

在 `__aexit__` 的 finally 中恢复父 Context、标记事务关闭，并将自身 `_state` 替换为新的空字典；进入阶段读取后若失败，也执行相同的引用解除。只替换引用，不调用旧字典的 `clear()`，避免破坏外部仍合法持有的状态对象。

不在退出时新增自动保存，不改变 commit、revision、锁释放、Stop 和结算顺序；未提交数据的处理沿用现有契约。关闭后 state/read_state 继续拒绝访问。已提交的磁盘内容、父层嵌套事务 Context 和调用者先前取得的状态内容不受影响。

## 5. 临时文本缓存

### 5.1 过期与隔离

保持现有 canonical scope 解析与读写接口的隔离规则，不重新组合 tenant/source。修复 suggestions 过滤返回值被忽略的缺陷；peek 应更新或删除对应缓存，take 返回前必须过滤，且读取不存在键不得创建空列表。

使用条目写入时确定的过期时间，读不续期。默认 TTL：推送和 suggestions 60 秒，Q&A 120 秒。Q&A 自定义有效期以写入时参数为准，读取参数只能进一步缩短有效窗口，不能延长已经到期的条目。Console 的 `get_recent(max_age_seconds)` 也只缩短存储有效期。

现有 `sticky` 只是返回给界面的标志，不绕过 TTL 或预算；delivery_key 的在缓存内去重语义保持不变，不增加另一个无界去重集合。

### 5.2 容量与超限

以下是每个 Python 进程的固定初始值，先使用模块常量，不新增配置页面或大量环境变量：

| 缓存 | 总字节预算 | 总条目 | 单 scope | 单条 |
| --- | --- | --- | --- | --- |
| Console 推送 | 16 MiB | 10,000 条 | 500 条且 2 MiB | 256 KiB |
| suggestions | 2 MiB | 2,000 个 session entry | 200 个 session entry 且 256 KiB | 最多 10 个建议，整条 16 KiB |
| Q&A | 6 MiB | 5,000 对 | 500 对且 1 MiB；每 chat 最多 50 对 | 64 KiB |

字节预算计入实际保存的键和字符串数据，包括 scope/session/chat/hash/delivery_key 及正文；不是仅计算正文。UTF-8 预算用于可预测业务数据上限，条目数限制容器开销；这两个数都不等于 Python 堆或 RSS 精确值。单条限制先检查，避免为超大输入先创建完整编码副本；不得引入每次写入全缓存 JSON 序列化或递归 `getsizeof()`。

更新、消费、删除和清理都在各缓存现有锁内同步维护字节/条目统计及写入顺序索引。覆盖写入先计算替换后的差额；只保留缓存拥有的不可变字符串或独立列表，调用方修改传入 suggestions 列表不能绕过预算。

写入顺序：单条校验 → 删除过期项 → 检查覆盖差额 → 淘汰本 scope 最旧项 → 必要时淘汰全局最旧项 → 插入。采用写入时间顺序，不因频繁轮询续期或移动位置。使用有界的有序索引按需弹出，不能每次写入排序全部缓存。

超限行为：

- 单条推送过大或键数据使其无法入库：整条拒绝，不截断，append 返回 False；成功写入或命中已有 delivery_key 返回 True。Console 的 send/send_content_parts 传播失败，避免拒绝写入仍返回成功。没有 session 的纯终端输出沿用原行为。
- 合法推送可淘汰旧条目；这是临时尽力交付缓存，不承诺可靠投递或持久化。按原因计数，日志不输出正文和 secret。
- suggestions 最多取前 10 个建议；若其整体仍超限则拒绝这一批，不截短句子。Q&A 超限则跳过辅助缓存，不截短原始匹配键；不能使主回答失败。
- 本阶段不新增大正文持久化和前端引用协议；需要可靠全文投递时另行设计。

### 5.3 主动清理服务

新增一个进程级清理服务，由 FastAPI lifespan 持有。默认每 60 秒依次调用推送、建议/Q&A、summary 完成记录的清理入口。清理入口直接遍历已保存的完整 scope 键，不依赖当前请求 Context，不调用会创建默认键的 getter。

每个 store 单独加锁，单次最多处理 256 条后释放锁并 yield，避免长时间占用事件循环；所有容器已有容量硬上限。扫描过程中并发覆盖须按 ID/到期时间复核，不能误删新条目。单个 store 清理失败只记录错误，不中止其他 store 或永久退出循环。

服务 start/stop 幂等；启动失败后能够停止已创建任务。关闭时取消并等待清理任务，之后停止生产方/工作区，再执行最终缓存 reset；不能在生产方运行期间重置计数。非 HTTP 场景仍执行读写时清理和容量检查，主动清理服务只由显式生命周期入口启动，不在模块 import 时创建 Task。

不要求在事件循环繁忙时精确到秒过期；正常空闲条件下应在 TTL＋60 秒＋一个分批扫描轮次内物理回收。读取逻辑始终即时过滤已到期数据。

## 6. 文件边界

| 文件 | 职责 |
| --- | --- |
| `src/swe/agents/memory/base_memory_manager.py` | 在途任务、幂等完成、有限结果、await 与关闭辅助方法 |
| `src/swe/agents/memory/summary_task_context.py`（新增） | 只处理 summary 的轻量 Context 捕获与创建，兼容 Python 3.10 |
| `src/swe/agents/memory/reme_light_memory_manager.py` | 显式轻量 trace 传递，先清任务后关闭后端 |
| `src/swe/agents/command_handler.py` | `/await_summary` 识别近期完成记录与准确文案 |
| `src/swe/config/context.py`、`constant.py`、`tracing/sanitizer.py` | 仅在需要时提供所属上下文的安全快照 helper，不修改默认请求行为 |
| `src/swe/app/runner/session.py` | 退出和进入失败时解除 `_state` 引用 |
| `src/swe/app/console_push_store.py` | 推送过期、预算、空键、写入结果及统计 |
| `src/swe/app/suggestions/store.py` | suggestions/Q&A 过期、预算、隔离及统计 |
| `src/swe/app/channels/console/channel.py` | 将推送拒绝写入反映到发送结果 |
| `src/swe/app/transient_memory_cleanup.py`（新增） | 清理任务的 start/stop 与周期调度，无缓存业务规则 |
| `src/swe/app/_app.py` | lifespan 接入和最终 reset 顺序 |

## 7. 验证与交付

新增测试使用实际类和方法，不以复制实现或仅 AST 抽取替代。时间推进用可注入/monkeypatch 时钟，不真实等待数分钟。弱引用测试可显式 GC，但生产不依赖周期 `gc.collect()`。

1. Summary：成功/失败/取消均自动移除 Task 和空 scope；保留有限可查询结果；traceback/模型/SessionExecution 弱引用可回收；父 Context 和租户环境未被改变。
2. await：任务先完成再 await、等待期间新增、多个等待者、等待者取消、容量淘汰及跨 scope 隔离，不重复结算、不丢新增任务。
3. Session：正常退出、异常、进入失败、取消和重复 close；提交内容仍在磁盘，旧状态字典未被原地清空，锁与父 Context 正确恢复。
4. 文本缓存：不轮询也能回收；peek/take 不返回过期建议；UTF-8 中文、覆盖、超长键、超大正文、空正文和空键；消费/淘汰后计数准确；sticky/delivery_key 与 tenant/source 隔离。
5. 生命周期：重复启动停止、部分启动失败、清理异常后下一轮继续、生产方停止后最终 reset，无悬挂后台 Task。
6. 合成负载：固定工作区连续执行 1,000 个不同 summary scope，全部结束后在途数归零；文本持续写入跨过容量上限，条目与预算不越界；多轮相同负载下存活对象不随历史 scope 数增长。

测试入口包括现有 `tests/unit/agents/test_memory_manager_async_summary.py`、`tests/unit/app/test_session.py`、`test_console_push_store.py`、`test_suggestions_store.py` 和新增 `test_transient_memory_cleanup.py`、summary 命令回归测试。始终用 `venv/bin/python -m pytest`，按变更范围运行；实施前补具体测试节点。

统计仅提供聚合数：pending/completed summary、缓存 entries/scopes/payload_bytes、expired/evicted/rejected_total 和 cleanup_errors_total。不把 session/scope 原值作为指标标签，不导出正文；现有 stats 字段保持兼容并增加聚合字段。

按两个独立变更交付：A 为 Task/Context/Session 生命周期，B 为临时缓存与清理服务。每个变更先 impact，再编写必要回归测试和实现，最后验证预期符号与流程；提交前 detect_changes。先单 Pod 观察完成任务数、清理速率与拒绝/淘汰率，再扩大部署。

RSS 只作长期辅助指标：释放对象不保证立即归还 OS；本阶段验收依据引用链消失、存活对象稳定和预算有界，不承诺固定 MB 或百分比降幅。

## 8. 实现与验证记录

已实现上述生命周期与缓存边界，并经独立设计覆盖和代码审查。清理服务使用空 Context；调用方取消仍向上传播。最终 reset 在两个关闭入口成功且 pending summary 为零后执行；现有 ServiceManager/MultiAgentManager 对其他服务停止失败的吞错行为未扩大重构，进程退出策略保持现状。

联合 pytest：165 passed、9 个原有 skipped。另一个既有失败 `test_get_in_memory_memory_uses_instance_tenant_id` 已在未修改基线复现，fake backend 返回字符串导致现有克隆路径 `.add` 赋值失败；排除该节点后的联合运行同为 165 passed、9 skipped、1 deselected。

新增覆盖包括 1,000 个 summary scope、模型/异常/父 Context 弱引用、并行 await 与取消、预算与 UTF-8、跨扫描 yield 覆盖、真实清理入口装配及关闭失败。Black、mypy、flake8、AST 等通过；完整 pre-commit 中 pylint 报告 `command_handler.py` 原有压缩分支的 `metadata` 参数诊断，该调用未在本阶段改动。

GitNexus impact 对目标方法未能解析，风险按 UNKNOWN 记录并源码复核；刷新索引后 detect_changes 标记 CRITICAL，识别 6 个类、407 条关联流程，其中 ConsoleChannel UID 为空且含大量不相关跨语言关联，不能据此给出可靠完整调用范围。已保留风险提示，按实际调用方回归并审查 diff，不把图结果当作无影响证明。

尚未运行生产 Pod 压测或测量实际 RSS 降幅。字节预算是保存业务字符串的 UTF-8 上限，进程总内存仍包括在途请求、其他缓存和 Python 容器开销；Redis 迁移另行设计。
