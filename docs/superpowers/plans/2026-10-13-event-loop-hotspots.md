# Event-loop Hotspots Implementation Plan

> **For agentic workers:** Use superpowers:test-driven-development for each task and GitNexus impact before editing existing symbols. Follow the independent subsystem tasks below and review each diff before integration.

**Goal:** 完成五项热点阶段 A 的非阻塞边界，保留现有协议和错误语义。

**Architecture:** runtime-state 复用包级 worker；大输出和备份使用分别受限的线程执行边界。异步准备请求资源，构造时注入，不把整个 Agent 构造移到线程。

**Tech Stack:** Python asyncio、ThreadPoolExecutor、contextvars、pytest、现有 Provider runtime cache。

## Task 1：有界输出 worker 与压缩

- [x] 新增 `tests/unit/test_output_workers.py`，用 threading.Event 控制执行，验证取消后的真实运行仍占用 slot，且 ContextVar 传播。
- [x] 新增压缩线程边界及取消不修改在线消息测试，先运行并确认失败。
- [x] 新增 `src/swe/output_workers.py`，使用提交前的进程级并发限制、共享 executor、复制请求上下文和真实完成释放。
- [x] 在 `tool_output_compaction.py` 新增异步入口：worker 内复制消息内容并调用同步压缩核心，返回后替换在线消息内容；即时结果和 ReMe 调用此入口。
- [x] 运行输出预算与压缩原有测试，覆盖持久化、原引用和 source 配置语义。

## Task 2：checkpoint 异步准备

- [x] 回归测试证明请求内存的文件锁和 checkpoint 读取在 worker 线程，且 epoch 在绑定和工具注册中复用。
- [x] 增加异步请求内存工厂，复用 runtime-state 边界；Query 在构造前 await，Agent 注入内存，保留同步调用兼容。
- [x] 删除生产 Query 构造内直接同步读取 checkpoint 的路径。
- [x] 运行 checkpoint store、attachment、command、compaction 及 Query 构造回归。

## Task 3：Provider 请求资源准备

- [x] 在模型工厂测试中证明 async 解析不在主线程等待存储锁，快照模型构造不调用 prepare_scope 或重读配置。
- [x] 用现有 Provider executor 异步准备主模型、fallback 和 retry/rate 配置，copy 配置以避免刷新改变已捕获结果。
- [x] Query/Agent builder 消费准备结果；同步入口沿用原实现。scope 与 source 隔离必须由现有解析决定。
- [x] 运行模型工厂及 Provider cache/tenant 测试。

## Task 4：Shell 输出处理

- [x] 在实际 subprocess 回归中记录 smart_decode/format/classify 的线程，先确认旧实现失败。
- [x] Unix 完成及超时路径通过输出 worker 执行 join/decode；执行工具通过输出 worker 完成格式化和错误分类。
- [x] 保持实时 frame、退出码、超时后缀和进程组清理，运行 shell live/interceptor/tenant/security 测试。

## Task 5：S3 异步门面

- [x] fake SDK 分页测试记录线程并控制延迟，验证取消不会提前释放 slot。
- [x] 新建备份 worker 边界，service/client 增加 async 包装，覆盖 router、worker、shell_worker、batch_service。
- [x] 保留同步方法和完整结果结构，SDK 构造也在 worker 中；运行备份与路由回归。

## 整体验证

- [x] 统一运行五项定向套件和新增边界测试。
- [x] black、git diff --check、编译检查。
- [x] 对指定 worktree 执行 GitNexus detect-changes；下界/截断结果不得写成无风险。
- [x] 更新阶段 A 完成状态及后续 B/C/D 限制，保留 worktree 和分支供继续开发。

## 验证记录

- 基线：`0f89bf1d5ac44d4c26dbeb502dc95d56fe47c149`，目标分支 `freshness-ttl-optimization`。
- 最终 26 文件整合 pytest：442 passed、10 failed（8.02s）。10 个失败全部在独立提取的基线源码上复现（116 passed、10 failed），属于 Shell 凭据引号断言、日志捕获和两条安全规则；没有修改这些行为。
- 30 个改动 Python 文件 AST 检查通过；所有改动文件的 pre-commit 检查通过，包括 mypy、black79、flake8、pylint；`git diff --check` 通过。
- GitNexus 已重新索引，并使用独立临时 Git 索引把新增文件纳入 `detect_changes(scope="all")`；结构化结果为 34 文件、193 符号，`partial=false`、`truncated=false`，不依赖 CLI 的前 15 项展示。图索引仍有动态 callable/receiver 未解析和流程枚举截断，零流程影响不能解释为无风险。模型构造入口的 pre-edit CRITICAL 风险已告知并以兼容路径、scope/override/fallback 回归验证。
- 实际 S3 与生产并发压测未执行；测试使用 fake SDK、线程 ID 和受控阻塞。完整性能测量及阶段 B/C/D 留待后续开发。
- 交付范围为本次 worktree 的阶段 A 代码、测试和文档；主工作区原有修改未纳入，阶段 B/C/D 与合并不在本次提交范围。
