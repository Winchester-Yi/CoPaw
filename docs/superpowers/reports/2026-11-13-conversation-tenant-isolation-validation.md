# 租户独立迁移与上线：实现核验记录

日期：2026-11-13。实现提交：`aedfa7bb8`。范围是 `.worktrees/conversation-mysql-nas`，未部署生产、未发送外部消息、未访问业务表进行业务数据检查。数据库写入限随机 `test_conversation_*` 测试库，文件测试限本地临时目录；按用户要求未运行万级基线或真实 NAS 压力测试。

## 已实现

- 显式 `discover --tenant-isolation`，按 service+tenant 聚合同账号所有 source/Agent/workspace/share；失败账号保留可信归属、阶段、代码和安全原因，不阻断可证明无关的正常账号。
- SQL 租户状态、独立校验和发布、运行时动态准入、多实例无需重启补迁；未发布/未注册账号 503 且无 legacy fallback。
- 已发布 A 先跳过旧源读取，B 续跑不覆盖 A；线上新增实体与迁移票据共用 ID 序列，防止 A/B ID 冲突。
- 零数据/零迁移票据账号的受审证据修订、CAS 和 pending 恢复、审计 revision；仅更新改变账号的 cutoff 与源变化 blocker。
- 部分上线后的受控回滚合成：A 最新 SQL/NAS 导出，B 冻结会话输入，精确分享归属和覆盖，前后输入 digest 校验。未执行旧镜像 verifier 时始终 rollback_ready=false。
- 专用无 DELETE 的分享 NOT NULL 收口 SQL；只有全部账号发布、分享定位完整后执行。

## 检查与证据

| 检查 | 结果 | 本机日志 |
| --- | --- | --- |
| 全会话子系统及相关 CLI 单元回归 | 1491 passed，54.98s | `/tmp/conversation-tenant-accepted.log` |
| 最终回滚/共享 IO/状态/重审/CLI 补充回归 | 49 passed，0.59s（与全套重叠，不相加） | `/tmp/conversation-tenant-last-regression.log` |
| 真实 MySQL：坏配置 B→A 发布并在线写入→修复并重审 B→B 补迁 | 1 passed，87.86s | `/tmp/conversation-tenant-retry-mysql-3.log` |
| 最终真实 MySQL：A 回滚 capture/export、B 冻结合成、分享保留、A 在线写入、B 补迁、多实例准入、最终 DDL | 1 passed，111.95s | `/tmp/tenant-rollback-mysql-verified.log` |
| 最终源码 hooks | AST、mypy、Black、flake8、pylint、私钥/空白检查通过 | `/tmp/conversation-tenant-hooks-5.log`、`/tmp/conversation-tenant-hooks-final.log`、`/tmp/conversation-tenant-hooks-last.log` |
| 无 tracing 替身的实际 CLI help | 失败：本地缺少 `trace_sdk` | `/tmp/conversation-tenant-real-cli-help.log` |

全套后增加的永久分享缺失回归包含于最终 49 项定向回归；测试不代表未验证分支无缺陷。hooks 的中间轮会自动补 trailing comma/格式，最终相应文件已复跑通过。测试使用仓库 tracing fake；真实数据库仍为实际 MySQL，不能把该结果当作执行镜像依赖验收。

## 独立核对与修复发现

子代理分别核对 discovery/retry、journal/orchestration、runtime/admission/rollback，并交叉复审。审查发现后修复并新增回归：

1. retention ticket 有独立归属结构，不能按通用 scope 解析；分享票据通过原始 token 的唯一可信服务映射归属。未知票据仍拒绝。
2. 回滚同名账号跨服务的分享误归属、未知 SQL service 被静默遗漏，改为精确 service+tenant+token 核验。
3. 已发布租户的原永久分享缺失，改为合并前阻断；新增 runtime token 仍可导出。
4. os.walk 默认忽略 scandir EIO/EACCES，补充明确 onerror；严格配置枚举不再使用可隐吞权限错误的 glob，且在读取前拒绝软链接目录/文件。
5. 两处单测 mock 未区分 orphan 查询与 unknown-row 查询，在无序表迭代时偶发断言失败；改为按实际 SQL 分支建模，并分别验证两类拒绝。

提交后独立复核确认最终修复均在 `aedfa7bb8`，未混入用户已有旧 runbook 修改和三份 Nov 11 报告。

## 失败历史与清理边界

中间 retry 集成曾在票据归属证明处安全拒绝，修复后第三轮通过。一次 ownership 集成在最终 check_schema 的索引读取遇到 30 秒超时，teardown 又出现 MySQL 2013 握手断连；这轮不是通过，日志保留在 `/tmp/tenant-rollback-mysql-ownership.log`。没有把共享故障降级为租户失败或吞掉错误。

最后一轮记录了精确父/子测试库身份，runner 退出 0 后只对该已知集合核验，剩余 0；`/tmp/tenant-rollback-latest-schema-identities.json` 为 0600 私有记录，无凭据。早先断连轮未保存子库名，不能精确证明其子库已清理；没有为清理而枚举其它 schema，也不猜测删除。pytest 另有既存只读临时目录垃圾清理警告，未清理不属于本轮的目录。

## 上线前仍须完成

以 [租户操作增补](../plans/2026-11-13-conversation-tenant-migration-operations.md) 为操作入口，并保留原严格模式手册。现场须安装真实 trace_sdk、让无替身 CLI help 全部通过，核对服务身份/密钥/挂载、备份与旧 writer 停止、冻结输入与在线根分离、真实 NAS 的可靠性及容量。万级性能和真实 NAS 未验收。

固定旧镜像逐服务根的读取并继续写入验收尚未执行；CLI 回滚内容仅可审查，不能直接以 passed/content_verified 当作 rollback_ready。已有迁移业务行/票据的输入修正不支持直接 retry-review；verify/finalize 失败先 resume。不得清控制表、手改 published 状态或让失败账号回退旧文件。
