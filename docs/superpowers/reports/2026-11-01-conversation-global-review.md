# 会话 MySQL / NAS 迁移全局独立审计

审计日期：2026-11-01，Asia/Shanghai。审计基线：`858fc4d65`，运行时代码包括
`74057c17b`，最近测试增量为 `aa846b947`。本次使用三个子代理，分别审查核心存储协议、
离线迁移与恢复、在线功能接入；父代理交叉阅读源码、核对原始证据并独立运行复现。
本次只新增审计文档，未修复运行时代码，未连接业务数据库或执行迁移、部署、提交。

## 结论

**不能认定会话数据及关联功能已正确且完整实现迁移，也不能据此直接生产切换。**

主链路和大多数关联入口已实现；核心协议未发现新的确认缺陷。但本轮确认了一个 P1
恢复完整性缺口和两个 P2 问题，见下文。它们独立于报告已经披露的目标环境验收缺口。
此前报告的“未发现确认 blocker”是此前审查结果，不能继续作为当前无问题的依据。

这里的迁移目标是 **MySQL 元数据、索引、执行权和正文引用 + 原 NAS 不可变正文**，
并非将全部聊天正文存入数据库；该分工符合已确认设计。Cron 定义/执行库、独立 Tracing
数据等外部领域不因本次会话迁移而整体搬迁。

## 已确认问题（按优先级）

### R1 / P1：单 scope 备份无法组成同一数据库的完整恢复

- `src/swe/app/conversation_storage/backup.py:756` 的 `_snapshot` 只捕获一个 workspace
  scope，并附带该 tenant 的 `legacy_share` 集合。
- 同文件 `1111` 行对七张表执行不带 scope 的全空检查；`1123` 行在事务中再次检查全表为空；
  `1160` 行又将全表内容与单 scope 快照比较。
- 因此，第一个 scope 恢复成功后，第二个 scope 即被
  `ConflictError: restore requires an empty metadata target` 拒绝。即使用独立空 NAS
  恢复第二个 scope 也失败。全仓未找到合并所有 scope 快照并恢复同一 schema 的实现。
- 这是全 scope 恢复能力缺失，不表示当前在线写入会直接丢数据。每 scope 恢复到不同
  测试库只能证明单 scope 可恢复，不能满足共享业务库内所有 scope 的完整恢复目标。
- 现有本地两个 scope 分别 backup/export 的证据也没有验证这一点。

复现：`/private/tmp/conversation_migration_audit_repro.py` 使用临时构造、可被
`load_backup` 接受的备份、临时 NAS 和内存数据库替身；只跳过依赖环境配置的 target
admission，不连接真实数据库。父代理独立运行得到：

```text
RESTORE_SCOPE_A True True
RESTORE_SCOPE_B ConflictError restore requires an empty metadata target
```

这是模块级控制流复现，不是两 scope 的真实 MySQL 全栈验收。源码中的三处全表约束和缺少
聚合入口共同支持结论。

修复与验收方向：一次捕获/恢复全部 scope，或先合并各 scope 的完整快照、对 tenant 级
`legacy_share` 去重并校验 ID/引用，再原子恢复至空目标。不能只删除空表检查。必须增加
同 tenant 多 workspace、跨 tenant、孤立分享和未知恢复模块的共同 schema 往返测试。

### R2 / P2：长期 Session 的恢复/发布路径存在 4096 个 publication 上限

- `src/swe/app/conversation_storage/projection_publication.py:95` 一次 pin 全部 current
  memory/task/error publication；请求内缓存未命中时执行该分支。
- `src/swe/app/conversation_storage/projection_references.py:180` 同样一次 pin 完整
  recovery projection 的 message/cold-Turn 引用集合；`256` 行的 hydration 使用它。
- `src/swe/app/conversation_storage/retention.py:27` 限制单批最多 4096 个去重 ID。
  4097 个不同 publication 引用会抛出
  `ValueError: publication pins require bounded positive integer IDs`，阻断相应读恢复或新发布。
- 数量指不同 publication，不是简单的消息条数。Cron task_messages 持续追加
  （`src/swe/app/crons/executor.py:989`），普通 memory compaction 不裁剪这部分历史；
  因而该规模可以随长期使用到达。请求缓存不能在新请求/重启后消除限制。

复现：`/private/tmp/conversation-global-audit-pin-limit.py` 使用真实 RetentionStore 和
只读数据库替身，父代理独立运行，两入口均确认在 4097 个 ID 时以预期 ValueError 失败。
没有进行 4097 条真实 SQL/NAS 历史的全栈构造；可达性另由调用链与无上限追加逻辑支持。

修复与验收方向：两处统一按 MAX_PIN_BATCH 分批 pin，保留同一快照/校验语义，并确保异常、
取消及 NAS IO 排空后释放所有批次。增加 4096/4097 边界、新请求、重启、Cron 累积历史测试。
不能靠删历史或放大单批限制解决。

### R3 / P2：有效一致性备份的 CLI 导出仍依赖旧冻结源

- `src/swe/cli/conversation_storage_cmd.py:263` 对所有操作先执行原源 inventory，
  `269` 行校验通过后，`270` 行才进入 export 分支。
- 当旧源挂载不可访问或路径变化时，即使一致性备份完整，`export --apply` 仍先报 ENOENT。
  直接调用 ConversationExporter 对同一备份成功。
- 手册要求保留旧源；此问题不代表在手册假定的正常挂载状态下一定失败。但它使实际回退
  CLI 多出一项并非备份内容所需的可用性依赖，不能把“有效备份可自足导出”视为已经成立。

上述 migration 复现脚本的父代理独立结果：

```text
DIRECT_EXPORT_VALID_BACKUP True
CLI_EXPORT_VALID_BACKUP 1 Error: [Errno 2] .../unmounted-source
```

修复与验收方向：export 分支校验备份、旧目标格式和空导出目录，不以重扫原始迁移源作为
前置条件；增加原冻结源不可访问而 backup 完整时的 CLI 导出测试。

## 功能覆盖判断

| 范围 | 本轮判断 | 关键限制 |
| --- | --- | --- |
| 身份、六表及 share locator、workspace/NAS 装配 | 实现已存在，未发现新的确认问题 | 目标实例 schema/capabilities/权限尚未验收 |
| Chat CRUD、专家绑定 CAS、多 Chat/Session、分页与 metadata | 已接入 SQL 权威，未发现额外在线文件旁路 | 结论限于所审调用链和现有测试 |
| current/history/archive/answer_turn/context/目标用户 Tracing | 查询与 NAS locator 路径已接入 | 长期 Session 的 projection 路径受 R2 影响 |
| 准入、generation/lease、Stop、终态和 unknown 结算 | 核心协议与设计一致，未发现新增确认缺陷 | owner death/IO drain 依赖真实证据 |
| compact/reset、语义发布和完整恢复模块 | 已实现，保留未知模块和 receipts | 引用规模上限仍需修复 |
| 永久分享、孤立分享、计数、selection proof | 已实现 SQL/NAS 路径 | 全 scope 恢复中的 tenant 级共享集合需聚合处理 |
| Console/CLI/Cron/W+ 接入 | 未发现额外确认旁路 | Gateway Session 亲和仍是外部部署契约 |
| inventory/import/verify/七表安装 | 已实现，具备阻断和幂等机制 | 当前真实历史只完成子集往返 |
| 一致备份/恢复/反向导出 | 单 scope 能力存在，整体闭环不完整 | R1、R3；固定旧镜像未验收 |
| GC/文件治理/旧 NAS-only 备份边界 | 已实现保护和 report-only 默认行为 | 物理回收应保持未启用直至相应验收 |

跨 Pod Stop 仅使用本地执行上下文，没有应用内远端取消代理。但设计文档
`docs/superpowers/specs/2026-10-29-db-nas-conversation-storage-design.md:210`
明确要求 Gateway 为 reconnect/Stop/approval 提供 Session 亲和，因此不列为新增代码缺陷。
目标亲和失败时，非 owner Pod 的 Stop 可能返回 idle；这必须进入部署验收。
SQL list 保留持久状态；current reconnect 对无本地 coordinator 的活动状态返回 unavailable，
不会据此伪造 terminal。

## 提交与证据核验

已将报告与提交链交叉核对：身份/bundle、publication、pins、Chat/history、执行与归档、
导入/verify、工厂接入、GC、备份、导出、安装，以及后续
`58ee8c62b`、`63e73f2c3`、`caed335f5`、`4228d9167`、`0be85c33f`、
`3176050f5`、`74057c17b` 等修复/专项均有实际提交。
最后 `858fc4d65` 为报告证据更新。本轮发现项没有修复提交。

父代理读取原始 JUnit，确认报告计数：

| 原始证据 | 核读结果 | 性质 |
| --- | --- | --- |
| conversation-current-74057c17b-full-mysql.xml | 275 tests，274 passed，1 skipped，0 failures/errors | 此前运行，不是本轮重跑 |
| conversation-cutover-final-three-files.xml | 74 passed，0 failures/errors/skips | 此前测试 delta |
| conversation-current-74057c17b-plan-unit.xml | 614 passed | 此前运行 |
| conversation-final-head-cross-regression.xml | 1238 passed | 此前运行 |
| conversation-answer-turn-green-fresh.xml | 3 passed | 此前专项 |

上述 XML 均位于 `/private/tmp/`。原始增量报告的 `passed=true`、
`generated_schemas_removed=true` 和十阶段 ledger 的 `status=passed`、98.1515 秒
也已核读；它们不覆盖 R1/R2/R3。

父代理本轮新鲜执行：

```bash
../../venv/bin/python -m pytest tests/unit/app/conversation_storage \
  tests/unit/cli/test_conversation_storage_cmd.py \
  tests/unit/scripts/test_rehearse_conversation_storage.py -q \
  --junitxml=/private/tmp/conversation-global-audit-unit.xml
```

结果：**514 passed，1.70 秒**。两个复现脚本分别执行完毕，退出 0，输出上述确认结果。
测试全绿说明现有测试没有捕获这些场景，不消除已复现问题。
子代理另跑的 101 项有重叠，不与 514 相加；功能接入子代理另有 55 passed，XML 为
`/private/tmp/conversation-global-audit-feature-units.xml`。
本轮没有重跑约 49 分钟的 MySQL 全目录，也没有重跑 Console 或全仓静态检查。

## 尚未关闭的验收门槛

1. 全量历史：原扫描有 6 个缺源 Session、107 个缺可信映射 Session 文件，仍然 ready=false；
   成功范围仅 40 Session / 55 Chat 子集。必须补齐真实源和映射并完成全部 scope 的核验。
2. 目标 MySQL/TDSQL：capabilities、权限、MDL、原子七表切换、EXPLAIN 和目标配置需独立验证。
3. 原 NAS 跨不同节点的双 Pod durability、竞争、迟到 writer、Pod death 和 IO 排空；
   本机双进程无法替代。
4. 固定旧生产镜像：反向导出后读取和继续写，含新增数据、Stop、compact、删除、绑定、
   分享计数和未知模块。当前 `rollback_ready=false`。
5. Gateway Session 亲和、目标入口/队列/全部 writer 排空和真实部署的完整冒烟。
6. 全 scope、完整规模的共同恢复与连续停机演练；RSS、NAS bytes/files、锁等待和 p95/p99
   仍缺实测。全仓 pre-commit 此前未通过，也不能称为全绿。

建议先修复并回归 R1/R2/R3，再完成上述环境和数据门槛。当前最准确的交付状态是：
**主体实现及单 scope 本地验证已交付，整体恢复与长期运行边界仍有实现缺口，生产验收未完成。**
