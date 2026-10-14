# Token 诊断排查入口

部署与字段口径见 [Token 部署说明](../../docs/deploy/token-diagnostics.md)。

| 现象 | 第一落点 |
| --- | --- |
| 界面 503 未启用 | SWE / Monitor 的有效 `SWE_TRACING_ENABLED`；B 还需两侧 `SWE_TOKEN_DIAGNOSTICS_CONTEXT_ENABLED`；两进程指向同一 MySQL database；24 张 Token 表是否已按完整初始 DDL 建立 |
| 400 / 403 | 网关是否核验并转发 X-Source-Id、X-Tenant-Id、X-User-Id，tenant 与 user 必须一致 |
| Trace 有数据，Token 无记录 | 仅新请求有 Token 明细；核对来源和租户；检查独立 writer 日志。Trace 成功不证明 Token 成功 |
| 数字显示未知 | 适配器未透传 usage、外部标题 API 无 usage、流中断或进程退出；不回退到 Trace 的 0 |
| 分类部分缺失 | 媒体不能使用文本密度估算；条目 / 字节 / 活跃预算已超限；角色来源不能拆出同一字符串中的技能或 Hook |
| 指纹规则证据不足 | 密钥为空、key version 不匹配、缺少 tool operation ID；不会用随机密钥或原文猜来源 |
| 一直 pending / failed | Monitor 独立 pool / analyzer；检查安全的异常类型日志、租约是否到期、data_revision 是否持续增加 |
| 任务结束仍不完整 | expected_attempt_count、expected_child_branches 与实际记录是否一致；gap_count；调用是否仍为 running / partial |
| 长任务规则证据不足 | 分支分析窗口上限；不会根据被截断的窗口宣布“无问题” |
| DDL / SQL 不符合数据库规范 | 检查是否使用修订后的 `scripts/sql/token_diagnostics_tables.sql`：24 张类型化表，主键及注释齐全，无 JSON 载荷；Monitor 使用显式列查询。初始 DDL 的 IF NOT EXISTS 不升级已有表 |
| Task 归因混淆 | 检查 `identity_source` / `execution_id`：AnswerTurn、实际 cron 执行或 token_generated；session / Trace 不作为任务键 |
| 恢复执行后预期次数过时 | 同一 Task/branch 的每次进入有独立 `capture_fragment_id`；`accounting_fragment` 事件分别保存预期次数。Store 在分支锁下读取并汇总片段，子分支期望取并集；未结束片段的分母为未知，片段读取超过 1000 时标记完整性无法验证。不要用已落库调用数反推预期分母 |
| 前缀不可用 / 首差未知 | B 开关、密钥、Context capture_state、request ended_at、同组条件及 24h/100 候选限制；B 是 prepared 文本编码有序 HMAC 块，不是最终渲染 Token 前缀。明确区分 no_history、missing_evidence、failed、pending、limited |
| 前缀翻页 400 | 除非法参数外，后台产生新 comparison 时查询层也会判定 cursor_snapshot_changed，HTTP 隐去细节返回通用 400。保留当前窗口/筛选，删除旧 cursor 从第一页重查，不能混合两次比较的候选 |
| 当前规则没有头 / 历史结果 stale | 比对 branch data_revision 与 head input_revision/snapshot/rule/threshold；业务 Step ID 仅在所属 Task/branch 内解释。过期或失败头不进入当前问题统计 |
| 引用过期 / 操作规模变化未知 | 对照引用的 Task/branch/Context；清理不级联其他分支。仅成功且 before/after 完整、表示及估算器一致才计算；summary call Usage 独立记账 |
| 看板总量不同于 Task 详情 | 窗口物理请求 started_at 与根 Task started_at、完整生命周期是不同口径；模型/实际执行 Agent 筛选贡献单列。不要用当前页求全局总量 |
| 执行 Agent 与根 Agent 筛选结果不同 | root_agent_id 按根归属选择 Task；agent_id 按生命周期匹配调用选择 Task，并只累计匹配调用的选中贡献。Task 生命周期总量仍含全部分支与模型 |
| 采集 partial/time_budget 或进程退出缺口 | 5ms 是协作预算；检查 256KiB/1024 块与队列预算。业务优先，writer timeout/取消不阻塞业务；尚未持久化的记录可能丢失，完整性不可确认时保持未知 |

本地 Python 回归使用项目 venv。Agent 依赖 `fcntl`，Windows 不能收集部分运行器 / SubAgent 测试；使用仓库 Linux venv 在 WSL 中执行。当前基线的 builtin_definition_provider 返回空列表，依赖 plan-researcher 的旧测试会失败；不要为通过 Token 测试擅自恢复内置专家。
