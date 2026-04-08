## 1. Thinking 标签解析

- [x] 1.1 扩展 `extract_thinking_from_text()`，支持 closing-only `abc</think>`
- [x] 1.2 新增 `strip_think_tags()` 与 `normalize_thinking_prefix()`
- [x] 1.3 增加 `tag_parser` 单元测试，覆盖完整 think block、closing-only 和标签清理

## 2. Kimi 专用 chat model

- [x] 2.1 新增 `KimiChatModel`，复用 `OpenAIChatModelCompat` 的流式解析
- [x] 2.2 仅对 Kimi 两种 think 标签文本做后处理，不实现 structured thinking 合并
- [x] 2.3 增加 `KimiChatModel` 单元测试

## 3. Provider / API / UI 接线

- [x] 3.1 新增本地 chat model registry，并让 `Provider.get_chat_model_cls()` 支持本地类
- [x] 3.2 扩展 `OpenAIProvider.get_chat_model_instance()` 支持 `KimiChatModel`
- [x] 3.3 将 `kimi-cn` / `kimi-intl` 默认 `chat_model` 切到 `KimiChatModel`
- [x] 3.4 扩展后端 `ChatModelName`
- [x] 3.5 扩展前端 provider 配置与 custom provider 创建选项

## 4. Stream 边界修复

- [x] 4.1 新增 reasoning boundary helper
- [x] 4.2 在 `AgentRunner.stream_query()` 包装基类事件流
- [x] 4.3 增加 runner reasoning boundary 单元测试

## 5. 文档与回补材料

- [x] 5.1 新建 OpenSpec 主档（proposal / design / tasks）
- [x] 5.2 编写高精度移植手册，覆盖文件、函数、变更目的、测试与回补顺序
- [x] 5.3 记录本次验证结果与前端既有构建阻塞项
