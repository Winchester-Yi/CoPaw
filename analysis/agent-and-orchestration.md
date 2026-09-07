# Agent 编排与执行内核

本文档聚焦 `src/swe/agents/`，说明 Agent 的核心对象、提示词拼装、工具体系、技能管理和内存子系统。

补充说明：回合级执行收口在 `src/swe/app/runner/runner.py`。当前后处理链路以 `Stop` 完成门禁、suggestions 与 tracing 为主；历史上的“回答后校验 -> 自动续跑”逻辑已退役，不再参与正常请求路径。

## 目录结构

| 目录/文件 | 作用 |
|-----------|------|
| `src/swe/agents/react_agent.py` | 主 Agent 实现 |
| `src/swe/agents/tool_guard_mixin.py` | 工具调用前的治理拦截层 |
| `src/swe/app/runner/operation_group.py` | Agent 显式操作组声明的校验、展示投影与审批重放辅助 |
| `src/swe/agents/model_factory.py` | 模型实例与格式化器装配 |
| `src/swe/agents/routing_chat_model.py` | 模型路由封装 |
| `src/swe/agents/prompt.py` | 系统提示构造 |
| `src/swe/agents/schema.py` | Agent 相关结构定义 |
| `src/swe/agents/command_handler.py` | 命令处理 |
| `src/swe/agents/skills_manager.py`, `src/swe/agents/skills_hub.py` | 技能扫描、加载、分发；创建、导入、启用和技能池刷新前通过 `scan_skill_directory()` 进入安全扫描闸口 |
| `src/swe/agents/hooks/` | Bootstrap、Memory Compaction、Tracing 等 Hook |
| `src/swe/agents/memory/` | Agent Markdown、短期/轻量记忆管理 |
| `src/swe/agents/tools/` | 文件、Shell、浏览器、截图、媒体、时间、Token 等内置工具 |
| `src/swe/agents/utils/` | 消息处理、文件处理、Token 计数等辅助能力 |

## 关键文件清单

### 核心 Agent

- `src/swe/agents/react_agent.py`
- `src/swe/agents/tool_guard_mixin.py`
- `src/swe/agents/model_factory.py`
- `src/swe/agents/routing_chat_model.py`
- `src/swe/agents/prompt.py`
- `src/swe/agents/schema.py`

### 技能与 Hook

- `src/swe/agents/skills_manager.py`
- `src/swe/agents/skills_hub.py`
- `src/swe/agents/hooks/bootstrap.py`
- `src/swe/agents/hooks/memory_compaction.py`
- `src/swe/agents/hooks/tracing.py`

### 记忆与工具

- `src/swe/agents/memory/agent_md_manager.py`
- `src/swe/agents/memory/base_memory_manager.py`
- `src/swe/agents/memory/reme_light_memory_manager.py`
- `src/swe/agents/tools/file_io.py`
- `src/swe/agents/tools/file_search.py`
- `src/swe/agents/tools/shell.py`
- `src/swe/agents/tools/browser_control.py`
- `src/swe/agents/tools/browser_snapshot.py`
- `src/swe/agents/tools/desktop_screenshot.py`
- `src/swe/agents/tools/view_media.py`
- `src/swe/agents/tools/memory_search.py`
- `src/swe/agents/tools/get_current_time.py`
- `src/swe/agents/tools/get_token_usage.py`

## 执行链路

```text
Runner 接收请求
  -> 构造 Agent 上下文与模型
  -> 生成系统提示
  -> 进入 ReAct 循环
  -> 工具调用前经过 Tool Guard
  -> 从执行参数副本剥离操作组展示元数据
  -> 工具 / 技能 / 记忆协同
  -> 输出响应并回写会话状态
```

## 扩展点

| 扩展点 | 推荐位置 |
|--------|----------|
| 新工具 | `src/swe/agents/tools/` |
| 新技能装配逻辑 | `src/swe/agents/skills_manager.py` |
| 新 Hook | `src/swe/agents/hooks/` |
| 新记忆实现 | `src/swe/agents/memory/` |

## 关联功能域

- 多租户上下文与目录隔离: [config-and-tenant-isolation.md](config-and-tenant-isolation.md)
- 工具审批和安全边界: [security-and-governance.md](security-and-governance.md)
