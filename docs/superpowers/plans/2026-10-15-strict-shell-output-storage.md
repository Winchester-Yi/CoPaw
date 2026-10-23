# Shell 输出存储失败禁止内存回退

用户已确认：以严格失败代替完整输出回退。范围为已启用 spool 的 Unix/Linux 调用；Windows、未绑定 spool 的同步兼容路径保持原行为。不做压测。

## 设计

- `create_shell_output_spool` 在启动命令前验证目录并试建临时文件；配置/路径/权限/初始化失败抛 `shell_output_storage_failed`，不再返回 None。未绑定 spool 才返回 None。
- `_StreamBuffer.append` 写入失败/短写后进入终态并抛出存储错误；不 read_all、不扩大内存 buffer，后续 append 继续拒绝。文件等待正常清理关闭，便于读取有限预览。
- Unix 运行同时监视进程退出与 reader 异常；reader 落盘失败立即触发进程组终止。清理独立 task 经现有重复取消屏障等待，所有 reader/worker 完成后再关闭文件。
- 最终 artifact 创建、写入、flush/fsync、rename、展示读取失败，清理本次不完整文件并报告存储错误，保留真实退出码；移除 `_full_response`。
- 错误类型固定 `shell_output_storage_failed`，内容明确阶段、执行状态/退出码、完整输出不可用与不自动重试。预览最多 max_bytes 原始字节（stdout/stderr 分配），加固定诊断开销；读取/解码失败时省略预览，错误本身仍可返回。错误不携带 artifact 引用。
- 小输出仍按现有有界内存返回；不删除命令副作用，不自动重跑。正常成功/失败分类、编码、持久化、超时与实时帧协议保持。

## 实施

- [x] 新增初始化不执行、写入失败立即终止、失败后 buffer 不增长、有限预览、最终失败保留退出码、重复取消清理测试，先确认失败。
- [x] 修改 spool 和 Unix 运行监视，更新旧的完整回退断言为明确存储失败。
- [x] 回归 Shell spool、工具预算、Shell 工具与实时输出相关测试；修改文件 pre-commit。
- [x] 更新 Playbook 与旧方案中的现状说明，GitNexus detect_changes、diff check 后提交。

GitNexus impact 已执行：类级返回 CRITICAL 且包含无关前端映射，函数级多为 UNKNOWN。源码调用核对与线程/进程回归用于补足，不把图中的零命中视作无影响。

## 验证结果

- 新增故障测试先确认旧行为失败；补充管道 EOF 断言后复现了失败 reader 留下 196608 字节未读数据与 transport 延迟释放，再以有界丢弃排空修复。
- 直接相关回归：`test_shell_output_spool.py`、`test_tool_output_budget_mixin.py`、`test_shell_live_output.py`，74 passed。
- 扩大 Shell/租户/安全回归：190 passed、10 failed。使用 HEAD `27bd7c886` 的原始 `shell.py` / `shell_output_spool.py` 在同一环境复跑相关三文件，116 passed、相同 10 failed（引号、日志捕获、网络规则断言），未引入新的失败。
- 修改文件 pre-commit 全部通过（含 mypy、black、flake8、pylint）；`git diff --check` 通过。未运行压测。
- GitNexus impact/query 的图结果有错误符号映射、函数 UNKNOWN，不能据此证明安全；提交前仍执行 detect_changes，最终范围用暂存 diff 与源码核对。
