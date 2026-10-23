# Shell Python 依赖安装拒绝

## 识别反馈

工具结果的 `error_type` 为 `shell_policy_denied`，Agent 调度层同时标记工具治理状态 `blocked`。拒绝不会发起审批，也不会结束当前 Agent 回合。

- “平台策略禁止通过 shell 工具安装 Python 依赖，本次命令未执行。”：识别到 pip 系列或 `uv pip install` 的安装操作。
- “无法可靠检查该 shell 命令，本次命令未执行，请使用明确的命令写法重试。”：语法错误、动态命令名、未支持的选项形式、解析器不可用或其他无法可靠检查的输入。

本策略没有租户配置开关，关闭 Tool Guard 或预批准均不能放行。

## 定位入口

1. `src/swe/security/shell_installation_policy.py`：统一语法识别、安装命令集合与拒绝原因。
2. `ToolGuardMixin._acting_shell_installation_denial`：Agent 工具调度检查，覆盖来源级同名 shell 工具；Hook 改写后的输入在 `ask` 审批之前再次检查。
3. `prepare_shell_command`：内建前台、后台及内部直接调用的启动前检查。
4. `tests/unit/security/test_shell_installation_policy.py`：无需执行命令的识别矩阵；Hook runtime 与 shell preparation 测试验证执行边界。

## 输入边界

- 命中复合命令时整次拒绝，前置片段也不执行。即使 `false && pip install ...` 的安装分支不会运行，仍按可见安装子句拒绝。
- 普通 `echo 'pip install ...'` 或文档搜索不命中；`echo "$(pip install ...)"` 中的命令替换会命中。
- 识别路径、Python 版本、环境变量前缀、常用包装器，以及内联 shell、`eval`、`trap` 和重定向后的参数。
- `bash setup.sh` / `python install_deps.py` 的文件内容和程序内部子进程不在检查范围。这不是整个容器的安装隔离。
- 检查器使用 POSIX/Bash 语法；Windows 的 cmd.exe / PowerShell 输入尚无对应语法支持，按无法可靠检查拒绝。不得将 Bash 解析成功当作 Windows 输入安全。
- 已知但没有对应语法支持的解释器（fish、csh、PowerShell 等）也拒绝；同一次调用中创建命令别名会改变后续命令身份，按无法可靠检查拒绝。查询别名仍可通过。
- Tree-sitter 运行时依赖缺失时拒绝执行；需要在部署镜像构建时安装 `pyproject.toml` 声明的依赖。

## 排查顺序

先区分安装命中与无法检查，再核对 Hook 改写后的实际 `command`。动态命令名改为明确可执行文件名；脚本内部行为不属于本策略的识别承诺。解析器故障时修复镜像依赖，不通过关闭租户安全配置绕过。

运行回归：

```bash
venv/bin/python -m pytest tests/unit/security/test_shell_installation_policy.py tests/unit/agents/test_tool_guard_hook_runtime.py tests/unit/agents/tools/test_shell_preparation.py -q
```
