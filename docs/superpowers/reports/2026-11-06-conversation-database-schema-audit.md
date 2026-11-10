# 会话迁移：当前数据库结构只读核对

日期：2026-11-06。工作树：`.worktrees/conversation-mysql-nas`，源码基准 `49a4222c8`。

## 核对范围与结论

使用用户先前提供的数据库连接，先枚举会话相关表，再核对 `copaw_dev` 的 information_schema 列、索引、引擎信息，并在独立只读事务中确认数据是否存在。服务端 MySQL 8.0.46。未读取业务正文、分享 token 或配置密钥；未执行 DDL、写入/删除业务记录或创建测试数据库。

**实际库仍为缺 service-unit 维度的早期结构，无法直接通过当前 worktree 的 schema readiness。除此之外，为实现历史内容原位引用，还需要调整内容定位模型。两者是不同问题。**

结构和存在性仅反映本次读取时点，不代表已经建立迁移停写边界。

## 实际结构与代码契约差异

| 对象 | 实际库 | 当前 worktree 契约 |
| --- | --- | --- |
| 六张 swe_conversation 业务表 | 全部存在，均缺 service_unit_key | 六表均要求服务单元字段及对应隔离约束 |
| Session 身份/租约/查找索引 | 未包含 service_unit_key | 对应索引需要服务单元前缀 |
| Chat 外部身份和最近列表索引 | 未包含 service_unit_key | 需要服务单元前缀 |
| Turn runtime key | 目前全库唯一 | 当前代码按 service_unit_key + runtime key 唯一 |
| Publication operation/path | 目前分别全库唯一 | 当前代码按 service_unit_key 分区唯一 |
| Publication pending/GC 索引 | 未包含 service_unit_key | 当前代码要求服务维度 |
| Retention 成员、guard、scope 索引 | 未包含 service_unit_key | 当前代码要求服务维度 |
| swe_chat_shares | 原 8 列；无 service_unit_key/publication_id/byte_offset/byte_length | 当前 bundle 方案要求扩展，最终 locator 方案还会进一步调整 |
| swe_conversation_migration_jobs | 不存在 | 预建迁移控制表 |
| swe_conversation_migration_entries | 不存在 | 预建迁移控制表 |

按 `schema_contract.INDEXES` 对比，六表共有 14 个同名索引的列序列与当前代码不一致；非 service-unit 的现有列，其类型和 NULL 属性未发现差异。该比较不是完整 schema readiness：默认值、字符集约束、DDL 并发变化等仍需正式预检。

六张业务表均为 InnoDB / Dynamic，表默认排序规则 utf8mb4_0900_ai_ci。精确领域身份由 VARBINARY 字段承担，不能因表级 collation 一概改写原分享 token 的比较语义。

## 数据存在性核对

| 表 | 本次只读事务结果 |
| --- | --- |
| swe_conversation_sessions | 无记录 |
| swe_conversation_chats | 无记录 |
| swe_conversation_turns | 无记录 |
| swe_conversation_messages | 无记录 |
| swe_conversation_publications | 无记录 |
| swe_conversation_retentions | 无记录 |
| swe_chat_shares | 3 条记录 |

六表使用 EXISTS 验证，未将 information_schema 的 TABLE_ROWS 估算当作空表证明。分享只查询 COUNT，未读取其业务字段值。

当前状态适合在正式上线前一次性对齐最终结构，避免先升级到旧 bundle-only 方案、随后再升级定位层。但本轮不执行建表、改表或重建；DBA 执行前仍需停写重验。现有三条分享必须保留 token、旧字段和内容语义，不能按“新会话表为空”清空分享。

## 建议处理

1. 冻结最终内容定位设计，再生成一套完整预建/升级 SQL；不让用户先执行现有脚本后再返工。
2. 保留 Session / Chat / Turn / Message occurrence 及原分享的领域身份；补 service-unit 字段和精确索引。
3. 内容层新增物理对象、组合视图、视图成员三张支撑表；普通消息直接 object + extent，避免每消息另建 ref 和 part。
4. Publication 回归提交操作；纯 SQL 元数据变化不强制拥有 NAS bundle。详细[结构调整方案](../specs/2026-11-06-conversation-content-schema-design.md)。
5. 新增表、locator 列和 schema contract 必须与 reader、导入器、GC、verify/finalize 同步升级；只建表不能使当前程序自动支持旧文件。

## 证据位置

不含凭据的结构快照位于本机 `/var/folders/95/98fn9l095ys34mjpz070j0400000gn/T/swe-conversation-schema-readonly-20261106-a9seuegi/schema-metadata.json`，权限 0600，含列/索引清单及对照结果，无业务行内容。

服务器还存在其他名称带 test 的 schema；本轮未读取其中业务记录，也未判断归属或清理。此前真实验收的通过/失败结果不因本次结构查询而改变，本轮没有运行迁移验收。
