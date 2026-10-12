# Token 记录与诊断部署

## 交付内容

SWE 保存任务、执行分支、模型请求尝试、输入数字明细与工具事件。Monitor 使用自己的连接池分析五类规则并提供查询；Console 的入口为“洞察中心 → Token 记录与诊断”，地址 `/analytics/token-diagnostics`。

Token 的队列、ID、表、事务、连接池与清理均独立于 Trace。两者只共享启用条件和可空的 Trace 关联引用；删除 Trace 不删除 Token。数据保留期默认为 30 天。

## 上线顺序

1. 在 SWE / Monitor 共同访问的 MySQL database 执行 `scripts/sql/token_diagnostics_tables.sql`。SQL 仅创建五张 `swe_token_*` 表，不修改 Trace 表。应用启动不会自动执行 DDL。
2. SWE 的 `SWE_DB_*` 与 Monitor 的 `MONITOR_DB_*` 必须指向上述 database。两个模块各自创建 1–2 个连接的小连接池，不借用已有事务连接。SubAgent 进程有自己的池，需要把并发进程数计入 MySQL 总连接预算。
3. SWE 和 Monitor 均配置 `SWE_TRACING_ENABLED=true`。SWE 读取 Tracing 的有效配置，Worker 继承父调用的启用快照。Trace 管理器创建或写入失败不会阻止 Token 采集。Monitor 配置未启用时接口返回 503。
4. 在 SWE 及所有 Worker 的环境配置 `SWE_TOKEN_DIAGNOSTICS_FINGERPRINT_KEY`，使用部署平台的密钥注入方式；不要提交到仓库。各实例使用相同密钥与 `SWE_TOKEN_DIAGNOSTICS_KEY_VERSION`（默认 `v1`）。Monitor 不需要指纹密钥。
5. 发布后端、Monitor 和 Console。验证新请求出现记录，并等待后台分析更新。历史 Trace 不自动回填。

未配置指纹密钥时仍保存数字用量，重复内容和工具规则显示“证据不足”。轮换密钥时同步变更 key version；不同版本的指纹不用于关联内容。

## 可配置限额

| 环境变量 | 默认值 | 位置 |
| --- | --- | --- |
| `SWE_TOKEN_DIAGNOSTICS_MAX_RECORDS` | 1000 | SWE / Worker；活跃快照、排队与写入中的记录数量预算 |
| `SWE_TOKEN_DIAGNOSTICS_MAX_BYTES` | 16777216 | SWE / Worker；编码载荷预算，不能视为进程 RSS 上限 |
| `SWE_TOKEN_DIAGNOSTICS_RETENTION_DAYS` | 30 | Monitor；1–365 天 |
| `SWE_TOKEN_DIAGNOSTICS_KEY_VERSION` | v1 | SWE / Worker；指纹版本 |
| `SWE_TOKEN_DIAGNOSTICS_FINGERPRINT_KEY` | 空 | SWE / Worker；不写入记录或 Worker 启动快照 |

默认每次采集最多 2000 个输入条目、512 KiB 条目载荷；每批最多 64 个事件、约 1 MiB。单次数据库写入最长 3 秒，内存记录重试期限 60 秒，关闭刷新预算 3 秒。超限先放弃输入条目并标记 partial；队列无法接收小型记录时记录已知缺口。进程崩溃可能丢失尚未落盘的数据，没有跨崩溃补偿保证。

规则扫描有界窗口：每个分支最多读取 256 次调用、10000 个输入条目和 1000 个工具事件。超过窗口时明确标记 `insufficient_evidence`，已有问题不会因缺少证据自动消失。单次调用的明细仍可独立分页查询。任务首页明细上限 1000 次调用 / 1000 个分支；超限的实际用量使用单独汇总查询，不拿明细窗口当完整总量。

清理每 60 秒执行一次，每次最多 20 个已过期分支；任务内任何活跃、近期结束或仍被分析租约保护的分支会保留该任务。优先清理子分支，再清理根记录。清理只操作五张 Token 表。

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

程序测试位于 `tests/unit/token_diagnostics/`。前端测试位于 `console/src/pages/Analytics/TokenDiagnostics/`。浏览器验证脚本 `scripts/testing/token_diagnostics_browser.py` 渲染真实 Console 路由，使用数字 API fixture，不访问业务数据库；覆盖三种桌面尺寸、hideMenu、任务 / 调用选择和刷新。

关闭配置并重启后停止新采集 / 分析；表与已有记录保留。回退旧应用不要求修改 Trace 表。部署前仍需要在测试 MySQL 上执行 SQL、验证写入与 Monitor 跨进程分析，确认网关身份配置；本次本地开发没有操作业务数据库，也没有用真实 Provider 请求核对账单。
