# 通道接入、API 与访问界面

本文档整理请求如何进入系统，包括消息通道、HTTP 路由、中间件，以及前端界面目录。

## 后端接入面

| 区域 | 关键路径 | 说明 |
|------|----------|------|
| 通道抽象 | `src/swe/app/channels/base.py`, `src/swe/app/channels/schema.py` | 定义通道模型和基础协议 |
| 通道管理 | `src/swe/app/channels/manager.py`, `src/swe/app/channels/registry.py` | 通道注册与生命周期管理 |
| 队列与渲染 | `src/swe/app/channels/unified_queue_manager.py`, `src/swe/app/channels/renderer.py` | 统一队列与输出格式 |
| 命令注册 | `src/swe/app/channels/command_registry.py` | 通道命令映射 |
| 具体通道 | `src/swe/app/channels/console/channel.py`, `src/swe/app/channels/zhaohu/channel.py` | Console / 招呼等通道实现 |
| 路由层 | `src/swe/app/routers/*.py` | Agent、配置、Provider、文件、消息、技能、Tracing 等 API |
| 中间件 | `src/swe/app/middleware/*.py` | Header 透传、租户身份、租户工作区注入 |
| 认证与审批 | `src/swe/app/auth.py`, `src/swe/app/approvals/service.py` | 身份校验与审批服务 |
| HTML 预览行为事件 | `src/swe/app/html_preview_clicks/`, `console/src/api/modules/htmlPreviewEvents.ts` | 记录主方案、子方案、按钮点击和模块曝光，并提供明细与旧版点击聚合查询 |

## 主要路由文件

- `src/swe/app/routers/agent.py`
- `src/swe/app/routers/agent_scoped.py`
- `src/swe/app/routers/agents.py`
- `src/swe/app/routers/config.py`
- `src/swe/app/routers/envs.py`
- `src/swe/app/routers/files.py`
- `src/swe/app/routers/local_models.py`
- `src/swe/app/routers/mcp.py`
- `src/swe/app/routers/messages.py`
- `src/swe/app/routers/providers.py`
- `src/swe/app/routers/settings.py`
- `src/swe/app/routers/skills.py`
- `src/swe/app/routers/skills_stream.py`
- `src/swe/app/routers/token_usage.py`
- `src/swe/app/routers/tools.py`
- `src/swe/app/routers/tracing.py`
- `src/swe/app/routers/voice.py`
- `src/swe/app/routers/workspace.py`
- `src/swe/app/routers/zhaohu.py`

补充约定：`PUT /envs` 采用当前请求 scope 的全量替换语义，请求体只能包含实际 env 键值对；`tenant_id`、`source_id`、`target_tenant_id`、`target_source_id` 属于保留 scope 字段，普通 API 遇到这些字段必须返回 `400`，不能静默忽略后继续保存。

### HTML 预览行为事件

`POST /api/html-preview/events` 使用 `event_type` 区分
`button_click`、`preview_view` 和 `module_exposure`。旧请求缺省为
`button_click`；`template_type` 区分 `main` 主模板与 `sub` 子模板，
`template_id/result_id` 标识本条事件实际关联的模板和生成结果。模块通过
`event_target_id/name` 标识，`trace_id` 串联生成与浏览链路。
旧版按钮、名单和客户统计只聚合
`button_click`，避免曝光事件污染原有点击口径。旧客户端查询事件明细时
若不传 `event_type`，也只返回 `button_click`，保持原接口语义。
传 `event_type=all` 可查询全部行为事件，并支持按模板类型、模板、生成结果、
模块、链路字段筛选及 `limit/offset` 分页。

详细字段约定与示例见
[HTML 预览事件维度设计](../docs/superpowers/specs/2026-08-14-html-preview-event-dimensions-design.md)。

## 前端目录

| 目录 | 说明 |
|------|------|
| `console/src/api/` | 控制台 API 调用封装 |
| `console/src/pages/` | 页面入口 |
| `console/src/components/` | 通用 UI 组件 |

AgentScope Chat 的工具调用先按 `tool_call_id` 合并调用与结果，再按后端校验后的显式操作组 ID 聚合。首个分组工具进入流时即显示默认折叠的操作组；展开后按原始顺序呈现任意多个 reasoning 与原有独立工具卡，每张工具卡继续默认折叠并可单独展开详情。响应结束后，操作组整体进入既有“执行过程”折叠；缺少操作组声明的历史事件继续使用原有独立工具卡片。

Tool Guard 的 `pending`、`rejected` 和 `blocked` 属于治理状态，不复用工具执行 loading。Console 在发起 SSE 前同步挂载新响应消息，确保快速到达的等待审批文本和 `approval_action` metadata 能在当前会话立即形成审批信息与审批框，而不依赖历史重载。

### 主要前端组件

| 组件 | 路径 | 说明 |
|------|------|------|
| ConversationQuickNav | `console/src/components/ConversationQuickNav/` | 会话快速导航，显示用户问题列表的侧边导航点，支持点击跳转和滚动追踪 |
| SKILL 配置 | `console/src/pages/Settings/SkillConfig/` | 系统设置下的 SKILL 主从配置页，复用定时任务列表作为名称选项，通过 `skillConfig.ts` 对接 list/detail/create/update 接口，并以占位数据呈现待接入的回检指标 |

#### ConversationQuickNav 组件结构

```text
console/src/components/ConversationQuickNav/
├── index.tsx           # 主组件入口
├── types.ts            # 类型定义（QuestionInfo、Props）
├── style.ts            # 样式（导航点、tooltip、高亮动画）
├── components/
│   ├── NavDot.tsx      # 导航点子组件
│   └── QuestionTooltip.tsx  # 问题 tooltip
├── hooks/
│   ├── useQuestionMessages.ts   # 提取已加载的用户问题列表
│   ├── useCurrentQuestion.ts    # 追踪滚动位置对应的当前问题
│   └── useScrollToMessage.ts    # 滚动到指定消息并高亮
```

**核心功能**：
- 从 ChatAnywhereMessagesContext 提取用户消息，过滤出已加载到 DOM 的消息
- 通过 MutationObserver 监听 DOM 变化，动态更新问题列表
- 滚动时自动追踪当前可见区域对应的问题（通过 getBoundingClientRect 计算）
- 点击导航点时平滑滚动到目标消息，并添加高亮闪烁效果
- 支持 `minQuestions` 参数控制最小显示数量（默认 1）

**使用位置**：`console/src/pages/Chat/index.tsx` 第 1263 行

| `console/src/stores/` | 状态管理 |
| `console/src/contexts/`, `console/src/hooks/` | 运行时上下文与钩子 |

## 请求进入系统的常见路径

```text
Console/HTTP Client
  -> app/routers/*
  -> middleware/*
  -> workspace/service_manager.py
  -> runner/runner.py

Message Channel
  -> app/channels/*/channel.py
  -> unified_queue_manager.py
  -> runner/runner.py
```

补充约定：`/console/chat` 的 SSE 事件流中，`object=response` 的终态帧（如 `status=completed` 或 `status=failed`）允许只推进状态字段而不重复携带 `output`。前端必须把这类空 `output` 终态帧视为合法结束信号，不能据此继续保持 loading，也不能清空已经渲染出的最后一条消息。

补充说明：控制台当前不再提供“回答后校验”驱动的继续执行卡片。聊天主链路完成后，前端只继续轮询 suggestions 等现有附加能力；额外自动续跑仅由 `Stop` 完成门禁的显式 `block` 触发。

## 关联功能域

- Agent 执行内核: [agent-and-orchestration.md](agent-and-orchestration.md)
- 安全与审批链路: [security-and-governance.md](security-and-governance.md)
