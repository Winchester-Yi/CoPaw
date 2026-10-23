# OpenAI 异常构造与 loop CPU 后续优化

执行技能：brainstorming、writing-plans、test-driven-development；用户已授权继续开发，保持兼容，不处理 Gemini 等其他客户端，不做压测。

## 设计与取舍

1. OpenAI TLS 预准备失败保存无 traceback 的异常副本，构造时重新抛出副本，由原主/备用模型选择逻辑处理。SDK 客户端仍在 loop 构造，避免在 loop 再次读取失效证书。不改同步 API 的默认路径，不关闭验证，不为其他 SDK 改变失败行为。失败快照只对本次准备有效，下一次请求重新准备。
2. 固定内置工具 Schema 使用函数身份作键的有限缓存。通过 SDK 的公开注册接口生成 Schema，每次使用深拷贝；只允许固定函数集合，不缓存闭包、绑定方法、技能或来源工具。主 Query 在已有上下文准备 worker 内预热，工具对象和 Agent 仍在 loop 安装。
3. SSE 生产者每成功发布 64 条主动 sleep(0)，普通事件和工具实时帧共用计数；保留顺序、背压与取消行为。订阅通知最多八个对象，已有界；小帧 JSON 编码不逐帧提交线程。
4. 模型 tracing/token/retry 包装构造经源码核对仅绑定字段，限流器在调用时取得，不增加线程切换。
5. OpenAI 管理操作（连接检查、模型列表、单模型检查、图片/视频探测）改走 `_client_async`：在有界 runtime-state worker 准备 TLS，返回 loop 调用原 `_client` 安装 SDK。保留同步入口、超时值、异常捕获位置；取消准备时不安装迟到客户端。

选择理由：整体构造移入线程会改变 SDK/Toolkit 所有权；每个小 JSON 帧提交 worker 又会增加调度成本。仅迁移可确认的证书 I/O 和重复 Schema 生成，并增加明确的协作式让出边界。

本地单次 cProfile（测试 tracing fake，无网络）：10 个固定工具注册共 56973 次调用，约 15ms，主要在 SDK _parse_tool_function、docstring parser 和 Pydantic Schema 生成。该数据仅用于定位，不代表吞吐或 p99 收益。

## 实施与验收

- [x] TLS：先增加失败回退不在 loop 读取证书、异常类型/信息、绑定 reset、后续成功请求恢复测试；运行失败后实现。
- [x] Schema：先测试与 SDK 原结构相同、深副本隔离、动态工具不缓存、主 Query worker 预热与 loop 注册不再解析；运行失败后实现。
- [x] 发布：用立即返回的 offer 验证连续普通事件/工具帧在 64 条内让出，顺序和取消清理不变；运行失败后实现。
- [x] OpenAI 管理入口：真实 SDK 验证证书线程与客户端构造线程，五个调用方验证准备绑定，取消/失败后不安装迟到客户端且后续请求可恢复。
- [x] 执行模型、Toolkit、Query、TaskTracker、SSE 和实时输出相关 pytest；修改文件 pre-commit 与 diff check。
- [x] 更新 Playbook；提交前执行 GitNexus detect_changes 核对当前分支。

验证记录：新增测试先复现 loop 证书读取、130 条连续发布均未让出、缺少 Schema 缓存及管理入口异步准备；实现后 17 个文件 230 passed、1 deselected（19.64s）。排除的 `test_subagent_toolkit_filters_builtins_and_excludes_delegate` 在 HEAD 59ed9ca65 原始 react_agent 源码上同样因现有 emit_wplus_sop_event 多于测试预期而失败，保留原行为。管理入口测试随后单独复跑 8 passed。静态检查适用项全部通过；格式钩子自动修改后再完整执行。

影响说明：GitNexus impact 已执行；PreparedModelConfig 返回 CRITICAL 且混入无关前端，多个函数 UNKNOWN。源码调用核对用于补足，不能将图中的空调用集合视为无影响。测试重点覆盖冻结配置、备用模型、同步兼容及工具实例隔离。
