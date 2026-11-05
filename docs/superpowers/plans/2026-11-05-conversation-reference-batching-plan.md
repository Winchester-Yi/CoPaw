# B1a 引用批量查询实施计划

> **For agentic workers:** 使用 superpowers:executing-plans，在现有 conversation-mysql-nas worktree 内逐项执行。按当前用户授权直接实施，inline 执行，不委派子代理。

**Goal:** 将原事务内的 locator、引用复核和 plan 归属点查变为有界批查，同时保留完整校验、错误和资源生命周期。

**Architecture:** 内部固定五种窄列模板，由共享 helper 校验 ID、按物理连接 packet 与 256 IDs 双重上限顺序切批。调用方仍按原 refs/messages 检查，避免 SQL 返回顺序和去重掩盖领域冲突。保留 Message FOR SHARE、Turn 普通读和所有外层事务。

**Tech Stack:** Python、现有 DatabaseConnection/DatabaseSession、MySQL、pytest。

来源：[已确认的 B1a 设计](../specs/2026-11-05-conversation-reference-batching-design.md)。前包提交 `19b77d542`。本包不实施 B1b 一致快照/联合 publication 锁、成员 INSERT 或 NAS 读取改造。

## 1. 数据库能力和 helper

文件：`src/swe/database/session.py`、新增 `src/swe/app/conversation_storage/reference_queries.py`；测试放在 `tests/unit/database/test_connection.py`、新增 `tests/unit/app/conversation_storage/test_reference_queries.py`。

- [x] 先在真实 DatabaseConnection 配合驱动替身测试读取 packet 不发 SQL、嵌套租约相同、跨 task 和失效租约拒绝。现代码访问属性应失败。
- [x] 加只读属性，复用现有能力，不重新查 server：

```python
@property
def max_allowed_packet(self):
    self._check()
    return self._entry.max_allowed_packet
```

- [x] helper 用 `ReferenceQuery` enum 选择 Message locator/current locator/ownership、Turn、publication ownership。仅拼接固定模板和占位符；显式列包括 SOURCE_FIELDS、projection 比较列，禁止 SELECT *。
- [x] 实现接口：

```python
async def fetch_reference_rows(database, query_kind, ids, service_unit_key):
    """Return id-to-row using bounded fixed SQL on a task-owned lease."""
```

输入 ID 先验证 `type(identity) is int and 0 < identity < 2**64`，再去重排序。service 必须是 1..256 字节的 bytes。空集不 acquire、不执行 SQL；无效模板不能变为任意 SQL。

在 `async with database.acquire() as session` 读取 packet，预算 `min(64 * 1024, max(0, min(packet - 1024, packet * 9 // 10)))`。固定 SQL 长度、service 最坏转义长度及协议余量先扣除；每 ID 预留 21 字节，取与 256 的较小值。容纳不了一项时 SQL 前抛 StorageUnavailable。

每批 `WHERE service_unit_key=%s AND id IN (...) ORDER BY id`，Message current 模板末尾 `FOR SHARE`。重复/额外 ID、错 service 的结果抛 IntegrityError；缺项交给领域。顺序 await，不建立事务或跨阶段缓存。

- [x] helper 测试覆盖空集、乱序去重、0/1/255/256/257/4095/4096/4097/8193/16385、uint64 边界、非法输入、窄列、锁、每阶段一次 acquire、小 packet 减批/无法执行、结果异常、缺项及取消。结果替身按本批 ID 和窄列返回，不无限返回所有行。

## 2. Projection 与 Source 调用方

文件：`projection_references.py`、`source_messages.py`；测试：新增 `test_reference_batching.py`、既有 `test_projection_scale.py`、`test_source_messages.py`。

- [x] 先写 `_snapshot_rows` 的成本回归：4097 refs 只允许集合查询，原点查应失败；相同 ID 冲突 ref 仍拒绝。
- [x] `_snapshot_rows` 先批查唯一 Message/Turn，再逐原 ref 验证 locator/Session/Turn 关联。same-publication block ref 无 ID，不新增查询。
- [x] `snapshot_sources` 保留重复 ID 拒绝；一次批查后按原 messages 调 `_validate_row`，原顺序检查和 pin/NAS 时点保持。
- [x] 两个 `revalidate` 批查 Message current 行，再按原字典逐字段对比；Turn 用普通读模板。空 snapshot 零 SQL。提取原比较字段为常量，仅用于避免列遗漏，不改变集合。
- [x] Source 原字段逐项变化及 Projection 的 `in_context`、Turn 原六列变化逐项拒绝；中/末批缺项仍失败。核对查询次数按批次数增长，重复 ref 不丢冲突，多 occurrence 不合并。
- [x] 更新 locator 测试替身支持 acquire/packet 和精确 IN 子集；已有长历史 pin/取消/失败清理回归保留，不改生产逻辑适配旧替身。

核心调用形式：

```python
rows = await fetch_reference_rows(
    service.db, ReferenceQuery.MESSAGE_LOCATOR,
    [ref["message_id"] for ref in refs if "message_id" in ref],
    request.scope.sql_values[0],
)
```

复核从原 snapshot 的 service 字段取得 scope，仍检查每个原 expected 行的全部字段，不只遍历返回行。

## 3. Plan 引用归属

文件：`publications.py`；测试：`test_reference_batching.py`、`test_publications.py`、`test_publications_service_isolation.py`。

- [x] 成本回归先覆盖 source/prune/share proof 合并集合、同 publication 多 Message、缺项和外部 scope。
- [x] `_validate_plan_reference_ownership` 以原 Message 集合调用 MESSAGE_OWNERSHIP，保留原 Session 校验；关联 publication 集合调用 PUBLICATION_OWNERSHIP，保留完整 WorkspaceScope 和 Session 检查。
- [x] 保留 reserve、write/confirm、commit 的重新检查和原错误；不缓存已验证引用，不改 operation/association 点查和锁。
- [x] PublicationDatabase 新增准确 fetch_all 分支；foreign-service 故障仍能被主动注入并拒绝，不只让替身过滤掉错误行。

## 4. 验证与记录

- [x] 运行 `../../venv/bin/python -m pytest tests/unit/database tests/unit/app/conversation_storage -q`，记录实际结果与设计差异。
- [x] 新增显式专用库 opt-in 的集成测试模块，fixture 执行前检查环境是否配置；不读取业务配置/dev.json。覆盖真实模板/事务复用/FOR SHARE 与 writer、窄列及缺项。缺库 skipped，明确不是验收通过。
- [x] 对本包实际文件运行 pre-commit、git diff --check；补实现报告与入口索引，记录替身成本和真实环境未验收项目。
- [x] 自审 scope、字段、引用原顺序、取消清理、同 task 顺序执行；不得扩大为 B1b/B2。用户已进一步要求提交 B1a，本包仅暂存相关文件，不包含既有无关改动。

## 执行状态

B1a 已实现；真实 MySQL 用例已新增但因专用库未配置跳过，实际证据和限制见[实现记录](../reports/2026-11-05-conversation-reference-batching-implementation.md)。本包已按用户后续要求提交。
