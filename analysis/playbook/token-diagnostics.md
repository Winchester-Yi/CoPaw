# Token 诊断排查入口

部署与字段口径见 [Token 部署说明](../../docs/deploy/token-diagnostics.md)。

| 现象 | 第一落点 |
| --- | --- |
| 界面 503 未启用 | SWE / Monitor 的有效 `SWE_TRACING_ENABLED`；两进程指向同一 MySQL database；11 张 Token 主表及关系明细是否已建 |
| 400 / 403 | 网关是否核验并转发 X-Source-Id、X-Tenant-Id、X-User-Id，tenant 与 user 必须一致 |
| Trace 有数据，Token 无记录 | 仅新请求有 Token 明细；核对来源和租户；检查独立 writer 日志。Trace 成功不证明 Token 成功 |
| 数字显示未知 | 适配器未透传 usage、外部标题 API 无 usage、流中断或进程退出；不回退到 Trace 的 0 |
| 分类部分缺失 | 媒体不能使用文本密度估算；条目 / 字节 / 活跃预算已超限；角色来源不能拆出同一字符串中的技能或 Hook |
| 指纹规则证据不足 | 密钥为空、key version 不匹配、缺少 tool operation ID；不会用随机密钥或原文猜来源 |
| 一直 pending / failed | Monitor 独立 pool / analyzer；检查安全的异常类型日志、租约是否到期、data_revision 是否持续增加 |
| 任务结束仍不完整 | expected_attempt_count、expected_child_branches 与实际记录是否一致；gap_count；调用是否仍为 running / partial |
| 长任务规则证据不足 | 分支分析窗口上限；不会根据被截断的窗口宣布“无问题” |
| DDL / SQL 不符合数据库规范 | 检查是否使用修订后的 `scripts/sql/token_diagnostics_tables.sql`：11 张类型化表，主键及注释齐全，无 JSON 载荷；Monitor 使用显式列查询。初始结构不提供旧 JSON Store 兼容 |
| Task 归因混淆 | 检查 `identity_source` / `execution_id`：AnswerTurn、实际 cron 执行或 token_generated；session / Trace 不作为任务键 |
| 恢复执行后预期次数过时 | 同一 Task/branch 的每次进入有独立 `capture_fragment_id`；`accounting_fragment` 事件分别保存预期次数。Store 在分支锁下读取并汇总片段，子分支期望取并集；未结束片段的分母为未知，片段读取超过 1000 时标记完整性无法验证。不要用已落库调用数反推预期分母 |
| 无法展示前缀复用量 / 首差位置 | 当前采集为消息 / Schema 条目 HMAC 与文本估算，没有最终渲染 Token 的有序前缀指纹；不能从整片 HMAC 还原最长公共 Token 前缀 |

本地 Python 回归使用项目 venv。Agent 依赖 `fcntl`，Windows 不能收集部分运行器 / SubAgent 测试；使用仓库 Linux venv 在 WSL 中执行。当前基线的 builtin_definition_provider 返回空列表，依赖 plan-researcher 的旧测试会失败；不要为通过 Token 测试擅自恢复内置专家。
