# Agent Config Read Consistency

这个问题的核心不是单个 `/api/mcp` 接口，而是 agent 控制面在不同路径上混用了两种配置来源：

- `workspace/agent.json` 的磁盘配置
- `Workspace.config` 的进程内缓存快照

当写接口更新了 `agent.json`，但读接口仍从 `Workspace.config` 返回数据时，同一 tenant / agent 的连续读取就会出现旧值和新值来回切换，尤其容易在 MCP 页面频繁轮询时暴露。

## 一致性边界

本次变更只约束 agent 控制面的配置读取和 reload 作用域：

- agent-scoped 控制读接口统一以 tenant-scoped `workspace/agent.json` 为权威来源
- `Workspace.config` 继续保留给运行时对象装配和当前进程内的局部缓存，不再作为控制面返回值的唯一真源
- 所有 agent 配置写路径在 reload 时都必须显式带上 `tenant_id`
- `/daemon restart` 也必须重载正确的 tenant-scoped runtime identity
- heartbeat 的 scheduler 重排和运行时 `last_dispatch` 读取也必须显式带上 runtime `tenant_id`
- `default + X-Source-Id` 这类请求必须统一映射到 effective tenant，不能只用逻辑 tenant 做 reload 或 root config 读写

## 首先看哪里

出现 MCP、tools、running-config、channels、heartbeat 之类“写后读取不稳定”的问题时，优先检查这些位置：

- `src/swe/app/agent_context.py`
- `src/swe/app/routers/mcp.py`
- `src/swe/app/routers/tools.py`
- `src/swe/app/routers/agent.py`
- `src/swe/app/routers/config.py`
- `src/swe/app/utils.py`
- `src/swe/app/runner/daemon_commands.py`
- `src/swe/app/runner/command_dispatch.py`

## 典型回归信号

- 同一 tenant、同一 agent 连续 `GET /api/mcp` 返回结果前后不一致
- MCP create / update / toggle / delete 成功后，下一次 GET 仍读到旧配置
- 两个 tenant 共享同名 agent id 时，读到了彼此的 MCP / tools / running-config
- `/daemon restart` 执行成功，但实际重新加载的是 tenant-less 或错误 tenant 的实例
- `PUT /config/heartbeat` 写入成功，但 CronManager 重排后仍按别的 tenant 的 heartbeat 配置执行
- 默认租户带 `X-Source-Id` 更新 agent 后，内存中的 live runtime 没有被 reload，只有磁盘配置变了

## 和 `complete-console-agent-switching` 的关系

这次修复只解决后端配置一致性，不扩展 console 的 active-agent 产品协议。

- 本 change 负责“后端读哪份配置、reload 命中哪个 runtime”
- `complete-console-agent-switching` 负责“前端当前选中的 agent 如何与后端 active-agent 协议协同”

如果后续问题出在 agent 选择交互本身，而不是读源漂移或 tenant 作用域错误，应转去看 `complete-console-agent-switching`。

## 磁盘文件被覆盖的独立问题

上述控制面读取一致性约束并不保证手工编辑与所有写入者之间的事务一致性。
如果磁盘上的 `agent.json` 确实发生回写或重建，继续阅读
[agent-config-file-overwrite.md](agent-config-file-overwrite.md)：普通回复会为
`last_dispatch` 序列化整份配置；默认 Agent 引导校验失败时可能移走原文件并
从模板重建；当前 API 进程内锁也不能协调手工编辑及多个 Pod。
