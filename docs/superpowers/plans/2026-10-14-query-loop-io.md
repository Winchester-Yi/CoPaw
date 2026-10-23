# Query Loop I/O Implementation Plan

**Goal:** 沿用兼容方案，移出主 Query 的配置读取和提示词文件 I/O，核实 Provider 与实时输出边界。

**Architecture:** 使用已有 runtime-state worker（8）传播 ContextVar。初次提示词文件部分与 metadata 在 worker 准备，用仅覆盖同步构造的请求级 ContextVar 传入，不跨请求缓存。提示词重建在 worker 计算结果，在 loop 应用；取消后不修改 Agent/在线消息。客户端、Toolkit 和 SSE 状态仍在 loop 安装。

**Tech Stack:** Python、asyncio、ContextVar、pytest。

- [x] `tests/unit/app/test_event_loop_request_preparation.py`：preflight 和 runtime fallback 回调检查真实线程及租户上下文，原实现须失败；`query_preflight.py` 与 `query_runtime.py` 改用 `await run_runtime_state_work(...)`。保留审批短路和已有配置快照。
- [x] `tests/unit/agents/test_prompt_async_preparation.py`：文件读取在 worker；同步构造在绑定范围不再读取；不同参数不复用；文件变化下一轮可刷新。`prompt.py` 增加请求级准备结果与绑定，`react_agent.py` 的 metadata helper消费准备结果。
- [x] `finalize_query_runtime` 在同步 Agent 构造前准备并绑定提示词。无需新增 SWEAgent 构造参数。真实模型客户端的 loop线程测试保持通过。
- [x] `rebuild_sys_prompt_async` 在worker返回prompt/token，在loop应用，保留同步接口与旧内存更新顺序；`query_attempt.py` 优先调用异步接口，对旧 Agent 使用同步回退。验证取消后worker完成不修改在线状态。
- [x] Provider refresh主Query已在Provider executor；实时callback仅有界文本处理、JSON编码与内存广播，没有同步落盘。记录同步CLI/无准备构造的兼容路径和其边界。
- [x] 定向pytest、全部改动pre-commit、diff检查、完整GitNexus变更分析。更新Playbook/设计审计结果并提交当前分支。

影响：preflight/runtime/prompt辅助函数LOW；rebuild_sys_prompt图UNKNOWN，文本确认query_attempt及测试调用；load_agent_config是CRITICAL公共入口，本轮只改变异步调用方，不更改其同步语义。GitNexus绑定当前worktree，索引HEAD2311b7506。

## 扩展范围与验收

- [x] 模型 TLS 证书加载加入 Provider worker；OpenAI/Anthropic/Ollama 的异步 HTTP 客户端仍在 loop 构造。每次准备独立 SSLContext，遵循 HTTPX 的 SSL_CERT_FILE/SSL_CERT_DIR，使用 SDK DefaultAsyncHttpxClient 保留 timeout/limits/follow_redirects。无效证书环境回退原构造路径以保留备用模型语义。
- [x] Prompt Hook、工具摘要、建议生成、技能优化、内存 compact/summary/dream 的模型初始化使用异步准备；内存流程配置读取使用 runtime-state worker。
- [x] 摘要/建议缓存先准备再取缓存锁，reset 增加代次以防等待期间失效快照重入缓存。该两入口 HIGH 风险已报告；模型工厂 CRITICAL 风险已报告并覆盖调用方回归。
- [x] 保留同步兼容入口。小块实时输出的文本/JSON/队列操作继续在 loop，没有同步文件 I/O。没有把所有 Python CPU 工作移入线程，也不宣称消除 GIL 竞争。
- [x] 独立复核补齐 Goal 模型准备、专家 TOML/依赖目录准备、session 路径 mkdir 和内存 summary 时区配置读取；运行时准备取消后清理已连接 MCP，重复取消与清理失败不覆盖原取消信号。

验证结果记录在 `analysis/playbook/event-loop-worker-boundaries.md`。

## 兼容构造入口复查

本轮保留 `SWEAgent(...)`、`AgentRuntimeBuilder.build_for_swe_agent` 和 `create_model_and_formatter()` 的同步 API。SDK client、Toolkit 和在线 Agent 仍在 loop 安装，不将完整构造器放入线程。

| 入口 | 实际调用上下文与处理 |
| --- | --- |
| `agent_runtime_builder.py` | 同步安装边界，消费 Agent 携带的 `prepared_model_config`；保留原签名和回调顺序。 |
| ReMe | compact/summary/dream 已调用异步工厂；同步 `_create_execution_model_formatter` 保留兼容，未发现异步生产调用。 |
| 工具摘要、建议 | 缓存未命中时先 await 配置/TLS，再在锁内同步安装 client；本轮修复其中隐式 Agent ID 在 loop 读配置的问题。 |
| 技能优化 | 流式 API 已使用 `get_model_async`；同步 `get_model` 保留兼容。 |
| Runner | Query、Goal finalization 和 completion judge 已由异步外层准备；同步 helper 不删除。 |
| 来源工具调试 | 补齐模型、Prompt 和 workspace skill snapshot 的异步准备；请求配置通过 runtime-state worker 加载。 |
| CLI / 外部同步调用 | CLI 目录未发现直接调用模型工厂或 SWEAgent；同步公共 API 继续支持脚本，不自动创建或嵌套事件循环。异步调用方应使用既有异步模型入口。 |

- [x] 隐式 Agent ID 解析移入 Provider worker，ContextVar 和有效租户参数保持传播。
- [x] `get_agent_and_config_for_request` 的配置加载移入 runtime-state worker。
- [x] Agent 构造日志使用实际解析的 model slot，不再为日志查询/冷初始化 Provider。
- [x] 来源工具调试消费已准备结果，Prompt 的绑定只覆盖同步构造，结束后 reset。
- [x] 新增原实现失败的线程边界、准备结果注入和日志查询回归；相关回归 154 项通过。

GitNexus 绑定主目录 `/Users/shixiangyi/code/Swe`，索引基线 `cbd3238c9`。共享模型准备、SWEAgent、请求配置 helper 的影响为 CRITICAL；来源工具调试入口和 callback 图映射有 UNKNOWN，已补充文本调用核对及对应回归。保留的同步 API、TLS 失败回退、Toolkit/模型包装 CPU 与其他 Provider 边界不据此宣称完全无阻塞。
