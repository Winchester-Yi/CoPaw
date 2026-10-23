# Shell 执行前准备的 worker 边界

用户已确认将 Shell 同步准备移入 worker，保留租户上下文与安全校验，loop 启动子进程。使用当前开发分支，不改后台进程同步入口，不做压测。

## 设计与验收

复用 `run_runtime_state_work`（8 个进程级提交许可、ContextVar 复制），将 `prepare_shell_command`、运行时路径守卫进入、进程限制配置读取作为一次准备。同步 API 保留。策略返回后在 loop 构造 preexec 闭包、获取租户执行许可、启动进程；环境、校验结果及策略均来自本次准备，不增加全局缓存。

准备可能创建守卫临时文件，因此由调用方持有 ExitStack；独立准备 task 保留结果与异常，取消不会取消该 task。无论准备失败、准备期间取消、启动失败还是正常结束，都等待准备 task 真正完成，再由 runtime-state worker 关闭守卫；重复取消使用现有清理屏障。取消准备后绝不启动进程。准备异常仍从原准备边界传播，工具失败类型不变。守卫清理保持在命令和子进程结束之后。

## 实施步骤

- [x] 在 `tests/unit/agents/tools/test_shell_preparation.py` 添加线程/上下文与 loop 启动测试：包装真实 prepare、路径校验、环境构造、守卫创建、策略解析，记录线程；并发租户各自得到正确工作目录/环境/策略。测试应先因 loop 执行准备而失败。
- [x] 添加阻塞准备、重复取消测试：threading.Event 控制 worker 完成，确认不启动进程、清理在 worker、准备线程完成前不退出。覆盖准备/策略失败、进程启动失败清理。
- [x] 在 `shell.py` 新增同步准备 helper 和拥有 ExitStack 的异步 context manager，使用 `run_runtime_state_work` 与 `await_shell_cleanup_task`；`execute_shell_command` 改用异步上下文，保留原执行和 spool 清理顺序。
- [x] 使用 `venv/bin/python -m pytest` 运行新测试、Shell spool、实时输出、预算、租户边界、进程限制和后台进程相关测试；将既有失败与 HEAD 对照。
- [x] 更新 `analysis/playbook/event-loop-worker-boundaries.md`，运行修改文件 pre-commit、GitNexus detect_changes、暂存 diff 检查后提交。

GitNexus impact：`execute_shell_command` 与 `prepare_shell_command` 均为 UNKNOWN，query 映射错误，不能将零命中视为无影响。源码搜索确认同步 API 还由后台进程使用，该 API 本轮不改。

## 验证记录

- RED：真实准备线程测试与 policy/startup 清理测试共 3 项失败，确认全部准备仍在 loop、策略读取失败未清理守卫。迁移后通过。
- 直接相关测试：新准备测试、spool、实时输出、工具预算、后台进程、runtime worker，94 passed；包含准备期间重复取消、清理 worker 期间重复取消、并发 tenant/source 环境/策略、拒绝启动与真实守卫目录删除。
- 扩大范围初次结果 196 passed、3 failed（最后新增一项清理取消测试在上述 94 项范围通过）。以 HEAD `f2ffc9100` 原始 shell 模块在同一环境运行进程限制与租户边界测试，103 passed、相同 3 failed：CPU 默认值 90 与测试预期 30 不一致的两项断言，以及一项 opencli 引号断言。均为既有失败，不修改无关配置或安全协议。
- 最终扩大回归排除上述 3 项已确认基线失败后：197 passed、3 deselected。修改文件 pre-commit 通过，包含 mypy、black、flake8、pylint。未运行压测；线程测试证明执行边界，不代表生产吞吐收益。

GitNexus 提交前检查：MCP all/staged 和 CLI staged 均已执行；CLI 明确提示 touched files 与索引符号未重叠，不能视为 clean tree。实际暂存范围经 diff 核对仅为本次 4 文件，依靠真实线程/取消/租户/兼容行为回归补足验证，图映射问题未纳入本次修复。
