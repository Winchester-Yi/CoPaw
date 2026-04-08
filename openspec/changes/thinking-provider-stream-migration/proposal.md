## Why

当前 `v1.0.0` 基线在三个地方存在断层：

- Kimi 的 `<think>...</think>` / `</think>` 文本 thinking 没有被规范化成标准 `thinking` block
- OpenAI-compatible provider 只能在 `OpenAIChatModel` 语义下运行，缺少一个可选的 Kimi 专用增强层
- runner 流式事件里，reasoning 结束时会暴露一个空白 assistant boundary message，而不是明确的 `REASONING Completed`

这些问题会导致：

- Kimi thinking 在 UI 和 formatter 侧表现不稳定
- provider 配置无法显式表达 “OpenAI 协议 + Kimi thinking 解析增强”
- 上层消费者需要感知并处理不必要的空白 boundary 事件

## What Changes

- 新增 `KimiChatModel`，作为 `OpenAIChatModelCompat` 的窄增强层，仅处理 Kimi 的两种 think 标签文本形式
- 扩展 OpenAI-compatible provider 的 `chat_model` 选择能力，允许配置 `KimiChatModel`
- 将内置 `kimi-cn` / `kimi-intl` 默认 `chat_model` 切换为 `KimiChatModel`
- 修复 runner reasoning end boundary 语义：空白 assistant boundary 不再透传，改为显式 `REASONING Completed`
- 补齐 OpenSpec 主档与高精度移植手册，方便未来版本回补

## Capabilities

### New Capabilities
- `kimi-thinking-normalization`: 将 Kimi `<think>` 文本转为标准 `thinking` block
- `openai-compatible-kimi-chat-model`: 为 OpenAI-compatible provider 增加 `KimiChatModel` 配置能力
- `reasoning-end-boundary-normalization`: 将 reasoning 结束边界从空白 assistant message 归一化为 `REASONING Completed`

### Modified Capabilities
- `provider-config-chat-model-selection`: 扩展前后端 provider 配置入口，允许选择 `KimiChatModel`

## Impact

- Affected code:
  - `src/copaw/local_models/tag_parser.py`
  - `src/copaw/providers/*.py`
  - `src/copaw/app/runner/*.py`
  - `src/copaw/app/routers/providers.py`
  - `console/src/pages/Settings/Models/components/modals/*.tsx`
- Affected behavior:
  - Kimi / OpenAI-compatible provider 的 `chat_model` 配置与实例化
  - reasoning stream 的结束事件语义
- Unchanged behavior:
  - MiniMax Anthropic provider 路线
  - 旧前端 builder 迁移策略
  - 控制字符 thinking 标签支持范围
