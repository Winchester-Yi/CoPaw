# Token 记录与诊断部署

## 交付内容

SWE 保存任务、执行分支、模型请求尝试、输入数字明细与工具事件。Monitor 使用自己的连接池分析五类规则并提供查询；Console 的入口为“洞察中心 → Token 记录与诊断”，地址 `/analytics/token-diagnostics`。

B 增量增加 prepared Context 的有序文本表示、来源实例、前缀比较、操作前后证据、八规则不可变评估及当前头。三个只读页面为 `/analytics/token-overview`、`/analytics/token-executions/:taskId`、`/analytics/token-issues`；A 原入口保留。执行导航未指定 Task 时返回看板任务列表。

Token 的队列、ID、表、事务、连接池与清理均独立于 Trace。两者只共享启用条件和可空的 Trace 关联引用；删除 Trace 不删除 Token。数据保留期默认为 30 天。

## 上线顺序

1. 在尚未建表的 SWE / Monitor 共同访问 MySQL database 执行 `scripts/sql/token_diagnostics_tables.sql` 全量初始 DDL。共 24 张 `swe_token_*` 表（A 11 张+B 13 张），不修改 Trace 表。应用启动不会自动执行 DDL；`CREATE IF NOT EXISTS` 不会升级已有表，不作为迁移方案。
2. SWE 的 `SWE_DB_*` 与 Monitor 的 `MONITOR_DB_*` 必须指向上述 database。两个模块各自创建 1–2 个连接的小连接池，不借用已有事务连接。SubAgent 进程有自己的池，需要把并发进程数计入 MySQL 总连接预算。
3. SWE 和 Monitor 均配置 `SWE_TRACING_ENABLED=true`。SWE 读取 Tracing 的有效配置，Worker 继承父调用的启用快照。Trace 管理器创建或写入失败不会阻止 Token 采集。Monitor 配置未启用时接口返回 503。
4. 在 SWE 及所有 Worker 的环境配置 `SWE_TOKEN_DIAGNOSTICS_FINGERPRINT_KEY`，使用部署平台的密钥注入方式；不要提交到仓库。各实例使用相同密钥与 `SWE_TOKEN_DIAGNOSTICS_KEY_VERSION`（默认 `v1`）。Monitor 不需要指纹密钥。
5. 发布 SWE、Monitor 和 Console 新版本，先保持 `SWE_TOKEN_DIAGNOSTICS_CONTEXT_ENABLED=false`，验证 Monitor/DDL 和 A 查询；随后在 Monitor、SWE 及所有 Worker 启用该 B 开关并验证新请求、分析及三个页面。只有既有 Tracing 总开关和 B 开关同时启用才有 B 采集/服务。历史 Trace 不自动回填。

未配置指纹密钥时仍保存数字用量，重复内容和工具规则显示“证据不足”。轮换密钥时同步变更 key version；不同版本的指纹不用于关联内容。

## 初始表结构与采集版本

本结构仅用于尚未建表的部署，直接执行修订后的初始 DDL；没有旧 JSON 载荷迁移或兼容读写。所有表及字段包含注释、主键；多值关系由独立明细保存，查询显式列举字段。

五张主表为 `swe_token_runs`、`swe_token_model_calls`、`swe_token_input_items`、`swe_token_execution_events`、`swe_token_findings`。六张关系表为 `swe_token_expected_children`、`swe_token_rule_evaluations`、`swe_token_rule_missing_fields`、`swe_token_finding_calls`、`swe_token_finding_operations`、`swe_token_finding_sources`。

事件读取接受 `schema_version=1.0/1.1`；B 关闭继续产生 1.0，B 新证据采用 1.1。一次根执行建立一个 Task；同一 AnswerTurn 或定时执行复用身份，后续请求独立。会话和 Trace 仅为关联字段。子 Agent 和辅助调用共用 Task，独立保存分支。B 评估身份、自然唯一键及当前头包含分支 ID；不同分支复用业务 Step ID 不共享评估替换历史。

每个可观察模型尝试保存 Context ID、步骤 ID 和父分支；`messages_only` / `message_approx` 标明采集边界，不包含服务端模板、Tokenizer 或 Token 偏移。工具步骤保留实际开始/结束时间，安全属性缺声明时为 unknown/false。

## 可配置限额

| 环境变量 | 默认值 | 位置 |
| --- | --- | --- |
| `SWE_TOKEN_DIAGNOSTICS_MAX_RECORDS` | 1000 | SWE / Worker；活跃快照、排队与写入中的记录数量预算 |
| `SWE_TOKEN_DIAGNOSTICS_MAX_BYTES` | 16777216 | SWE / Worker；编码载荷预算，不能视为进程 RSS 上限 |
| `SWE_TOKEN_DIAGNOSTICS_RETENTION_DAYS` | 30 | Monitor；1–365 天 |
| `SWE_TOKEN_DIAGNOSTICS_KEY_VERSION` | v1 | SWE / Worker；指纹版本 |
| `SWE_TOKEN_DIAGNOSTICS_FINGERPRINT_KEY` | 空 | SWE / Worker；不写入记录或 Worker 启动快照 |
| `SWE_TOKEN_DIAGNOSTICS_CONTEXT_ENABLED` | false | SWE / Monitor / Worker；启用 B 增量，仍受总开关约束 |

默认每次采集最多 2000 个输入条目、512 KiB 条目载荷；每批最多 64 个事件、约 1 MiB。单次数据库写入最长 3 秒，内存记录重试期限 60 秒，关闭刷新预算 3 秒。超限先放弃输入条目并标记 partial；队列无法接收小型记录时记录已知缺口。进程崩溃可能丢失尚未落盘的数据，没有跨崩溃补偿保证。

规则扫描有界窗口：每个分支最多读取 256 次调用、10000 个输入条目和 1000 个工具事件。超过窗口时明确标记 `insufficient_evidence`，已有问题不会因缺少证据自动消失。单次调用的明细仍可独立分页查询。任务首页明细上限 1000 次调用 / 1000 个分支；超限的实际用量使用单独汇总查询，不拿明细窗口当完整总量。

清理每 60 秒执行一次，每次最多 20 个已过期分支；任务内任何活跃、近期结束或仍被分析租约保护的分支会保留该任务。优先清理子分支，再清理根记录。清理覆盖 24 张 Token 表；跨分支引用不级联删除，读取端标记证据过期。

B 固定预算：每次 Context 采集最多 256KiB 编码表示、1024 个 256-byte 块及协作式 5ms；时间耗尽返回 `partial/time_budget`，不是硬延迟保证。前缀只查当前请求之前已结束的同 source/tenant/Agent/Provider/model/阶段/表示/密钥版本请求，窗口 24h，最多比较 100 个候选，超限明确显示。单次分析/HTTP 读取预算为 5s；分析失败落库预算 3s。列表每页 1–100，时间过滤默认最近 24h，最长 30d。

B 启用时旧 A 全量输入明细降为 unavailable，真实 Usage 继续独立记录，避免再做无界输入序列化。摘要调用单独计入物理请求用量；操作 before/after 只有成功、完整、同表示及同估算器才显示规模变化。业务质量保持未知。

## 数字与诊断口径

- 实际输入 / 输出是本地适配器透传的 usage；缺失值为 null，接口明确报告的 0 保留为 0。失败或中断时保留已知部分，不伪造最后一次 usage。
- 流式 usage 按最近一次累计快照记录，不把累计值相加。RetryChatModel 可观察的每次尝试使用独立 call ID，同一次逻辑调用共享 logical ID。
- 缓存 / 推理字段属于报告总量的子项，展示时不重复相加。OpenAI ChatUsage 的 metadata 中可用子项会保留；适配器未透传的缓存、结束原因或内部重试无法恢复。此处统计不等于 Provider 完整计费账单。
- UTF-8 字节密度只用于输入估算；不加载 tokenizer，也不保存原文。媒体及嵌套媒体估算为未知。分类可按完整估算的比例对应实际输入，所有分摊值标 `≈`。
- 首期可靠分类来自准备输入的消息角色、工具 schema、tool operation ID 与显式来源标记。完整系统提示词可以归因到系统；已混入同一字符串的技能 / Hook / 知识内容不能可靠拆开，不通过语义猜测分类。
- 外部标题 API 的调用本身可观察，但其内部模型用量不可见；保存辅助调用和未知 usage，避免遗漏后仍宣称完整。
- 重复工具结果：至少 3 个不同逻辑调用携带同一 producer 与表示，首次之后估算累计至少 8000。
- 重复注入：同一调用出现相同指纹的系统 / 技能 / 注入内容，额外副本至少 1000。
- 工具重复：至少 3 个不同执行 ID 具有相同工具、参数与结果 / 错误；显式 polling 排除。结论为待复核，不判定死循环。
- 历史增长：同分支、用途、模型、估算器和压缩周期，至少 4 个逻辑调用，正基线增长至少 2 倍且至少 8000。没有明确阶段时只报告增长观察。
- 输出预算：已知有效上限达到 90% 时提示；明确 length 结束时报告截断事实。没有补写关联时不推断执行影响。

## 查询与身份边界

Monitor 路由前缀 `/api/token-diagnostics`；Console 经现有网关访问 `/api/monitor/token-diagnostics`。所有查询要求 `X-Source-Id`、`X-Tenant-Id`、`X-User-Id`，且 tenant 与 user 相同。接口不接受任意租户过滤器，也不信任客户端 `X-User-Role` 来扩大访问范围。

这些身份头必须由部署已有的认证网关核验、覆盖后转发；Monitor 应只对该网关开放。身份头一致性校验不能替代身份认证。直接暴露 Monitor、允许调用者任意伪造身份头，不具备可靠权限隔离。

| GET 路径 | 内容 |
| --- | --- |
| `/runs?limit=50&cursor=...&model=...` | 当前范围根任务列表，游标分页 |
| `/runs/{task_id}` | 任务汇总、分支、调用、分类、问题与覆盖状态 |
| `/runs/{task_id}/calls?branch=...&limit=100&offset=0` | 调用分页 |
| `/calls/{call_id}/inputs?category=...&limit=100&offset=0` | 输入数字明细分页 |
| `/runs/{task_id}/findings` | 已记录潜在问题（最多 500 项） |
| `/by-trace/{trace_id}` | 当前范围关联的独立 Token 任务（最多 100 项） |

没有当前范围的任务记录时返回 404，不查询其他租户。数据库不可用返回安全的 503 文案，不回传连接错误、地址或密钥。后台使用领取标识和 30 秒租约，每 10 秒续租；提交结果校验领取标识、租约与数据版本。计算期间新数据到达会保持 dirty / pending，继续分析。

## 验证与回退

B GET 路由前缀 `/api/token-diagnostics/v2`，Console helper 使用 `/monitor/token-diagnostics/v2`，沿用上述认证范围。端点为 dashboard、tasks、task branches/calls/operations、contexts、call prefix、issues、group instances、evaluations。不存在/外域 404，身份不一致 403，无服务/超时 503，非法过滤或游标 400；admin 不能扩大范围。游标绑定认证范围、过滤及资源，前缀候选游标还绑定 comparison；新快照替换后查询层判定 `cursor_snapshot_changed`，HTTP 返回安全的通用 400 文案；保留窗口/筛选从第一页重新查询。

看板物理尝试用量按请求 started_at 窗口；Task 列表按根 started_at 窗口，详情为完整生命周期。模型筛选贡献与 Task 全量分开展示。分布只包含终态且采集/Usage 完整的 Task，并显示样本数。前缀比例按文本字节加权，展示参与和排除分母；它不等同缓存命中率或节省。估算不等同实际 usage，null 不转成 0。

八规则 R01–R08 为上下文增长、内容持续携带、工具结果规模、安全工具重复、疑似循环、文本前缀稳定性、静态上下文规模、同类任务用量异常。结果区分 candidate/not_hit/insufficient_evidence/not_applicable，pending/failed 与 stale 单独展示。缺安全属性、真实 Step/进展、业务比较组时分别按 R04/R05/R08 合同降级。仅可信静态来源签名可聚合；其他候选按独立实例显示。建议包含前提、风险与验证步骤，仅提供记录和证据下钻。C 阶段的反馈闭环、策略执行、缓存/裁剪/停止、实验与质量归因暂不增加；逐请求 Provider 账单专项核对继续暂缓。

程序测试位于 `tests/unit/token_diagnostics/`。关系 fixture 默认执行 SQLite；真实 MySQL 需设置专用测试 `TEST_TOKEN_MYSQL_DSN`（如 `mysql://test_user:test_password@127.0.0.1:3306/test_token_diagnostics`）后运行：

```bash
venv/bin/python -m pytest tests/unit/token_diagnostics/test_relational_storage.py tests/unit/token_diagnostics/test_b_storage.py tests/unit/token_diagnostics/test_b_analysis.py tests/unit/token_diagnostics/test_b_retention.py tests/unit/token_diagnostics/test_mysql_lease_competition.py -q
```

上述 B 测试也复用该参数化 fixture。它创建并删除 UUID 后缀的隔离 schema，测试用户需要相应 CREATE/DROP 权限；不得使用业务连接。`test_mysql_lease_competition.py` 使用两个独立 aiomysql pool，同步实际候选读取后竞争同一租约，检查只有一个成功、过期替换后的旧 token 不能续租或发布、新 token 可以发布。无 DSN 时该用例跳过，不以 SQLite 验证竞争；本轮没有真实 MySQL 结果，发布前仍需执行上述实库验收。

前端测试位于 `console/src/pages/Analytics/TokenOverview/`、`TokenExecutions/`、`TokenIssues/`、`TokenDiagnosisShared/` 和旧 `TokenDiagnostics/`。旧 A 浏览器脚本 `scripts/testing/token_diagnostics_browser.py` 使用数字 API fixture；B 的 `scripts/testing/token_diagnostics_v2_fixture.py` 将隔离 SQLite 中的事实交给实际 Monitor reader 和 v2 路由，可用于三页面交互验收。它包含人工构造的分析状态，不证明真实规则产出或 MySQL 并发行为。

关闭配置并重启后停止新采集 / 分析；表与已有记录保留。回退旧应用不要求修改 Trace 表，但旧版 JSON Store 与本结构不兼容，须关闭旧版 Token 采集/分析。部署前仍需要在测试 MySQL 上执行 SQL、验证写入与 Monitor 跨进程分析，确认网关身份配置；本次本地开发没有操作业务数据库，也没有用真实 Provider 请求核对账单。
