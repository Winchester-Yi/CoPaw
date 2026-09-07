# 事件循环阻塞优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除文件/媒体处理和技能运行时解析路径中的同步阻塞，降低 `RUNTIME_DIAGNOSTIC` 的 `event_loop_lag`，并保持现有文件安全边界、技能选择语义和多租户隔离。

**Architecture:** 将媒体下载拆为“异步 I/O + 有界并发 + 原子落盘”的适配层；将技能 manifest 重整/安全扫描从请求热路径移到启动、变更或后台任务，并为每个请求复用不可变的技能快照。短期先把无法立即异步化的同步工作整体移入专用 worker，长期替换为真正的异步 HTTP 和可 await 的扫描接口。

**Tech Stack:** Python `asyncio`、`httpx.AsyncClient`、`ThreadPoolExecutor`、现有 workspace manifest/skill scanner、pytest、`RUNTIME_DIAGNOSTIC`。

## 已确认的运行边界

- Query 在开始时捕获不可变 `Query Skill Snapshot`（有效技能、metadata、路径、内容签名和 runtime profile）；后续技能变更只影响后续 query。注册前签名变化时仅该技能失败关闭，query 继续运行但不加载无法确认的 Workspace Skill。
- 新 query 发现缓存失效时，先在 worker 中等待一次去重后的 reconcile，再捕获快照；不得用旧快照静默满足新 query。已开始的 query/子 Agent 继续持有其 launch snapshot。
- Workspace Skill 使用 manifest/channel/签名校验；临时 Chat-private Scenario Skill 继续使用 Session Marketplace Resource Snapshot，不进入 workspace manifest/channel 解析，但同样要求扫描结果和路径边界有效。
- 媒体只允许 HTTP(S)、`file://`、本地路径和 base64；媒体解码后统一上限 10 MiB，单次下载共享 60 秒总 deadline（连接 10 秒、读取空闲 30 秒），最多 3 跳重定向。
- 媒体使用每进程一个有界 executor/semaphore（默认 4）；取消时协程立即结束，worker 负责 `.part-*` 清理。音频下载可并行，转码/转写使用独立并发 1；转写 provider 优化不在本计划范围内。
- 安全扫描保留现有 `block/warn/off` 超时语义；不叠加长期双层线程池。generation 仅进程内诊断使用，不写入 manifest；不引入运行时 feature flag，回滚依赖 Kubernetes 镜像/版本。
- 本计划不改变现有本地路径策略、最终文件名覆盖语义或 host/IP 访问策略；这些另行审计。

### 当前实现状态（2026-09-01）

- A2–A4 与 B2–B4 的运行时隔离已落地：媒体入口使用进程级有界 worker/slot，HTTP(S) 优先走流式异步下载，多个 block 并行且按原序写回；query 使用一次性 Workspace Skill Snapshot，Agent、Hooks 和 Background SubAgent 复用其已确认依赖。
- 安全扫描和 manifest 重整的同步等待已移入 worker bridge；扫描缓存先做递归 stat 探测，变化后再计算内容签名；HTTP 4xx/5xx 不再无谓回退到 wget/curl。
- Workspace manifest/目录变更已接入进程内 coordinator；快照发布前后复核 manifest 与技能 freshness，变化时有限重试，避免在本进程内发布半成品快照。该协调不跨 Kubernetes 进程，跨进程一致性仍依赖现有文件锁与构建后复核。
- 最终快照复核若遇到权限/读取异常会 fail-closed 地移除全部 Workspace Skill，并继续普通 Query；已通过的定向测试、合成压力和静态检查记录在本计划中。按范围裁决，生产压测、RUNTIME_DIAGNOSTIC p95/p99 基线和完整仓库套件不作为本计划门槛；完整套件另受缺失脚本、依赖和本机 provider secret 权限影响。
- Query 异步快照和异步扫描现直接提交到有界 scanner executor；worker 内执行同步扫描，不再形成默认 asyncio worker 等待 scanner pool 的双层结构。取消/超时由协程立即返回，运行中的 worker 在完成后释放 slot；已用单 worker 并发回归验证无死锁。

### 范围裁决（2026-09-01）

用户确认本计划不考虑生产 Kubernetes API、生产窗口和生产发布压测；这些属于后续运维验收，不作为本计划完成门槛。当前完成依据为本地热点定向测试、合成压力、静态检查及代码审计。

---

## 1. 现状与问题边界

### 1.1 文件/媒体消息链路

当前调用链为：

```text
SWEAgent.reply()/run_research_phase()
  -> process_file_and_media_blocks_in_message()
  -> _process_single_block()
  -> _process_single_file_block()
  -> download_file_from_base64()/download_file_from_url()
  -> _download_remote_to_path()
  -> subprocess.run(wget/curl) 或 urllib.request.urlretrieve
```

关键事实：

- `src/swe/agents/react_agent.py:2145`、`:2239` 在 async 方法中直接 `await process_file_and_media_blocks_in_message(msg)`。
- `src/swe/agents/utils/message_processing.py:43-78` 虽然调用的是 async 函数，但这些函数内部仍执行同步解码、目录创建、写盘和远端下载。
- `src/swe/agents/utils/file_handling.py:140-177` 最多连续等待 60 秒 `wget` 和 60 秒 `curl`；回退到 `urllib.request.urlretrieve` 时没有显式超时。
- `src/swe/agents/utils/file_handling.py:180-197` 对 `.file` 文件再做一次同步 HEAD 请求（10 秒超时）。
- `src/swe/agents/utils/file_handling.py:249-267` 的 base64 解码、MD5、`mkdir` 和整块写盘也在事件循环线程执行。
- `process_file_and_media_blocks_in_message()` 按 block 顺序串行处理；多个媒体块会累加等待时间。音频 ffmpeg 转换已使用 `asyncio.to_thread`，但下载本身没有隔离。

因此，“函数声明为 async”不能代表不阻塞；只要任一同步调用运行，事件循环线程就无法处理其他请求和 sampler。

### 1.2 技能运行时链路

当前查询阶段的主要链路为：

```text
select_runtime_context_directives()
  -> build_context_reference_directives()
       -> _skill_directives_by_name()
            -> build_skill_use_directives()
                 -> resolve_effective_skills()
                      -> reconcile_workspace_manifest()
  -> build_skill_use_directives()                 # 显式/场景技能再次执行
       -> resolve_effective_skills()
            -> reconcile_workspace_manifest()
```

关键事实：

- `src/swe/app/runner/query_runtime.py:88` 先处理引用技能，`:99` 再处理显式/场景技能。
- `src/swe/app/runner/skill_selection.py:49` 每次解析都调用 `resolve_effective_skills()`；`:67-76` 对每个选中技能再次读取并解析 `SKILL.md` frontmatter。
- `src/swe/agents/skills_manager.py:1708` 的 `resolve_effective_skills()` 无条件调用 `reconcile_workspace_manifest()`。
- `reconcile_workspace_manifest()` 会获得文件锁、读写 JSON、遍历 enabled/disabled 技能目录、移动目录、读取每个 `SKILL.md` 并重建 metadata；这是同步文件系统操作，而且可能触发安全扫描相关逻辑。
- `src/swe/agents/react_agent.py:_register_skills()` 还先调用 `ensure_skills_initialized()`，随后再调用 `resolve_effective_skills()`，存在额外重整。
- `src/swe/security/skill_scanner/__init__.py:505` 在线程池提交扫描后调用 `future.result(timeout=...)`；若该函数从 async 路由直接调用，等待仍发生在事件循环线程。

### 1.3 不应改变的语义

- manifest 中 `enabled`、`channels`、`config`、`source`、metadata 合并和 UTF-8 安全重命名规则必须保持不变。
- 技能安全扫描不能因缓存或异步化而绕过；缓存键必须能检测 `SKILL.md`/技能目录内容变化。
- 下载仍需支持 `file://`、本地路径、base64、HTTP(S)、`.file` 后缀修正和现有错误占位文本。
- 每个租户/工作区的路径解析、manifest 锁和权限边界必须保持隔离。

## 2. 目标与可观测性

### 2.1 目标

1. 请求处理线程不再执行网络读写、外部进程等待、整块媒体写盘、manifest 重整或同步安全扫描。
2. 同一请求内最多读取一次技能 manifest、每个技能的 frontmatter/runtime profile；命中签名缓存时不重复读取。
3. 多媒体块可并行下载，但并发受限且结果写回顺序稳定。
4. 所有远端操作具备连接、读取、总时长和大小上限，并在失败时清理临时文件。
5. 能用日志将单次下载/重整/扫描耗时与 `event_loop_lag_max_ms` 对齐。

### 2.2 指标与日志

新增结构化字段（不记录 URL query 中的 token）：

- 下载：`media_download_ms`、`media_source_type`、`media_method`、`media_bytes`、`media_timeout`、`media_error`、`media_worker_queue_ms`。
- 技能：`skill_manifest_reconcile_ms`、`skill_manifest_cache_hit`、`skill_count`、`skill_md_parse_ms`、`skill_scan_queue_ms`、`skill_scan_ms`。
- 请求：`runtime_skill_snapshot_generation`、`selected_skill_count`、`reference_skill_count`。

验收基线和目标应在压测前记录：在相同流量、相同媒体大小和技能数量下，`event_loop_lag` 的 p95/p99/max、请求 p95、下载超时率和 manifest cache hit ratio 均需对比。第一阶段以“p95 < 200 ms、且不再出现由单次下载/重整造成的秒级尖峰”为目标；若生产基线高于 200 ms，则采用相对基线下降并消除可归因秒级尖峰作为门槛，具体数值写入压测报告。

### 本地合成压力记录（2026-09-01）

使用临时工作区调用 `get_workspace_skill_snapshot_async()`，heartbeat 间隔 5 ms；扫描设为 `off`，因此结果只代表快照/事件循环隔离能力，不代表安全扫描吞吐或生产网络。记录如下：

| 技能数 | 并发 | 总耗时 ms | lag p95 ms | lag max ms |
|-------:|-----:|----------:|-----------:|-----------:|
| 10 | 1 | 765.9 | 3.33 | 21.77 |
| 10 | 10 | 95.5 | 0.80 | 1.51 |
| 10 | 50 | 436.1 | 0.92 | 28.69 |
| 100 | 1 | 73.7 | 0.74 | 0.96 |
| 100 | 10 | 764.1 | 1.05 | 1.84 |
| 100 | 50 | 4010.4 | 0.98 | 27.63 |
| 500 | 1 | 290.5 | 0.83 | 2.33 |
| 500 | 10 | 3838.9 | 1.06 | 6.28 |
| 500 | 50 | 19707.4 | 1.01 | 58.58 |

该记录不替代 Kubernetes 生产窗口；生产流量、媒体下载、扫描开启和 `RUNTIME_DIAGNOSTIC` p95/p99 对比仍是发布门槛。

## 3. 工作流 A：文件/媒体处理

### Task A1：建立可回归的阻塞基线

**Files:**

- Modify: `src/swe/agents/utils/file_handling.py`
- Modify: `src/swe/agents/utils/message_processing.py`
- Test: `tests/unit/agents/` 下新增媒体处理测试（沿用现有测试目录风格）

- [x] 在下载入口记录 `monotonic()` 前后耗时、来源类型、最终字节数和异常类别；日志不得包含完整签名 URL。
- [x] 用可控的 fake downloader 注入 100–500 ms 延迟，测试同时运行的 heartbeat task 能持续 tick，从而先证明现状测试会暴露 loop block。
- [ ] （后续运维项，不纳入本计划）记录单个、多个 URL/base64、`.file` 后缀和失败回退四组生产基线。

### Task A2：立即止血——把整个同步实现移出事件循环

**Files:**

- Modify: `src/swe/agents/utils/file_handling.py`
- Modify: `src/swe/agents/utils/message_processing.py`

- [x] 保留现有同步实现为明确的私有 worker 函数（包括 `_resolve_local_path`、`_download_remote_to_path`、HEAD、magic bytes、base64 写盘），在 async 公共入口中使用专用有界 `ThreadPoolExecutor` 的 `loop.run_in_executor()`；不要只包裹某一条 `subprocess.run`。
- [x] 使用每进程共享、默认 4 的媒体 worker/semaphore；请求只申请 slot，不创建线程池。
- [x] 为 legacy urllib 路径增加显式连接/读取/总 deadline 控制；HTTP 主路径由 A3 的 httpx 替换。
- [x] 保持取消语义：协程取消后不再修改 message，临时文件在异常/取消路径清理。
- [x] 下载到 `<name>.part-<随机后缀>`，校验存在、非空和大小后用 `os.replace()` 原子改名；失败或超限删除临时文件。

### Task A3：长期方案——真正的异步 HTTP 流式下载

**Files:**

- Create: `src/swe/agents/utils/async_download.py`
- Modify: `src/swe/agents/utils/file_handling.py`
- Modify: `src/swe/agents/utils/message_processing.py`
- Test: `tests/unit/agents/test_async_download.py`

- [x] 使用复用的 `httpx.AsyncClient`，配置 `connect`、`read`、`write`、`pool` timeout 和总时长 deadline；禁止每个 block 新建 client。
- [x] 以 `aiter_bytes()` 流式写临时文件，累计字节超过 `MAX_MEDIA_BYTES` 立即中止并删除；若有 `Content-Length` 且超过上限，在读取前拒绝。
- [x] 最多允许 3 跳 HTTP(S) 重定向，每跳重新校验 scheme、剩余 deadline 和大小上限；不把不受信任 URL 拼接到 shell 命令。
- [x] 通过响应头和首块 magic bytes 推断扩展名；legacy fallback 保留同步 HEAD 兼容路径。
- [x] A2 将 base64 整体解码/hash/写盘放入同一有界 worker，并对约 14 MiB 编码原文预检；暂不做分块解码。

### Task A4：有界并行处理多个 block

**Files:**

- Modify: `src/swe/agents/utils/message_processing.py`
- Test: `tests/unit/agents/test_message_processing.py`

- [x] 先收集待处理 block，使用 `asyncio.gather()` 并通过媒体 semaphore 限制并发；结果按 index 汇总，不在 worker 中并发修改 `message.content`。
- [x] gather 完成后按原 index 顺序统一写回 block 和“文件已下载”文本，确保模型看到的内容顺序不变。
- [x] 单个 block 失败只影响该 block；保留现有 file 错误文本和 image/audio/video 保留原 block 的行为。
- [x] 音频转码保留 `to_thread` 路径，并使用独立并发 1 的音频 slot。

### Task A5：媒体路径测试与验收

- [x] 测试 URL 成功、HTTP 4xx/5xx、超大响应、重定向上限和临时文件边界；legacy fallback 保留既有测试覆盖。
- [x] 测试两个以上 block 的并行上限、写回顺序、取消后 message 不被半写入。
- [x] 使用 heartbeat + `RUNTIME_DIAGNOSTIC` 集成测试验证：人为延迟下载时，event-loop sampler 仍能采样；下载耗时应出现在媒体日志而不是 loop lag 中。

## 4. 工作流 B：技能 manifest、SKILL.md 与安全扫描

### Task B1：拆分“重整”和“只读解析”接口

**Files:**

- Modify: `src/swe/agents/skills_manager.py`
- Modify: `src/swe/app/runner/skill_selection.py`
- Modify: `src/swe/app/runner/context_references.py`
- Test: `tests/unit/agents/test_skill_manifest_runtime_cache.py`

- [x] 保留 `reconcile_workspace_manifest(workspace_dir)` 作为显式变更/初始化操作；新增只读 `read_skill_manifest(workspace_dir, reconcile=False)` 运行时入口。
- [x] 让 `resolve_effective_skills()` 接受可选的已读 manifest 或 `SkillManifestSnapshot`；传入快照时不得再次 reconcile。
- [x] `build_skill_use_directives()` 接受 effective names 和 metadata 的可选参数；若 metadata 已含 description，则不再逐个读取和解析 `SKILL.md`。无 metadata 时保留兼容的旧读取逻辑。
- [x] manifest metadata 中已有 `description`、`requirements` 等字段时直接复用；只有技能内容实际需要加载时才读取全文。

### Task B2：实现工作区级版本化缓存和失效

**Files:**

- Modify: `src/swe/agents/skills_manager.py`
- Create: `src/swe/agents/skill_runtime_snapshot.py`
- Test: `tests/unit/agents/test_skill_manifest_runtime_cache.py`

- [x] 定义不可变快照，至少包含 `workspace_dir`、进程内 `generation`、manifest stat（mtime/size/inode）、effective names、每个技能的绝对目录、metadata、内容签名和 runtime profile。
- [x] 缓存键按工作区隔离；热路径先做轻量 stat token，变化后在 worker 中递归计算内容签名。
- [x] 所有成功的启用/禁用/安装/删除/恢复/迁移操作在写 manifest 后主动失效；外部手工改动由 manifest mtime 或内容 freshness 检测兜底。generation 仅作进程内诊断。
- [x] 使用进程内锁保护缓存更新；重整仍由现有文件锁串行化，避免两个请求同时移动同一技能目录。
- [x] 新 query 的缓存/reconcile 异常或 manifest 损坏时，不加载无法确认的 Workspace Skill，但继续普通 query；不静默放行未扫描技能。管理 API 仍保留严格错误。

### Task B3：一个请求只构建一次技能快照

**Files:**

- Modify: `src/swe/app/runner/query_runtime.py`
- Modify: `src/swe/app/runner/context_references.py`
- Modify: `src/swe/app/runner/skill_selection.py`
- Modify: `src/swe/app/runner/query_contracts.py`（若运行时输入模型需要字段）
- Test: `tests/unit/app/test_skill_selection.py`
- Test: `tests/unit/app/test_runner_context_references.py`

- [x] 在 query runtime 准备阶段取得并验证一次 `WorkspaceSkillSnapshot`；快照构建/校验在 worker 中执行。
- [x] 保留 request/scenario 顺序后再追加 reference 顺序；同名技能保持现有 reference 优先、输出位置不变，并复用一次快照进行 channel 过滤和路径解析。
- [x] 将 `SkillUseDirective`、Agent 注册适配器和 `SkillRuntimeProfile` 的输入从快照 metadata/profile 构造，避免重复读取。
- [x] 把快照挂到 request-scoped inputs；Hooks、Agent 注册和 Background SubAgent 继承同一 launch snapshot；临时 Chat-private Scenario Skill 继续消费 Session Marketplace Resource Snapshot。
- [x] 增加/覆盖无变化 query 的缓存复用断言；快照捕获阶段在 worker 中完成一次 reconcile，并在 Agent 注册阶段复用 metadata/profile。

### Task B4：把 reconcile 和扫描移出 async 热路径

**Files:**

- Modify: `src/swe/agents/skills_manager.py`
- Modify: `src/swe/security/skill_scanner/__init__.py`
- Modify: `src/swe/app/routers/skills.py`
- Modify: `src/swe/app/routers/agents.py`
- Modify: `src/swe/app/workspace/tenant_initializer.py`
- Test: `tests/unit/security/test_skill_scanner_executor.py`
- Test: `tests/unit/app/test_skills_stream_trace_scope.py`

- [x] 启动、安装/启用/禁用/删除、迁移等变更流程显式执行 reconcile；async 路由通过 `await asyncio.to_thread(reconcile_workspace_manifest, workspace_dir)` 或专用 executor 调用，sync 路由继续使用同步 API。
- [x] 新 query 的快照构建/校验在 worker 中完成；已开始 query 继续使用其旧快照，不在 loop 中调用同步 reconcile。
- [x] 使用有界 bridge worker 隔离 `scan_skill_directory()` 的同步等待，不在事件循环中调用同步 scanner。
- [x] 保留现有扫描超时语义：`off` 跳过、`warn` 记录后继续、`block` 超时仍按当前实现返回 `None` 并继续；只有明确安全结果才写入缓存。
- [x] 扫描缓存键使用技能目录路径和 stat/content fingerprint；变化后重新计算内容签名，不能按技能名永久缓存。
- [x] reconcile、扫描和请求读取共享同一工作区级协调器：变更期间不发布半成品 snapshot，失败时保留旧快照并记录错误。协调器为每进程一个；跨进程竞态由现有 manifest 文件锁及快照构建后复核/有限重试兜底。

### Task B5：技能路径测试与验收

- [x] 测试两次 `resolve_effective_skills()` 在无变化时复用快照；修改 `SKILL.md` 后能失效并读取新签名。
- [x] 测试引用技能与显式技能重复时只解析一次，输出顺序和去重语义不变；持久 workspace 场景技能遵循 channel/安全过滤，临时 Chat-private 场景技能遵循 session snapshot/路径边界。
- [x] 测试扫描线程池繁忙、排队超时、扫描执行超时、取消和 cache hit；async 调用期间 heartbeat 必须持续运行。
- [x] 测试 manifest 移动/重命名、锁竞争、损坏 JSON、缺失技能目录和多租户工作区隔离。
- [ ] （后续运维项，不纳入本计划）在技能数量（10/100/500）和并发请求（1/10/50）下记录 reconcile 次数、`SKILL.md` 读取次数、扫描队列等待和 event-loop lag。

## 5. 发布顺序与回滚

1. 先发布 A1/B0 观测字段，至少采集一个完整生产窗口，确认 lag 尖峰与下载或 reconcile 的相关性。
2. 发布 A2 和 B4 的 worker 隔离，稳定前保留有界 worker 兼容路径；若出现线程池饥饿，优先降低 worker 并发或回退到串行 worker，不回退到 loop 内同步调用。
3. 发布 B1–B3 快照缓存；通过命中率、技能选择结果和安全扫描结果对比旧路径。
4. 发布 A3/A4 真异步下载和并行 block；按部署批次推进，重点观察超时、磁盘占用、连接池和上游限流。
5. 稳定后删除旧 wget/curl 热路径和同步扫描调用，仅保留供迁移/CLI 使用的显式同步 API。

回滚要求：不增加运行时 feature flag；通过 Kubernetes 镜像/版本回滚，稳定前保留有界 worker 兼容路径。回滚不得删除已有 manifest、下载文件或扫描缓存，且应保留诊断字段以便定位。

## 6. 完成标准

- [x] `rg`/AST 检查 async 请求链路中不再直接调用 `subprocess.run`、`urlretrieve`、同步 HEAD、`reconcile_workspace_manifest` 或 `future.result`（同步实现仅从 worker 边界调用）。
- [x] 媒体和技能热点定向测试通过（65 passed）；完整仓库套件不作为本计划门槛，失败项记录为既有依赖/环境问题。
- [ ] （后续运维项，不纳入本计划）运行包含慢下载、慢扫描和 manifest 变更的生产回归压测。
- [x] 使用 GitNexus 对实际修改的函数执行了 `impact(direction="upstream")` 并复核调用方；提交前执行 `detect_changes()`。整体审计会包含用户既有改动，已单独核对本次暂存集合。
