## Context

用户要求第二阶段先设计、暂不使用 Redis，并确认所有旧写入方退出后才灰度启用新格式。第一阶段已交付 Task/Context 生命周期与临时缓存预算；本变更聚焦长会话历史和 checkpoint 追加成本。

完整技术设计、源码证据、参数、恢复与验收协议见 [第二阶段设计稿](../../../docs/superpowers/specs/2026-10-15-memory-phase2-design.md)。该文件是技术细节的唯一维护入口；此文件概括关键决定，不复制两份协议。

## Goals / Non-Goals

**Goals:**

- 普通执行不解析全部冷终态 turn；保留权威状态、正文、顺序、重试与历史查询。
- checkpoint 单次追加不再全量读写历史日志，按 epoch/sequence 和水位读取。
- 在实际 NAS 上保持持久提交、取消、崩溃恢复与隔离契约，支持迁移和离线导出回退。

**Non-Goals:**

- Redis、数据库会话迁移、SSE/媒体预算、全历史 API 协议变更、所有进程内存固定上限。
- 自动丢弃历史、journal已提交段GC、manifest/evidence索引全面重构。

## Decisions

1. 先实施2A journal，再实施2B冷turn；分别提交、分别灰度。
2. journal使用有界段和单原子提交根，锁内分配序号，已提交数据严格校验。checkpoint/epoch/manifest的版本引用同根发布，不依赖异常rollback覆盖进程死亡。
3. session继续作为业务提交根；冷turn用确定路径和不可变revision文件，pending intent约束恢复，避免自建B-tree。所有未结算状态受保护，近期终态窗口64条且256KiB。
4. 内部读取分执行快照、定点查询和历史投影，不在通用getter中自动hydration全部历史。完整外部历史返回语义保持，按输出规模增长的成本明确保留。
5. 数据不按TTL删除；不删除已提交journal段或旧generation。新格式开关只控制新转换，不能把已迁移对象切回旧真源。

## Risks / Trade-offs

- NAS小文件增加 → 合成负载验证inode/元数据成本；不能用每次全历史扫描换取低内存。
- 多文件与取消结果不确定 → intent或原子根作为提交判据，SIGKILL及two-Pod故障注入。
- 旧writer无法理解新格式 → 全量升级排空后启用；回退旧二进制前停写并导出当前数据。
- Agent历史和完整响应仍可能很大 → 验收固定活动Agent输入，单列输出与其他历史字段成本，不宣称全进程内存有界。

## Migration Plan

按完整设计第6节：只读预览 → 校验NAS锁/rename/fsync → 全部writer升级排空 → 每对象持锁迁移 → 单租户灰度 → 观测并扩大；回退导出须包含启用后的新写入。

## Open Questions

没有必须由用户再次选择的架构问题。段大小、热窗口为明确初始值，实施时通过既定基准校验；生产StorageClass能力与性能为上线门禁，尚未验证。本轮未开始实施。
