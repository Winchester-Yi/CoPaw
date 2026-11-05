# B1a locator 与引用校验批量查询实现记录

日期：2026-11-05。基线：`19b77d542`。工作区：`.worktrees/conversation-mysql-nas`。

## 实现范围

按[详细设计](../specs/2026-11-05-conversation-reference-batching-design.md)与[实施计划](../plans/2026-11-05-conversation-reference-batching-plan.md)，将原事务内逐 ID 点查改为固定模板的有界集合查询：

| 调用方 | 变化 | 保留的领域校验 |
| --- | --- | --- |
| Projection 捕获 | Message、Turn 分别批查后，逐原 ref 校验 | Session、deleted、publication/offset/length、Turn 关联；相同 ID 的冲突 ref 不能被去重覆盖 |
| Source 捕获 | 批查唯一 source ID，按原输入逐项校验 | 重复 ID 拒绝、typed identity、可见性、Session 原顺序；locator 与 pin 同事务，NAS 仍在事务外 |
| 提交复核 | Source/Projection Message 分批 FOR SHARE；Turn 保留普通读 | SOURCE_FIELDS、原 projection 字段（含 in_context）和 Turn 六列逐项比较；缺项失败 |
| Plan 归属 | source/prune/share proof 汇总后批查 Message 与 publication | 原完整 scope、Session、缺项与 proof 关联检查；各发布阶段重新查询，无跨阶段缓存 |

新增 `reference_queries.py`，只允许 enum 选择五种模板，不能传入任意表名、列或锁。Message 返回现有比较字段并集，Turn 和 publication 只取所需窄列，避免批量读回 plan/执行身份文本。

单批最多 256 个唯一 uint64 ID；实际批大小还受同一物理连接的 packet 限制和 64 KiB 请求预算约束。预算扣除固定 SQL、service 最坏转义长度、协议余量和每个 uint64 最多 20 位加分隔符。容纳不了一项就在查询前抛 StorageUnavailable。空集不获取租约、不发 SQL；重复、额外 ID 或错误 service 的返回行不能静默接受。

`DatabaseSession.max_allowed_packet` 是只读能力，复用连接初始化已验证的上限，不新增 SQL。helper 顺序 await、复用当前 task 的租约，不开启或提交事务，不做 NAS IO，不持有跨阶段结果。

## 成本与兼容边界

替身计数证明：预算足够时 4,097 个唯一 Message locator 查询从 4,097 次变为 17 次；257 个 Message 的最终复核为两次 FOR SHARE；同 publication 的多个 Message 归属检查只查一次 publication。测试替身按每批 IDs 和真实窄列返回，不能用“返回全部行”掩盖漏查。

这是查询命令数量证据，不能换算为生产延迟或吞吐倍数。完整 snapshot 的 O(N) 行数与总内存、事务总锁仍存在。没有更改 schema、索引、持久化格式、HTTP 接口、publication 状态机、reader pin 上限/生命周期或未知提交语义。

## 验证

实施前数据库及存储单元基线：822 passed。新增 packet 属性用例先因属性不存在失败；五项领域成本用例先因旧逐条 fetch_one 失败，再在实现后通过。

覆盖 helper 的 0/1/255/256/257/4095/4096/4097/8193/16385、uint64 边界、乱序去重、窄列、非法输入、错误 service/额外/重复行、小 packet 减批及拒绝、首/末批失败与取消。领域测试覆盖所有原比较字段、缺项、多 occurrence、重复冲突、源顺序、完整 scope 和再次检查时的变化；既有长历史与 NAS 取消 drain/pins 清理回归保留。

真实 DatabaseConnection 配合驱动替身验证：多批查询复用已开启事务，不额外获取物理租约，不新增 BEGIN/COMMIT；packet 读取不发 SQL，并拒绝跨 task 或失效租约访问。

针对性验证：137 passed、2 skipped。扩大回归 **987 passed、2 skipped**：数据库、完整会话存储单元测试、Tracing 两组路由，以及新增真实 MySQL 模块。本包 pre-commit、`git diff --check`、相关文档链接和占位项检查通过。

新增显式专用库 opt-in 的 MySQL 测试，覆盖五模板、窄列、257 行跨批、同租约、scope/缺项、EXPLAIN 记录，以及最后一批 FOR SHARE 阻塞 writer、提交后变化被拒绝。未配置专用库，两项 skipped，fixture 未执行；这不是 MySQL 锁行为/packet/执行计划验收通过，也未执行现场 NAS 性能测量。

## 剩余内容

B1b（全量 Chat/History 一致快照、其他无界 IN/UPDATE、publication 联合锁）、B2（pin/dependency/Message 批量 INSERT）、同 bundle 读取、NAS 背压、有界缓存和写入快照准备仍待处理。B1a 不能宣称所有逐条 SQL 或无界集合查询已经消除。

本轮已按用户要求提交前包 A：`19b77d542`。B1a 已按用户后续要求提交；真实 MySQL 验收状态仍为未执行。

扩大回归命令：

```bash
../../venv/bin/python -m pytest \
  tests/unit/database \
  tests/unit/app/conversation_storage \
  tests/integrated/conversation_storage/test_reference_queries_mysql.py \
  tests/unit/routers/test_tracing_chat_history_parity.py \
  tests/unit/routers/test_tracing_chats_api.py -q
```
