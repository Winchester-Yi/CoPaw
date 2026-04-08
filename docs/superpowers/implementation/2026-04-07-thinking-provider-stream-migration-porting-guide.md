# Thinking / Provider / Stream 特性移植手册

日期：2026-04-07

相关主档：
- `openspec/changes/thinking-provider-stream-migration/proposal.md`
- `openspec/changes/thinking-provider-stream-migration/design.md`
- `openspec/changes/thinking-provider-stream-migration/tasks.md`

---

## 1. 文档目的

本手册用于未来升级 CoPaw 基线版本后，快速把本次 “thinking / provider / stream” 特性补回新版本。

目标不是解释需求背景，而是提供**高精度移植路径**：

- 改了哪些文件
- 改的是哪个函数/类/逻辑段
- 新增/修改代码的目的是什么
- 回补顺序是什么
- 对应测试是什么
- 哪些边界不在本次改动范围内

---

## 2. 回补顺序（强依赖顺序）

建议严格按下面顺序回补：

1. `tag_parser` 基础能力
2. 本地 chat model registry
3. `KimiChatModel`
4. `Provider` / `OpenAIProvider` / `ProviderManager`
5. 后端 API `ChatModelName`
6. runner stream boundary helper 与 `AgentRunner.stream_query()`
7. 前端 provider 配置弹窗
8. 测试文件
9. OpenSpec / 实施文档

原因：
- `KimiChatModel` 依赖 `tag_parser` helper
- provider 层依赖 registry 和 `KimiChatModel`
- UI / API 配置入口依赖后端 `chat_model` 名称稳定
- 测试需要建立在最终接线完成之后

---

## 3. 代码改动明细

### 3.1 `src/copaw/local_models/tag_parser.py`

**位置 / 关注点**
- `text_contains_think_tag`
- `extract_thinking_from_text`
- 新增 helper：`strip_think_tags`
- 新增 helper：`normalize_thinking_prefix`

**修改内容**
- 将 `text_contains_think_tag()` 从只匹配 `<think>` 扩展为同时匹配 `</think>`
- 在 `extract_thinking_from_text()` 中增加 closing-only 分支：
  - 当文本里存在 `</think>` 但没有完整 `<think>...</think>` block 时
  - 将 closing tag 之前的文本认作 `thinking`
  - 将 closing tag 之后的文本认作 `remaining_text`
- 新增标签清理 helper，用于去掉字面 think 标签

**目的**
- 支持 Kimi 返回 `abc</think>` 这种非标准 closing-only 格式
- 为 `KimiChatModel` 提供稳定的文本清洗与规范化能力

**回补注意点**
- 不要把控制字符 thinking 标签支持一起塞进来
- 不要在这里引入 structured thinking 合并逻辑

**对应测试**
- `tests/unit/local_models/test_tag_parser.py`

---

### 3.2 `src/copaw/providers/chat_model_registry.py`

**位置 / 关注点**
- 新增模块
- `OPENAI_COMPATIBLE_CHAT_MODELS`
- `is_openai_compatible_chat_model`
- `get_local_chat_model_cls`

**新增代码**
- 本地 chat model registry
- 当前只注册 `KimiChatModel`

**目的**
- 让本地增强 chat model 能被 provider 基类解析
- 避免以后继续在 `Provider.get_chat_model_cls()` 里硬编码多个 `if`

**回补注意点**
- 这个文件是 provider 层和本地 chat model 的连接点
- 如果新版本已有类似 registry，应优先并入现有机制，而不是重复新建

**对应测试**
- `tests/unit/providers/test_provider_manager.py` 中 `test_openai_provider_can_resolve_kimi_chat_model_cls`

---

### 3.3 `src/copaw/providers/kimi_chat_model.py`

**位置 / 关注点**
- 新增模块
- `class KimiChatModel(OpenAIChatModelCompat)`
- `_iter_base_stream_responses`
- `_normalize_kimi_response`
- `_iter_kimi_stream_responses`
- `_parse_openai_stream_response`

**新增代码**
- 在 `OpenAIChatModelCompat` 基础上做 Kimi 专用后处理
- 处理两种文本形式：
  - `<think>...</think>`
  - `abc</think>`
- 对已有 `thinking` block 仅做标签清理
- 对 `text` block 做拆分：
  - 生成标准 `thinking` block
  - 保留剩余 `text` block

**目的**
- 不污染通用 `OpenAIChatModelCompat`
- 把 Kimi 的差异收敛在 chat model 层

**回补注意点**
- 不要把它升级成通用 “thinking 修复器”
- 不要加入 structured thinking 合并
- 仅保留 Kimi 当前需要的两种文本语义

**对应测试**
- `tests/unit/providers/test_kimi_chat_model.py`

---

### 3.4 `src/copaw/providers/provider.py`

**位置 / 关注点**
- `Provider.get_chat_model_cls()`

**修改内容**
- 在查 `agentscope.model` 之前，先查 `chat_model_registry.get_local_chat_model_cls()`

**目的**
- 让 `KimiChatModel` 能用于：
  - provider 配置校验
  - provider 信息回显
  - 未来本地 chat model 扩展

**回补注意点**
- 如果新版本的 provider 基类已经支持自定义 registry，直接接入，不要重复拼两层逻辑

**对应测试**
- `tests/unit/providers/test_provider_manager.py`

---

### 3.5 `src/copaw/providers/openai_provider.py`

**位置 / 关注点**
- 顶部引入 `is_openai_compatible_chat_model`
- `OpenAIProvider.update_config()`
- `OpenAIProvider.get_chat_model_instance()`

**修改内容**
- 覆写 `update_config()`：
  - 对 OpenAI-compatible provider 允许更新 `chat_model`
  - 只接受 OpenAI-compatible 范围内的名称
- `get_chat_model_instance()` 按 `self.chat_model` 分支：
  - `OpenAIChatModel` -> `OpenAIChatModelCompat`
  - `KimiChatModel` -> `KimiChatModel`

**目的**
- 让内置和自定义 OpenAI-compatible provider 都能切到 `KimiChatModel`
- 保持 transport / auth / base_url 逻辑仍然复用 `OpenAIProvider`

**回补注意点**
- 不要误伤 Anthropic / Gemini provider
- 如果新版本 `OpenAIProvider` 已有自己的自定义 model 分发层，要把 `KimiChatModel` 接进那一层，而不是重复覆写

**对应测试**
- `tests/unit/providers/test_openai_provider.py`

---

### 3.6 `src/copaw/providers/provider_manager.py`

**位置 / 关注点**
- `PROVIDER_KIMI_CN`
- `PROVIDER_KIMI_INTL`

**修改内容**
- 给两个内置 Kimi provider 增加默认：
  - `chat_model="KimiChatModel"`

**目的**
- 让内置 Kimi 开箱即走专用解析路径

**回补注意点**
- 当前 `_init_from_storage()` 不会用磁盘值覆盖 builtin `chat_model`
- 如果新版本改了 builtin merge 逻辑，需要确认这个默认值不会被旧配置反向覆盖

**对应测试**
- `tests/unit/providers/test_kimi_provider.py`

---

### 3.7 `src/copaw/app/routers/providers.py`

**位置 / 关注点**
- `ChatModelName`

**修改内容**
- 在字面量里新增 `KimiChatModel`

**目的**
- 放开后端 API 的合法值范围
- 保证前端配置保存/测试连接时不会被后端 schema 拒绝

**回补注意点**
- 若新版本把 `ChatModelName` 拆成 enum 或 schema 常量，要在对应位置加值

**对应测试**
- 间接由 provider API/UI 验证

---

### 3.8 `src/copaw/app/runner/stream_boundary.py`

**位置 / 关注点**
- 新增模块
- `_is_empty_reasoning_boundary_message`
- `_normalize_reasoning_boundary_events`
- `normalize_reasoning_boundary_stream`

**新增代码**
- 维护当前活跃 reasoning message
- 将空白 assistant boundary 替换为同 id 的 `REASONING Completed`

**目的**
- 修正 reasoning 结束事件语义
- 避免上层消费者感知到空白占位消息

**回补注意点**
- 这是 runtime 集成层修复，不是 adapter 层重写
- 如果新版本 adapter 已经原生修复该语义，优先删掉本层补丁，而不是叠加双重完成事件

**对应测试**
- `tests/unit/app/test_runner_reasoning_end_boundary.py`

---

### 3.9 `src/copaw/app/runner/runner.py`

**位置 / 关注点**
- 顶部 import `normalize_reasoning_boundary_stream`
- `AgentRunner.stream_query()`

**修改内容**
- 新增 `stream_query()` override
- 包装 `super().stream_query()` 的事件流
- 调用 `normalize_reasoning_boundary_stream()`

**目的**
- 把 reasoning 边界修复插入 CoPaw runtime 层，而不改第三方 runtime 包

**回补注意点**
- 这一层必须放在 `Runner.stream_query()` 之后、向上层暴露之前
- 不要移到 `query_handler()`，否则拿不到 adapter 之后的标准事件

**对应测试**
- `tests/unit/app/test_runner_reasoning_end_boundary.py`

---

### 3.10 `src/copaw/providers/__init__.py`

**位置 / 关注点**
- 包导出逻辑

**修改内容**
- 从立即导入改成惰性导出 `ProviderManager` / `ActiveModelsInfo`

**目的**
- 降低包级副作用
- 避免在导入子模块（如 `kimi_chat_model`）时无意义触发 `provider_manager`

**回补注意点**
- 这是低风险结构性优化
- 如果新版本已经做了惰性导出，不需要重复改

**对应测试**
- 本地测试导入链间接受益，无独立断言

---

### 3.11 `src/copaw/app/runner/__init__.py`

**位置 / 关注点**
- 包导出逻辑

**修改内容**
- 从立即导入改成惰性导出 `AgentRunner` / `router` / `ChatManager` 等

**目的**
- 让 `stream_boundary` 能被独立导入测试
- 避免导入 `copaw.app.runner.*` 时强制触发整套 runner 依赖

**回补注意点**
- 如果新版本 runner 包结构已调整，这个改动可能不再需要

**对应测试**
- 本地测试导入链间接受益，无独立断言

---

### 3.12 前端：`console/src/pages/Settings/Models/components/modals/ProviderConfigModal.tsx`

**位置 / 关注点**
- 常量区新增 chat model option
- `canEditChatModel`
- provider config 表单中的 `chat_model` 选择器

**修改内容**
- 为内置 OpenAI-compatible provider 放开 `chat_model` 选择
- 内置 OpenAI-compatible provider 只显示：
  - `OpenAIChatModel`
  - `KimiChatModel`
- custom provider 显示：
  - `OpenAIChatModel`
  - `KimiChatModel`
  - `AnthropicChatModel`

**目的**
- 满足“所有 OpenAI-compatible provider 都允许选择 `KimiChatModel`”

**回补注意点**
- 当前文案仍叫 “Protocol”，这是刻意保留
- 不要把 built-in OpenAI provider 放开到 `AnthropicChatModel`

**验证**
- 前端 build 尝试执行，但仓库存在既有无关 TypeScript 错误

---

### 3.13 前端：`console/src/pages/Settings/Models/components/modals/CustomProviderModal.tsx`

**位置 / 关注点**
- 创建 custom provider 时的 `chat_model` Select

**修改内容**
- 新增 `KimiChatModel` 选项

**目的**
- 让自定义 OpenAI-compatible provider 能在创建时直接绑定 Kimi thinking 增强

**回补注意点**
- 如果新版本改成由后端返回枚举选项，应优先接后端下发配置

---

## 4. 测试文件清单

本次新增/修改的测试：

- `tests/unit/local_models/test_tag_parser.py`
- `tests/unit/providers/test_kimi_chat_model.py`
- `tests/unit/providers/test_openai_provider.py`
- `tests/unit/providers/test_kimi_provider.py`
- `tests/unit/providers/test_provider_manager.py`
- `tests/unit/app/test_runner_reasoning_end_boundary.py`

Windows 本地验证中，为了绕过 `fcntl` 缺失，测试文件对 `fcntl` 做了 shim：

- `tests/unit/providers/test_kimi_provider.py`
- `tests/unit/providers/test_provider_manager.py`

这只是测试层适配，不是生产逻辑改动。

---

## 5. 本次验证结果

### 已通过

使用仓库 `.venv` 解释器执行：

```bash
"/mnt/c/Users/lenovo/Desktop/CoPaw/.venv/Scripts/python.exe" -m pytest \
  tests/unit/providers/test_kimi_provider.py \
  tests/unit/providers/test_provider_manager.py \
  tests/unit/providers/test_openai_provider.py \
  tests/unit/providers/test_kimi_chat_model.py \
  tests/unit/local_models/test_tag_parser.py \
  tests/unit/app/test_runner_reasoning_end_boundary.py -q
```

结果：`62 passed`

### 前端验证

执行：

```bash
npm --prefix console run build
```

结果：失败，但失败项均为仓库中现有的无关 TypeScript 问题，集中在：

- `src/components/agentscope-chat/Markdown/Markdown.tsx`
- `src/components/agentscope-chat/Stream/index.ts`
- `src/components/LanguageSwitcher/index.tsx`
- `src/pages/Agent/Skills/*`

本次修改的两个 provider modal 文件未在报错列表中出现。

---

## 6. 明确不包含的范围

这次回补时不要顺手带入以下内容：

- MiniMax provider 类型迁移
- 旧前端 runtime builder 改造
- 控制字符 thinking 标签支持
- structured thinking 合并
- 前端 “Protocol” 文案重命名

---

## 7. 回补检查清单

未来版本回补时，至少逐项确认：

- `tag_parser` 是否仍保留 think 标签解析入口
- provider 基类是否仍通过 `get_chat_model_cls()` 做类解析
- `OpenAIProvider.get_chat_model_instance()` 是否仍是统一实例化入口
- `ProviderManager` builtin merge 逻辑是否会覆盖 `chat_model` 默认值
- `Runner.stream_query()` 是否仍是上层统一事件出口
- 前端 provider 弹窗是否仍由这两个 modal 文件负责
- 若 upstream 已修复 reasoning boundary，删除本地 patch，避免双重 `Completed`

这份清单完成后，再决定是直接 cherry-pick 代码，还是按功能点手动移植。
