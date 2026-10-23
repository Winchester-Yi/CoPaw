# Python 依赖安装执行前拦截

状态：用户已确认完整设计并要求继续；已实施并完成本次交付验证，尚未提交或部署。

## 已确认决策

- 范围为 Python 依赖安装，包括 `pip install`、`pip3 install`、`python -m pip install`、`uv pip install`。
- `pip list`、`pip show` 等查询不属于本策略的拦截目标；本策略不扩展到 npm、pnpm 或系统包安装。
- 命中时拒绝本次 shell 工具调用并返回原因，不发起审批，不结束当前 Agent 回合，Agent 可以继续处理任务。
- 识别实际安装操作，而非任意文本子串：`echo 'pip install requests'`、搜索文档中的安装命令等普通文本引用允许通过本策略。
- 策略为平台强制政策，所有租户、主 Agent 和 SubAgent 均受约束，租户不能关闭。
- 本期检查提交命令中可见的安装操作，包括嵌套 shell 命令与命令替换；不追踪脚本文件内容或程序内部启动的安装行为。
- 复合命令一旦命中，整次工具调用在执行前拒绝，不运行任何前置片段，也不删除安装片段后执行剩余部分。
- 命令集合限定为 pip 系列与 `uv pip install`，覆盖 `-r`、`--upgrade`、`-e` 等安装形式；本期不增加 `uv add`、`uv sync`、Poetry、Pipenv 或 Conda 的安装命令。
- 识别常见静态变体：可执行文件路径前缀、Python 解释器版本、环境变量前缀、常见命令包装器及全局参数位置变化；不依赖固定文本子串匹配。
- 无法解析命令语法、无法确定命令名或无法可靠完成策略检查时，整次调用拒绝并说明原因，Agent 可以用明确写法重新提交。
- 默认前台 shell、后台 shell 以及来源级同名 shell 覆盖工具均受约束。
- 对应领域术语为 [Python Dependency Installation Denial](../../../CONTEXT.md)。

## 现有能力与约束

- `src/swe/agents/tool_guard_mixin.py` 的 `_apply_pre_tool_hook` 已支持 `PreToolUse` 返回 `deny` / `block` 后拒绝调用并允许 Agent 继续；`ask` 和 `stop` 分别是审批与结束回合语义。
- 现有 Tool Guard 自定义规则产生风险检测结果，可能进入人工审批，不能单靠新增高风险正则表达上述拒绝策略。
- 内置 shell 工具通过 `command` 字段接收命令。检查该字段本身不等于检查脚本内容或约束其所有后代进程。
- `wiki/hook/pre-tool-use-command-demo/` 已有命令 handler 返回拒绝决策的示例，`wiki/hook/conditional-pre-tool-use-demo/` 已有按工具匹配与 `failPolicy: block` 的配置示例，可参考其拒绝反馈语义。
- 普通租户 / Agent 可配置 Hook 不能单独构成已选定的平台强制政策；执行前检查必须不能被租户关闭或借参数改写绕过。
- `prepare_shell_command` 是内建普通 shell 与后台 shell 的共同启动前入口，但来源级工具的同名 shell 覆盖不经过此入口。`_acting_policy_denial` 已有在 `PreToolUse` 前后重复校验策略的模式，可供强制检查参考。
- 现有 shell 拦截器只处理部分命令拆分，Plan Mode 使用 `shlex` 并拒绝复合语法；二者均不是现成的完整 shell 语法识别器。
- 本策略只承诺上述可见输入边界内的平台强制检查，不承诺阻止平台中任意程序安装依赖。对嵌套命令的识别也不意味着放宽已有的路径或命令执行限制。

## 设计方案

### 强制执行边界

使用平台拥有的统一检查逻辑，不依赖租户可关闭的 Hook 或 Tool Guard 开关。Agent 调度边界检查 shell 调用，覆盖来源级同名工具；内建 shell 的共享准备入口执行相同检查，覆盖前台、后台和内部直接调用。

检查必须覆盖 `PreToolUse` 参数改写后的最终命令，并早于审批、预批准或其他执行放行分支。拒绝结果沿用现有工具治理反馈路径，不新增审批入口，不终止 Agent 回合。

### 命令识别

以 shell 语法和命令参数结构识别安装操作，不通过执行命令、展开变量或加载脚本来判断。区分命令位置、普通字符串参数和会执行的命令替换；递归检查可见的嵌套 shell 命令。

识别明确的 pip 安装子命令，包括前述路径、版本、包装器和选项变体。全局参数的值即使等于 `install` 也不能误判为安装子命令。能静态还原的引用或拼接按还原后的命令判断；命令名依赖动态求值而无法可靠判断时拒绝。

复合命令做执行前检查，不尝试预测条件分支或先运行安全片段。因此 `false && pip install requests` 也整次拒绝；安装子句在提交的命令中可见即可命中。

解析不支持的语法、解析异常或检查器故障均拒绝本次调用；不降级为子串匹配或默认放行。具体解析库与实现拆分留给实施阶段，不扩大已确认的覆盖边界。

### 拒绝反馈

- 命中安装操作：说明“平台策略禁止通过 shell 工具安装 Python 依赖，本次命令未执行”。
- 无法可靠检查：说明“无法可靠检查该 shell 命令，本次命令未执行，请使用明确的命令写法重试”。
- Agent 可使用已安装依赖或其他符合平台策略的方案继续处理任务；普通用户审批不能放行被拒绝的调用。

## 验收场景

表中的“通过”仅表示本策略不拒绝，其他既有路径、安全与工具策略继续生效。对嵌套 shell 的案例可在分析器层面验收，不要求现有执行边界允许其运行。

| 输入 / 场景 | 本策略结果 |
| --- | --- |
| `pip install requests`、`pip3 install -r requirements.txt` | 拒绝 |
| `python -m pip install --upgrade requests`、`uv pip install -e .` | 拒绝 |
| `/venv/bin/pip install requests`、`python3.11 -m pip install requests` | 拒绝 |
| `env X=1 pip install requests`、`sudo pip install requests`、`command pip install requests` | 拒绝 |
| `pip --disable-pip-version-check install requests` | 拒绝 |
| `pip --log install list`，其中 `install` 是日志文件参数 | 通过 |
| `mkdir output && pip install requests && python main.py` | 整次拒绝，`mkdir` 也不执行 |
| `false && pip install requests`、管道 / 分号 / 换行中包含安装子句 | 整次拒绝 |
| `bash -c 'pip install requests'` | 拒绝 |
| `echo "$(pip install requests)"` | 拒绝，命令替换不是普通文本引用 |
| `echo 'pip install requests'`、`rg 'pip install' README.md` | 通过 |
| `pip list`、`pip show requests`、`python -m pip list` | 通过 |
| `pip uninstall requests`、`uv sync`、`poetry install`、`npm install` | 不属于本策略的安装命令集合 |
| `bash setup.sh`、`python install_deps.py` | 不读取文件内容，不追踪其内部安装行为 |
| `$INSTALLER install requests`、无法解析的 shell 语法、检查器故障 | 无法可靠检查，整次拒绝 |
| `echo "$INSTALLER install requests"` | 通过，动态参数不等于动态命令名 |
| Hook 将普通命令改写为安装命令 | 在执行前拒绝最终命令 |
| 已预批准调用、Tool Guard 关闭、不同租户或 Main Agent / SubAgent | 不影响本策略强制拒绝 |
| 前台、后台及来源级同名 shell 覆盖中的安装命令 | 均拒绝 |

## 验证与交付

实施前按仓库要求对拟修改符号执行 GitNexus 影响分析。以验收表验证识别逻辑，再验证实际执行入口未启动被拒绝的命令、复合命令无前置副作用、无审批请求且 Agent 能继续。

运行相关 Python 测试时使用项目虚拟环境。提交前执行必要检查与 GitNexus 变更检测；在实现完成前同步补充相关 playbook 的定位与排查说明。

## 最终确认

上述逐项决策来自用户 Q1–Q10 的选择；用户随后要求“继续”，进入实现。实现使用 Tree-sitter Bash 语法树；Windows 输入没有对应解析器时按无法检查拒绝。运行时要求安装 `pyproject.toml` 中新增的两个解析依赖。

平台策略在 Agent 调度与共享 shell 准备入口生效；Hook 改写后的安装命令在 `ask` 审批之前拒绝。实现排查入口记录在 [playbook](../../../analysis/playbook/python-installation-denial.md)。

## 交付验证记录

- 最终核心回归：324 通过，覆盖识别矩阵、前后台无启动副作用、Hook 改写、来源级同名适配器、预批准及拒绝后继续调用。
- 扩大相关回归：590 通过、11 失败。失败涉及 OpenCLI 凭据引号 / 日志预期、既有工具注册集合及网络传输规则；使用 HEAD 中的原始 shell 与 Tool Guard mixin 模块重跑相同 11 项，全部复现，未修改这些无关问题。
- 新增检查器、识别测试及依赖声明通过完整的相关 pre-commit 检查；已有接入文件通过相关静态检查，其中原有逗号格式保持不变、单独跳过该格式钩子。检查器函数圈复杂度均不超过 15。
- 独立代码审查发现的重定向参数、shell `-c` 选项、内联 action 和 `eval --` 等问题均补充失败回归后修复，并完成复核。
- GitNexus 索引及全文索引已重建；变更检测已包含新增文件。已有执行路径接入点与新增策略 / 测试符号符合预期，风险为 HIGH，符合平台统一执行前约束的作用范围。
- `git diff --check` 通过；未提交或部署。既有未完成的其他工作区修改予以保留。
