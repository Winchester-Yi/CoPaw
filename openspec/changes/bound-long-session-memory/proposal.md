## Why

第一阶段已经解除已完成 Task/Context/Session 的引用持有链，但长会话仍会在每次请求全量解析历史 `turn_states` 正文；checkpoint 追加也会读写全部事件文件。历史长度因此仍放大单请求内存、锁持有时间和 NAS I/O。

## What Changes

- 继续使用 NAS 文件持久化和 POSIX flock，不引入 Redis、MySQL 会话表或 SQLite。
- 新增版本化的 session terminal-turn ledger：只迁移已持久结算的 turn，session 保留全部未结算记录和有界近期终态窗口；历史正文和状态仍可查询，不做数据丢弃。
- session JSON 作为提交根，先持久化可按 turn 定位的不可变冷版本，再原子发布新 revision；保留旧快照读取、取消和结算恢复语义。
- checkpoint 事件按 epoch/sequence 分段追加，锁内分配序号，增加流式分页读取；保留 checkpoint、compaction boundary、UI archive cursor 和证据引用语义。
- 普通执行、定点 turn 查询和分享选择改用按需读取；旧完整历史 API 保持返回语义，明确其输出规模仍影响内存。
- 增加迁移预览、故障恢复验证和合成负载验收。新格式仅在所有写入方升级后灰度启用；运行旧二进制前必须导出回旧格式。

## Capabilities

### New Capabilities

- `session-terminal-turn-ledger`: 有界热 turn 状态、可查询冷终态记录、跨文件提交、迁移与回退。
- `segmented-checkpoint-journal`: epoch 隔离、锁内连续序号、持久追加、按水位流式读取和损坏处理。

### Modified Capabilities

无外部 API 需求变更。现有 history、answer-turn、recovery、sharing 契约继续有效；不改变已发布分页接口的含义。

## Impact

- `src/swe/app/runner/session.py`、`session_lifecycle.py`、`runner.py`、`api.py`，Console recovery 与 chat sharing。
- `src/swe/agents/memory/conversation_archive.py` 及 checkpoint 消费方；新增小型文件持久化 helper，避免让通用 session 模块承担历史展示逻辑。
- NAS 上增加 session 所属的 ledger 目录及 checkpoint journal v2；所有路径继续由租户/source/workspace/user/session 或 Chat 身份解析，客户端不提供文件路径。
- 本变更仅方案设计。详细设计位于 `docs/superpowers/specs/2026-10-15-memory-phase2-design.md`；实施任务未开始，不改动生产运行时代码。
