# Token 诊断 B 阶段测试报告

状态：本地验收完成，真实 MySQL 验收未运行。以下仅记录实际执行证据；测试范围为 Token 子系统及相关 Console，不代表全仓库回归或生产验收。

| 范围 | 最新实际结果 |
| --- | --- |
| 全 Token 子系统，项目 venv pytest | 403 passed / 1 skipped（MySQL 双 pool 竞争用例）；含五项观察器故障验收与真实子进程退出 |
| capture/writer/B runtime/storage/analysis/retention/routes/relational/process-exit 检视范围 | 181 passed；新增观察器验收独立 5 passed |
| queries/v2 routes/lifecycle/A routes | 74 passed |
| v2 API/Shared + A 原页面 + 总览/执行/问题/Sidebar 前端回归 | 82 passed，10 suites |
| Console build / typecheck / scoped ESLint / Prettier | 通过；构建有混合静态/动态导入及 >500KB 包警告 |
| 真实 MySQL 24 表/回滚/租约两实例/清理 | 未运行：未配置专用 DSN，无可用服务 |
| 总览真实 iframe 浏览器 | 1280/1440/1920/600，无页面横向溢出，hideMenu 生效；任务列表跳转实际聚焦 |
| 三页面实际集成与详情/问题浏览器 | 完整路径与精确返回已通过；三 Call、partial、503、stale、expired、limited、no-candidates/no-history、长实例 ID、键盘验证通过 |

## 故障与一致性回归

| 设计验收范围 | 实际测试入口与状态 |
| --- | --- |
| 起点前缀、短尾/UTF-8/首差、99–101 候选、隔离与版本、字节加权 | `test_prefix_analysis.py`、`test_context_capture.py`，已通过 |
| 真实准备请求、重试/来源独立实例、操作前后/摘要、故障缓冲 | `test_b_runtime.py`、`test_model_observer.py`、`test_b_fault_acceptance.py`，已通过 |
| 八规则门槛、空分母、安全/Step/进度/业务比较组不足 | `test_context_rules.py`，已通过；旧 `test_rules.py` 保留 |
| 24 表、历史/head/修订、失效租约、取消、保留清理 | `test_b_storage.py`、`test_b_analysis.py`、`test_b_retention.py`、`test_relational_storage.py`，SQLite/受控驱动已通过；MySQL 未运行 |
| UTC 窗口/生命周期/模型与 Agent、当前历史/游标、11 端点/身份 | `test_b_queries.py`、`test_b_routes.py`，已通过；旧 routes 保留 |
| 三页真实实例定位、返回原筛选/分页、状态、键盘与嵌入 | 前端行为测试及实际 reader 浏览器已通过；分页/身份越界/错误响应由单元合同回归覆盖 |

跨分支同业务 Step、规则、修订和快照首先复现仅一条评估，修复后有两条评估、两个独立当前头和分支内替换链。其他红→绿回归包括 Task 执行状态过滤、Step 引用范围、前缀候选分页不能混合 comparison、已分析但证据不足不能永久 pending，以及实际 aiomysql 方法受控取消/回滚不掩盖原信号。

SQL adapter 默认使用 SQLite；对真实 aiomysql 方法的受控读阻塞测试也不证明 MySQL 事务锁或两实例 lease 竞争。专用 TEST_TOKEN_MYSQL_DSN fixture 会创建/销毁 UUID schema，并用于 B storage/analysis/retention，禁止业务连接。新增 `test_mysql_lease_competition.py` 使用两个独立真实 pool，同步完成候选读取后竞争同一分支；验证一个 winner、旧 token 无法续租/发布、新 token 能发布，并关闭 pool 后销毁 schema。使用 Python 3.10 可用的 Event 同步。该用例本轮 1 skipped，不能宣称实库通过。

最终环境检查：`TEST_TOKEN_MYSQL_DSN` 未配置，`mysqld` 不可用；Docker executable 存在但服务 socket `/Users/shixiangyi/.orbstack/run/docker.sock` 不存在。未连接业务数据库。真实 MySQL 24 表、事务/唯一键、竞争及清理仍为发布前验收项，执行命令见部署说明。

真实子进程使用实际 DiagnosticWriter，输出调用方结果标记后直接 os._exit(23)，首次 flush 前未产生落盘文件。另用独立播种的事实验证实际 Task reader 保留预期 1 / 观察 0、不完整及 actual_total=null。该回归单项通过；它不等同于业务模型的端到端崩溃恢复，也不验证写入已开始时被杀的数据库恢复。

新增五项观察器故障验收通过：缺 key、字节预算、时间耗尽、队列满、实际写入超时。使用真实 Collector/LogicalObserver/AttemptObserver 验证返回对象身份；可保留的 Usage 独立存在，缺输出保持 null，队列满不伪造 Usage；计数/字节缓冲有界。采集时钟受控，数据库超时用真实 asyncio timeout；独立检视 READY。首次两条失败为测试对部分文本规模的错误假设，修正为已采集前缀与未知完整条目数，不是生产行为修复。

浏览器 fixture 由实际 TokenStore 写入隔离 SQLite，再由真实 Monitor 查询读取，含三次调用、三份 Context、前缀 comparison/candidates、operation before/after/summary、评估→问题组→实例→详情关联。它包含人工构造的分析状态，用于渲染和交互验收，不证明规则在真实请求下形成这些候选，也不是 MySQL 或生产数据证明；前缀和操作字段按实际证据合同对齐，未知值仍保持 null。

## 三页面验收

真实 iframe 中完成总览→Task→Call 2/3/1 切换→返回总览→R01 问题→组→实例→Call 3 执行证据→返回问题。最后返回 URL 与进入详情前完全一致，包含 UTC 窗口、rule/state、组和实例；独立行为测试覆盖游标隔离及 hash 保留。Call 3 输入 3000、输出和真实总量未知。操作 before 200 / after 100 的估算变化与摘要调用 Usage 分开展示。

三页分别在 1280/1440/1920/600 的实际 iframe 下检查，document scrollWidth 等于 innerWidth，hideMenu 生效，Call/问题组可用 Enter 选择。在实际 reader 响应上受控注入 503、历史/引用过期、候选受限、no_history 和 255 字符 instance_key：覆盖 503 保留可用问题组，不展示覆盖指标、不伪造零覆盖；问题 503 无组且不展示“没有候选”；历史/过期实例执行链接数为 0，详情仍可读；受限及无历史提示可见；长实例窄屏无页面横向溢出。状态注入只验证 UI 合同，后端形成相应状态的条件由 Python 测试验证。

浏览器未出现 pageerror。控制台包含依赖 forwardRef/CSS SSR 警告及受控 503 网络日志；jsdom 测试有 getComputedStyle pseudo-element 未实现提示。构建的混合 import 和包体积警告未消除；没有将这些告警计为功能验收失败。未进行真实 Windows/Edge 或生产网关联调。截图保留在本地 `output/playwright/`，未随代码提交打包。

最终页面复核修复了三类证据错绑和两类筛选/定位问题：历史 Context revision 不复用当前字节偏移；操作前后验证所属操作、角色和 Task/branch；返回默认 UTC window 固定，异步 window 回填保留微秒，已固定窗口刷新和元数据变化不清空未提交草稿；缺失 group/finding 不回退到其他实例。规范与质量复审 READY，Task 13 提交 `46452deeb`。

## 最终复跑命令

按批准设计验收表逐项核对：

| 场景 | 结论与证据 |
| --- | --- |
| 同起点与中间重复 | 通过；`test_prefix_analysis.py` |
| 单条目末尾、短尾、UTF-8、首差字节区间 | 通过；`test_prefix_analysis.py` / `test_context_capture.py` |
| 99/100/101 候选、同时间、未来/运行中历史 | 通过；前缀及 B 查询回归；受限 UI 注入验证 |
| 租户/模型/表示/密钥版本变化 | 通过；采集、前缀、查询与路由范围回归 |
| 15%–25% / 上界19% 的 R06 判断 | 通过；`test_context_rules.py` |
| 重试、相同内容不同来源、调用缺口 | 通过；runtime / observer / rules 回归 |
| 操作前后、新消息、独立摘要 Usage、缺侧 | 通过；storage / queries / runtime / 执行页与浏览器 |
| 工具安全/Step/进展/质量缺失降级 | 通过；`test_context_rules.py`；没有补造业务证据 |
| 八规则边界、零/空分母、阈值版本 | 通过；`test_context_rules.py` / `test_b_storage.py` |
| 队列/预算/DB timeout/取消/退出 | 本地通过；fault / writer / analysis / process-exit；崩溃恢复范围见上文 |
| 多实例竞争与评估重试 | 历史/幂等/失效 lease 本地通过；真实双 pool MySQL 用例已提供但未运行 |
| 晚到、分析落后、保留清理 | SQLite/受控驱动通过；MySQL 清理未运行 |
| 加权比例、跨窗口、模型/Agent 贡献 | 通过；`test_b_queries.py` 与总览/执行页合同 |
| 三页导航、精确返回、错误与授权 | 通过；routes、前端合同及实际 reader iframe；生产网关未验证 |

```bash
venv/bin/python -m pytest tests/unit/token_diagnostics/ -q
pnpm --dir console test:run src/pages/Analytics/TokenOverview src/pages/Analytics/TokenExecutions src/pages/Analytics/TokenIssues src/pages/Analytics/TokenDiagnosisShared src/pages/Analytics/TokenDiagnostics src/api/modules/tokenDiagnosisV2.test.ts src/layouts/Sidebar.test.tsx
pnpm --dir console run build
```

上述结果分别为 403 passed / 1 skipped、82 passed、exit 0；build 同时执行 tsc。最终改动文件通过 ESLint/Prettier、Python Black（79 列）/flake8 仓库忽略项和 `git diff --check`，提交钩子按显式暂存文件执行。

## 采集预算实测

2026-10-13 本地真实时钟，每组 100 次；数字取最近复测，受机器负载影响。原始结果 `output/playwright/token-b-capture-benchmark.json`。

| 输入 | median / p95 / max（ms） | 结果 | 超过 5ms |
| --- | --- | --- | --- |
| 小文本 | 0.022 / 0.025 / 0.087 | 100 complete | 0 |
| 5 条消息约 60KB | 4.267 / 4.375 / 4.609 | 100 complete | 0 |
| 13MB | 5.024 / 5.031 / 5.034 | 100 partial/time_budget | 100 |
| 缺 key | 0.004 / 0.004 / 0.033 | 100 unavailable/missing_key | 0 |

13MB 最大输出 72,192 编码字节、282 块、1 occurrence；未遍历完整输入补分母。5ms 是协作检查预算，不能作为硬延迟上限。

## 检查限制

GitNexus 提交前 detect_changes 均无 partial/truncated，但动态派发和跨语言消费者未被图完整解析，UNKNOWN 以定向源码检索补证。Python 3.14 提交钩子的 Pylint/Astroid 标准库解析崩溃，使用临时 Python 3.12 配置执行同规则；仓库配置未修改。仅检查本轮文件，用户文件未暂存。
