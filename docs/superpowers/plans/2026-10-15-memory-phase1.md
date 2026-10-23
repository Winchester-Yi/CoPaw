# Memory Phase 1 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 完成已确认的第一阶段内存生命周期及临时缓存优化。

**Architecture:** summary 在 manager 内管理在途任务和有限完成记录；SessionExecution 在退出时解除状态引用；临时文本各自限制容量，由单个 lifespan 服务周期清理。Redis 及第二阶段缓存不在范围内。

**Tech Stack:** Python 3.10+、asyncio、contextvars、FastAPI、pytest。

完整契约和参数以 `docs/superpowers/specs/2026-10-15-memory-phase1-design.md` 为准。

## Task A: Summary Task 与 Context

- [x] 对 BaseMemoryManager 的初始化、创建、计数、等待和兼容属性，以及 ReMe close/summary_memory、summary 命令运行 upstream impact，报告 UNKNOWN 或高风险并核对源码调用。
- [x] 在 `tests/unit/agents/test_memory_manager_async_summary.py` 添加完成即移除、轻量记录、等待竞态、取消、弱引用与关闭回归测试，先观察失败。
- [x] 在 `base_memory_manager.py` 实现在途追踪、有限完成记录和幂等结算；`summary_task_context.py` 实现明确 allowlist；ReMe 接入显式 trace 和关闭清理；命令支持已经完成的记录。
- [x] 运行 `PYTHONPATH=src ../../venv/bin/python -m pytest tests/unit/agents/test_memory_manager_async_summary.py -q`，通过后验证相关 tenant scope 和命令测试。

## Task B: 临时文本缓存

- [x] 对 `console_push_store.py`、`suggestions/store.py` 和 Console channel 待修改符号运行 impact 并复核消费方。
- [x] 为过期建议、无人读取清理、预算/覆盖/UTF-8/超长键、隔离和推送失败传播添加测试，确认旧实现失败。
- [x] 实现设计的 TTL、字节/条目/每 scope 预算、写入顺序淘汰和正确计数，暴露 `prune_expired()` 与 `reset()`。
- [x] 运行 `PYTHONPATH=src ../../venv/bin/python -m pytest tests/unit/app/test_console_push_store.py tests/unit/app/test_suggestions_store.py -q` 及新增缓存测试。

## Task C: 会话状态释放

- [x] 对 `SessionExecution.__aenter__` 和 `__aexit__` 做 impact，复核查询清理和提交顺序。
- [x] 在 `tests/unit/app/test_session_memory_release.py` 添加正常/异常/取消/进入失败/嵌套及重复 close 测试，先确认失败。
- [x] 退出与进入失败时将 `_state` 替换为空字典，不原地 clear，不修改持久化契约。
- [x] 运行新增测试及 `test_session.py`、`test_query_session_execution.py`、`test_session_state_merge_coordination.py`。

## Task D: 清理生命周期与集成

- [x] 对 `_start_lifespan_background_services`、`_shutdown_lifespan_resources` 运行 impact。
- [x] 在 `tests/unit/app/test_transient_memory_cleanup.py` 覆盖启动/停止幂等、清理隔离、错误恢复、取消和最终 reset 顺序；先观察失败。
- [x] 新增 `transient_memory_cleanup.py`：每60秒依次清理三类存储，异常隔离；lifespan 启停，生产方关闭后 reset。
- [x] 运行上述测试及现有生命周期测试，按实际变更扩展必要范围。

## Task E: 审查与交付

- [x] 独立审查设计覆盖，再审查并发安全、预算索引及错误处理；修复后重新验证。
- [x] 对全部修改的 Python 文件运行 Black 检查及必要范围 pytest，运行 `git diff --check`。
- [x] 运行 detect_changes（指定此 worktree）；若图不完整，如实记录并检查实际 diff，不能把零识别当成无影响。
- [x] 更新设计状态与 Playbook，记录测试结果、容量语义和运行时压测限制。
- [x] 同步代码至主工作区并保留 worktree 供审阅；不自动部署。
