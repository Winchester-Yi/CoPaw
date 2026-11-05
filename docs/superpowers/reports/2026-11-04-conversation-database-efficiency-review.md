# 会话元数据 MySQL/NAS 迁移：连接、SQL 与异步效率审查

审查日期：2026-11-04。对象：`.worktrees/conversation-mysql-nas`，HEAD `099ecaeb847dd174a7f2e5fce0739775745dcdb6`。本次由连接管理、SQL/缓存、异步调用链三个子代理独立审查，主代理复核源码并运行单测。仅新增本报告，未修改实现，未访问实际数据库。

## 结论

**驱动与连接复用方向合理，已实现原生异步、连接池和事务绑定；会话存储尚未充分利用现有预编译批量能力，整体效率也不能仅凭 async 和连接池判定达标。**

最值得优先处理的是：续租预算与共享池等待预算不协调、同一工作区写事务竞争公共 guard、长会话逐引用 SQL、每次连接租约的检查往返。预编译和缓存应在这些问题被量化后有选择地补充。没有证据要求为性能全面替换驱动或引入 ORM。

源码中的一致性措施较完整：未知提交不盲重试、NAS 在主要 SQL 事务之外执行、reader pins 防止读时回收、任务独占数据库 session、失败连接隔离。优化必须保留这些约束。

## 与 Spring/MyBatis 常见能力对照

| 能力 | 当前实现 | 评价 |
|---|---|---|
| 连接池 | 自定义异步物理连接池；默认每进程 2–10 条 | 已实现复用，并非每条 SQL 建连 |
| SQL 参数化 | 常规 cursor.execute(query, params) | 已实现，值交给驱动处理；不等于服务端预编译 |
| 服务端预编译复用 | `PreparedInsert` + `prepared=True`，按物理连接缓存，LRU 上限 4 个形状 | 能力存在；会话存储未调用，主要用于 Tracing |
| 批量执行 | 通用 execute_many；预编译路径按行数、字节数、参数数量切分 | 基础能力存在，会话 pins、引用等热点仍有逐条操作 |
| 同事务连接绑定 | ContextVar + 当前 task 归属校验 | 嵌套调用复用同一连接，跨 task 使用会拒绝 |
| 一级查询结果缓存 | 未见通用 SQL+参数结果缓存 | 不具备 MyBatis SqlSession 式查询缓存 |
| 内容缓存 | 请求内 occurrence/projection cache，按 scope/session/revision 定位 | 能减少部分 NAS 内容重复读取；SQL 权威校验仍执行 |
| 跨请求缓存 | 未见会话元数据的 Redis/二级结果缓存 | 多实例一致性边界较清晰，但不能称已充分缓存 |
| 异步 | mysql.connector.aio；NAS 使用 asyncio.to_thread | SQL是真异步；NAS线程排队、取消和CPU处理仍有边界 |

MyBatis 的 SIMPLE/REUSE/BATCH executor 和查询结果缓存是不同机制；MyBatis 默认 SIMPLE 也不代表自动复用所有 PreparedStatement。JDBC PreparedStatement 是否落实为服务端预编译还与驱动配置有关，不能将“用了 MyBatis”直接等同于全部性能能力已开启。[MyBatis Java API](https://mybatis.org/mybatis-3/java-api.html)、[MyBatis 配置](https://mybatis.org/mybatis-3/configuration)。MySQL 官方将 `prepared=True` 的二进制协议执行与普通 cursor 区分，并说明相同 statement 可跳过后续 prepare。[MySQL Prepared Cursor](https://dev.mysql.com/doc/connector-python/en/connector-python-api-mysqlcursorprepared.html)。

## 已实现且值得保留的设计

- `src/swe/database/driver.py:4` 使用官方 `mysql.connector.aio.MySQLConnection`；`session.py:60` 的 execute/fetch/close 均 await。无需通过线程池包裹 SQL。官方也将该包定义为 asyncio 接口：[MySQL 异步连接文档](https://dev.mysql.com/doc/connectors/en/connector-python-asyncio.html)。
- `database/pool.py:149` 在连接创建前预留容量，满池有限等待；`connection.py:85` 和 `session.py:26` 实现租约复用及 task 所有权。连接容量不会因取消后过早补建而轻易越界。
- `connection.py:127` 将事务中的多条 SQL 绑定同一连接；提交失败或取消后隔离连接，不隐式重连重放业务。嵌套 transaction 为复用外层事务，没有独立 savepoint 语义。
- `pool.py:260` 对未确认结束的 I/O 保留槽位，close 具有有限等待；代价是异常 I/O 长期不退出时池容量降级，需要监控。
- `conversation_storage/runtime.py:158` 将多个 workspace 的适配器注入同一个 database；`_app.py:1004` 创建应用数据库对象。没有为每个租户/workspace 单独创建连接池。
- `chat_repository.py:163` 先取 SQL locator 与 pins，退出事务后读取 NAS；`publications.py:855` 在最终提交事务开始（`:891`）前完成主要 NAS 校验。避免把普通 NAS 延迟直接变成长期 SQL 持锁。
- Chat 查询通过 JOIN 投影当前状态（`chat_repository.py:45`）；Chat 游标分页有范围条件与 LIMIT（`:322`），历史归档也采用有界读取。schema 为常见身份查找、近期会话、消息顺序与回收建立了索引（`scripts/sql/conversation_metadata_tables.sql`）。

## 主要发现与建议

优先级定义：P1 为高并发上线前应验证并控制的可用性风险；P2 为已有代码证据的效率或故障隔离问题，收益需实测；P3 为有条件采用的优化。以下风险均不等于已证明生产故障。

### 1. P1：续租预算与数据库等待预算不协调

事实：执行租约默认 30 秒（`execution.py:91`）；续租循环每次 sleep 约 10 秒后 await renew（`runner_integration.py:151`）。renew 与其他业务共用数据库池，经过 guard 与 Session/Chat/Turn 加锁（`execution.py:626`）。数据库默认 acquire_timeout=30 秒，read/write_timeout=600 秒（`database/config.py:44`），单次操作还取读写预算最大值（`session.py:40`）。

影响推断：正常任务也可能因池等待或行锁等待错过剩余租约；后续 authority 校验明确拒绝过期所有权（`execution.py:596`），续租失败会取消父任务。这首先是可用性风险，并非数据损坏证据。async 只能让等待期间其他协程运行，不能保证续租及时获得连接和锁。

建议：以租约剩余时间约束续租的获取连接、锁等待及执行总 deadline，预留取消/处理余量；为续租提供可验证的优先容量或隔离资源。不能只增大池或租约。压测时覆盖后台 Tracing 饱和、同 scope 锁竞争和数据库延迟，检查 renew P99 与过期率。

### 2. P2：同一 workspace 的部分写事务存在公共 guard 串行点

事实：`retention.py:181` 的 ensure_guard 每次进行 INSERT ... ON DUPLICATE KEY UPDATE，再 SELECT ... FOR UPDATE；`lock_guard:210` 再查一次同一 guard；`assert_writes_allowed:218` 继续查 backup fence。guard 唯一键不含 user/session（DDL `:246`），嵌套 transaction 又复用外层事务，因此锁到外层提交才释放。

影响：同一 service_unit/tenant/source/agent/scope_kind 下，不同用户、不同会话的受 guard 保护写事务会竞争同一行。加连接或加 Pod 不能消除此锁竞争，可能增加持连接等待。这里不包括所有写：普通 reader pin/release 不走此 guard（`retention.py:317`、`:364`）。

建议：先统计 guard 锁等待、持锁时长和调用次数；消除同一事务重复的 guard 查询，缩短持锁 SQL。若仍为瓶颈，再设计普通写共享保护、backup/GC 排他保护。必须同时解决首次创建 upsert 的排他锁、事务隔离下 fence 可见性及锁顺序，不能直接删除 guard 或机械把 FOR UPDATE 改成 FOR SHARE。

### 3. P2：每条独立 CRUD 存在三次额外检查往返

事实：取得连接执行 ping（`pool.py:183`）；业务 SQL 后，归还连接执行 get_autocommit（`:236`），再完整查询 session baseline（`:242`、`:125`）。本机官方驱动 get_autocommit 源码确认会发送 `SELECT @@session.autocommit`。

因此，健康的单条独立 CRUD 通常约为“1 次 ping + 1 次业务 SQL + 2 次检查查询”，约四次串行命令往返，不计连接建立、事务命令和驱动额外行为。n 条 SQL 在同一个明确租约内可以分摊三次检查；不应简单将所有调用一律乘四。

建议：测部署网络 RTT 和检查耗时占比；在有明确生命周期的短业务单元内复用租约，避免跨 NAS 等待长占连接。评估合并归还检查、按闲置时长探活或限制受管连接的 session 状态修改；保留防污染和失效隔离，不能直接取消所有检查。示例：RTT=2ms 时三次检查约增加6ms，这只是模型，不是实测值。

### 4. P2：长会话恢复/提交的逐引用 SQL 会线性放大

事实：`projection_references.py:108` 对每个 message/turn 引用 fetch_one；`:52` revalidate 再逐条查询，message 使用 FOR SHARE。缓存命中内容不会跳过这些 SQL 权威校验（`:166`–`:172`）。缓存刷新还可能为新引用逐条查 locator（`:322`）。

影响：包含 N 个唯一引用的路径可能有约 N 条快照查询；进入完整 revalidate 时再增加约 N 条，另有 pin、依赖等成本。即使主键命中且每条很快，串行网络 RTT 与事务持锁时间仍随 N 增长。局部 NAS 内容缓存不能解决此类 SQL 往返。

建议：按 service/session 分组、去重、分块批量读取 locator，构造 id→row 映射完成等价校验；锁定阶段按稳定 ID 顺序批量 FOR SHARE，保留原子性。勿在同一事务连接上 asyncio.gather 多条 SQL。验收需把 SQL 次数从 O(N) 控制到 O(ceil(N/batch_size))，并验证并发删除、更新和 GC。

### 5. P2：普通读路径包含持久化 reader pin 写放大

事实：`chat_repository.py:163` 在列表/单条详情读取时创建 pins，之后逐条 NAS hydrate（`:133`），批量 revalidate 后释放。`retention.py:317` 先锁 publication，再每条 `_insert` 一个 retention，最后校验成员；finish_reader 最终更新 released（`:379`）。依赖记录也存在逐条插入（`:392`）。

影响：读并非纯 SELECT；一页有 K 个不同 publication 时，pin INSERT 数量为 O(K)。重复读还增加 retention 证据记录和数据库写负载。证据记录的保留有一致性/审计目的，不能未经生命周期定义直接清除。

建议：优先将 pin 和 dependency member 插入改成受限批量 INSERT，可复用现有 PreparedInsert；保留锁、成员集合验证和释放语义。若产品列表只需要轻元数据，可评估独立轻量列表投影，避免列表总是读取内容元数据。此项涉及接口数据需求，不能直接省略返回字段。

### 6. P3：预编译能力没有覆盖会话 SQL 热点

事实：常规 SQL 在 `session.py:60` 使用 buffered dictionary cursor，没有 prepared=True。`prepared.py:50` 才建立 prepared cursor，按 `(模块级模板对象, 批次行数)` 缓存 SQL 对象和 cursor；缓存位于物理 PoolEntry，跨租约可复用，上限4。另有限制65535参数、字节预算、max_allowed_packet 与惰性行生成。全仓检索显示业务调用点主要是 `tracing/store.py`，会话目录没有 insert_prepared 调用。

评价：参数化是正确的，但不能宣称会话 SQL 已实现服务端预编译复用。现有批量设施也不是任意 SELECT/UPDATE 的 statement cache，动态批次形状会竞争4个缓存项。

建议：先用于 pin/dependency 等固定模板批量写，再用实测决定是否为高频稳定 SELECT/UPDATE 增加有界 statement cache。保留物理连接归属、cursor 释放、失败连接丢弃及重连后失效规则。单次 prepared=True 而每次关闭 cursor 会失去复用收益；prepare/reset 的协议成本也需计入，不能保证所有小 SQL 都更快。

### 7. P2：NAS 有线程卸载，但缺少独立背压与有限停止策略

事实：`publications.py:192`、`retention.py:41` 使用 asyncio.to_thread；默认共享 executor 为32线程（`_app.py:257`）。这些入口未见独立 NAS 提交并发限制。收到取消后会 shield 等待 worker 完成；NAS 不返回时没有本层时间上限。数据库池的有限清理不能约束这种 NAS 等待。

影响推断：NAS 变慢会在线程池排队，影响其他 to_thread 工作；NAS 永久阻塞会让停止/退出长期等待。此设计防止后台线程仍写入时错误报告停止、或过早释放 pins，其安全目标应保留。

建议：给 NAS 独立有界执行资源与入口背压，记录队列等待和 worker 年龄；设计进程隔离、异常隔离和未知提交恢复策略。不能仅在等待外套 timeout 后立刻释放 pins，Python 取消 future 不会终止已运行的文件系统调用。

### 8. P2：全状态复制/JSON处理仍在事件循环内

事实：`semantic_runtime.py:79`、`:107` 同步 deepcopy/比较整个 root；`session_adapter.py:327` 对状态进行同步 json.dumps/encode；读回也有调用线程中的 JSON 解码。

影响推断：大状态、高频语义发布可能抬高 event-loop lag，影响 SSE 和续租调度。数据库异步无法消除 CPU 串行处理。尚无现场耗时证明。

建议：先记录状态大小、复制与编解码耗时、发布频率；基于不可变快照评估卸载或增量处理，避免把活动 Agent 的可变对象直接跨线程访问。

## 分页、索引与缓存的补充判断

- 近期会话索引以 service_unit/tenant/source/agent/user 为前缀，channel 场景还有专门索引（DDL `:89`）。管理员跨用户、未指定 user 的排序不必然能复用同样的索引顺序，需要实际 EXPLAIN；本次没有据此断言全表扫描。
- offset 分页保留 COUNT 与 OFFSET；cursor 分页避免深 OFFSET，但也每页 COUNT（`chat_repository.py:293`、`:322`）。全量 filter_chats/list_chats（`:239`）没有 LIMIT，通用 buffered cursor 会把结果读入内存。应按调用场景限制全量接口，并评估是否确需每页精确总数。
- `projection_cache.py:12` 是有作用域的 ContextVar 缓存，键含 service/scope/session/revision；remember 会清空旧项（`:29`），不是持续增长的通用查询缓存。它帮助复用内容引用，不绕过授权、删除与 revision 校验。
- 不建议先缓存 FOR UPDATE/FOR SHARE 或 authority/lease 校验结果。若引入跨请求缓存，应从不可变 publication 内容开始，键包含完整 scope 与版本；动态 Chat 列表、状态和删除结果要明确多 Pod 失效机制。
- 全应用共用池会让 Tracing、Cron、治理和会话竞争容量（`_app.py:1013`–`:1054`）。总连接预算应按“Pod数 × worker数 × 每池max_connections”计算，还需覆盖滚动发布重叠及 CLI/迁移。拆池也会增加总连接预算，需先测量。

## 验证结果与证据边界

执行命令均在目标 worktree，使用项目虚拟环境：

```text
../../venv/bin/python -m pytest tests/unit/database -q
49 passed in 0.91s

../../venv/bin/python -m pytest tests/unit/app/conversation_storage -q
662 passed in 37.39s
```

这些测试覆盖适配器契约、连接复用、取消、隔离、预编译形状与会话语义，支持上述静态判断；不提供真实吞吐、延迟或数据库故障验收。

**版本差异：**`pyproject.toml:84` 锁定 mysql-connector-python==26.7.0；当前 venv 实测为9.4.0。`driver.py:10` 还有针对26.7 prepared cursor关闭错误的适配，因此必须在锁定版本重新跑驱动契约和目标库集成测试。本次没有擅自升级共享环境。

**未执行目标库测试：**没有连接 MySQL/TDSQL，没有执行 EXPLAIN ANALYZE、真实 NAS 压测、多 Pod 并发和数据库故障切换测试。集成 fixture（`tests/integrated/conversation_storage/conftest.py:30`）要求专用隔离库，未配置时 skip，不能把 skip 算验收通过。

**计划测试存在代表性不足：**`test_query_plans.py:15` 的构造及后续热点 SQL 没有完整体现当前生产查询中的 service_unit 前缀、JOIN 和 guard/pin 流程；主要断言非 ALL 且存在 key，不能证明扫描行数、排序成本、锁竞争或请求总 SQL 数可接受。建议直接复用实际 SQL 构造，补不同用户过滤、长会话及真实选择性数据。未实跑，不推断该测试必然失败。

## 建议实施和验收顺序

1. **建立基线**：使用锁定驱动与目标数据库/NAS；记录池忙闲/等待、SQL与检查次数、事务时长、guard锁等待、续租P99/过期、NAS排队及event-loop lag。使用SQL模板标识记录指标，避免记录敏感参数。
2. **先保续租**：独立总deadline与容量策略；用满池、行锁等待、慢SQL验证健康任务不会因预算配置失去租约。保留过期authority拒绝语义。
3. **减少往返**：按事务复用guard校验、批量locator校验、批量pin/dependency；随后评估租约探活/归还检查合并。逐项比较相同工作负载前后SQL数及P95/P99。
4. **控制NAS和CPU负载**：有界提交资源，慢NAS与停止测试，长会话序列化基线；不牺牲未完成IO的pin保护。
5. **再选择预编译与缓存**：固定批量热点优先，之后基于 prepare/execute/close 计数和缓存命中率决定扩展；不做全面框架替换。

负载至少覆盖：首屏列表与翻页、长会话恢复/持续追加、同scope多session并发、不同scope并发、Tracing同时写入、连接池饱和、NAS高延迟、任务取消与滚动退出。记录数据规模与并发数，并同时验收未授权/已删除内容不可见、GC不误删、未知提交可恢复等一致性条件。缺少业务SLO时不编造统一通过阈值。
