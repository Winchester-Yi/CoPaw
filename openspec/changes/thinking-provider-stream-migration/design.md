## Context

当前仓库里已经存在三块可复用基础：

- `OpenAIChatModelCompat` 负责 OpenAI-compatible 流式 tool-call 兼容与 `extra_content` 透传
- `tag_parser.py` 已支持标准 `<think>...</think>` 与 `<tool_call>...</tool_call>` 提取
- `AgentRunner` 统一承接上层 `runner.stream_query(request)` 调用

本次目标不是重写 provider 体系，而是在现有链路上补齐 Kimi thinking 解析能力与 reasoning 结束边界语义。

## Goals / Non-Goals

**Goals**

- 为 Kimi 响应新增专用 thinking 规范化层，支持：
  - `<think>...</think>`
  - `abc</think>`
- 保持 Kimi 仍然使用 `OpenAIProvider`，不新增 `KimiProvider`
- 让所有 OpenAI-compatible provider 都可选择 `KimiChatModel`
- 把 reasoning 结束的空白 assistant boundary 归一化为显式 `REASONING Completed`
- 产出能用于未来版本快速回补的详细移植文档

**Non-Goals**

- 不支持控制字符 thinking 标签
- 不做 structured thinking block 合并
- 不改 MiniMax Anthropic provider 路线
- 不迁移旧前端 runtime builder
- 不修改 “Protocol” 文案为双层协议/行为模型

## Decisions

### Decision: 用 `KimiChatModel` 而不是 `KimiProvider`
`KimiChatModel` 作为 `OpenAIChatModelCompat` 的子类存在，provider 仍然保持 `OpenAIProvider`。

**Why**
- Kimi 的差异是解析后处理，而不是 transport / auth / endpoint 协议
- 可以最小化 provider 体系改动，避免把 MiniMax、OpenAI、DashScope 等路由重新拆分

### Decision: 增加本地 chat model registry
`Provider.get_chat_model_cls()` 先查本地 registry，再查 `agentscope.model`。

**Why**
- 现有逻辑只认识 AgentScope 原生类
- 后续再增加类似本地增强 chat model 时，不需要继续把分支硬编码到 `Provider` 基类

### Decision: 所有 OpenAI-compatible provider 都允许选择 `KimiChatModel`
不限于内置 `kimi-cn` / `kimi-intl`，只要 provider 走 OpenAI-compatible 路线，就允许选择。

**Why**
- 用户已经明确需要这个暴露范围
- `KimiChatModel` 本质上是对文本 thinking 标签的后处理，不依赖特定 provider id

### Decision: `KimiChatModel` 只处理两种 Kimi 文本形式
仅支持：

- 完整 `<think>...</think>`
- closing-only `abc</think>`

不处理 structured thinking 合并，不扩展控制字符协议。

**Why**
- 这是当前需求边界
- 能避免把 Kimi 专用解析放大成通用推理内容重写器

### Decision: reasoning 边界修复放在 `AgentRunner.stream_query()`
新增轻量 stream boundary helper，对基类 `Runner.stream_query()` 产出的事件流做包装。

**Why**
- 不需要 fork `agentscope_runtime` 的 adapter
- 不需要重写 `query_handler()` 的业务路径
- 修复范围可控，集中在 CoPaw 的 runtime 集成层

## Implementation Design

### 1. Thinking 标签解析

在 `src/copaw/local_models/tag_parser.py` 中：

- 扩展 `text_contains_think_tag()`，让 closing-only `</think>` 也算命中
- 扩展 `extract_thinking_from_text()`：
  - 原有完整 block 逻辑保持
  - 新增 closing-only 分支：`</think>` 之前的内容视为 thinking，之后的尾部视为 remaining text
- 新增：
  - `strip_think_tags(text)`
  - `normalize_thinking_prefix(text)`

### 2. `KimiChatModel`

新增 `src/copaw/providers/kimi_chat_model.py`：

- 继承 `OpenAIChatModelCompat`
- 先复用上游解析结果，再逐个 block 做后处理
- 对 `text` block：
  - 若含 think 标签，则拆成 `thinking` block 与剩余 `text` block
  - 若剩余文本为空，则只保留 `thinking`
- 对已有 `thinking` block：
  - 只做字面 think 标签清洗

### 3. 本地 chat model registry 与 provider 接线

新增 `src/copaw/providers/chat_model_registry.py`：

- 暴露 `get_local_chat_model_cls()`
- 暴露 `is_openai_compatible_chat_model()`

调整 provider 层：

- `Provider.get_chat_model_cls()`：
  - 先查本地 registry
  - 再回退到 `agentscope.model`
- `OpenAIProvider.get_chat_model_instance()`：
  - `chat_model="OpenAIChatModel"` -> `OpenAIChatModelCompat`
  - `chat_model="KimiChatModel"` -> `KimiChatModel`
- `OpenAIProvider.update_config()`：
  - 内置/自定义 OpenAI-compatible provider 都允许切到 `KimiChatModel`
- `provider_manager.py`：
  - `kimi-cn` / `kimi-intl` 默认 `chat_model="KimiChatModel"`
  - `_provider_from_data()` 继续落到 `OpenAIProvider`

### 4. API / UI

后端：

- `ChatModelName` 字面量加入 `KimiChatModel`

前端：

- `ProviderConfigModal`：
  - 对内置 OpenAI-compatible provider 放开 `chat_model` 选择
  - 内置 OpenAI-compatible provider 只允许 `OpenAIChatModel` / `KimiChatModel`
  - custom provider 允许 `OpenAIChatModel` / `KimiChatModel` / `AnthropicChatModel`
- `CustomProviderModal`：
  - 新增 `KimiChatModel` 选项
- 保留现有 “Protocol” 文案，不改 i18n 词条结构

### 5. Stream 边界修复

新增 `src/copaw/app/runner/stream_boundary.py`：

- 维护当前活跃的 reasoning message
- 若后续收到空白 assistant `MESSAGE + InProgress + no content`：
  - 吞掉该 boundary message
  - 输出同 id 的 `REASONING + Completed`

`AgentRunner.stream_query()` 改为对 `super().stream_query()` 的结果做包装。

## Risks / Trade-offs

- `KimiChatModel` 被开放给所有 OpenAI-compatible provider 后，非 Kimi provider 如果返回字面 think 标签，也会被同样清洗
  - 当前视为可接受行为，因为语义是标签规范化，不依赖 provider id
- 前端保留 “Protocol” 文案会继续有轻微语义偏差
  - 当前是显式选择，不阻碍功能落地
- `console` 全量 build 当前有仓库内既存 TypeScript 错误
  - 本次仅验证相关文件改动未引入新的后端回归，前端全量构建结果需与既有问题分开解读

## Validation Plan

- 单元测试：
  - `test_tag_parser.py`
  - `test_kimi_chat_model.py`
  - `test_openai_provider.py`
  - `test_kimi_provider.py`
  - `test_provider_manager.py`
  - `test_runner_reasoning_end_boundary.py`
- 前端：
  - 尝试执行 `npm --prefix console run build`
  - 若失败，记录是否为既有无关错误
- 文档：
  - OpenSpec 三件套完整
  - 移植手册覆盖代码/配置/测试/文档四类改动
