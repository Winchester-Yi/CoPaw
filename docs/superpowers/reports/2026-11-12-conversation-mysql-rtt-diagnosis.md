# 迁移 MySQL 往返延迟与超时诊断

日期：2026-11-12。范围：只读 SQL 采样、连接池与迁移调用路径源码核对；未改变安全校验、连接恢复策略或数据库数据。

## 采样方法与结果

使用本机到已授权 MySQL 的现有配置，逐次执行 `SELECT 1 AS ok,DATABASE() AS schema_name`。每次检查返回值与配置 schema 一致；不记录连接地址、用户名、密码或 schema 名。顺序执行两组各 100 次，非并发负载测试。

| 模式 | 成功次数 | 错误次数 | p50 | p95 | 最大值 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 每次调用 `DatabaseConnection.fetch_one`，含完整租借归还 | 100 | 0 | 240.58 ms | 265.72 ms | 284.33 ms |
| 单次 `acquire` 内固定 session，计时仅查询调用 | 100 | 0 | 60.34 ms | 77.60 ms | 98.79 ms |

原始汇总位于本机 `/tmp/conversation-mysql-readonly-latency.json`；该临时文件不是仓库交付依赖。p50 使用中位数，p95 使用排序后第 95 个样本；测量包含 Python 调度与驱动开销，并非纯网络 RTT。一次短采样没有重现偶发失联，不能否定长时间运行中已经观察到的超时。

普通租借的额外开销与当前实现一致：获取连接时 `ping(reconnect=False)`，执行查询，归还时读取 autocommit 与 session baseline，共约四轮请求。迁移在 `named_lock` 内固定连接，嵌套操作复用 session，因此不能把普通租借的 240 ms 直接当作迁移每条 SQL 成本；固定 session 的约 60 ms 更接近当前热路径量级。大量串行 SQL 仍会让远程网络延迟主导总耗时。

## 超时边界

- 集成测试 fixture 显式使用 connect 10 秒、read/write 30 秒；未覆盖 acquire，因此使用默认 30 秒。
- 生产配置默认 acquire 30 秒、connect 10 秒、read/write 600 秒、cleanup 1 秒。
- read/write 超时按每次操作计算，整个迁移运行数小时本身不会消耗下一条 SQL 的 30 秒预算。
- `ConnectionPool.acquire` 有独立的总 deadline。新建连接的 connect、session baseline 与 ping 使用剩余预算，因此 traceback 中 baseline 的 `timeout=0.286` 可以是先前约 29.714 秒已经耗尽后的剩余额度，不能据此认定配置的读取超时只有 0.286 秒。
- 超时会将 entry 标为不可用并取消未完成操作。此诊断不建议自动重连后继续写入，因为原事务、命名锁与提交结果可能已不确定。

正常采样 p95 低于 100 ms；30 秒足以覆盖正常样本，却无法避免公网异常或服务端停顿。仅提高超时不能修复网络，也会延后发现连接失效。若使用不同的长时间基准超时配置，须明确记录该配置，并保留原故障注入测试的故障检测约束。下一步应在失败时保留阶段、固定 SQL 标识及耗时，避免输出参数或业务正文，区分池等待、建连、读响应与服务端执行。

## 可证明的最小 SQL 去重

`RetentionStore.lock_guard()` 原先先执行 `ensure_guard()`，随后再执行完全相同的 guard `SELECT ... FOR UPDATE`。`ensure_guard()` 已完成唯一 guard 创建竞争处理、锁定读取、scope 校验及固定元数据校验，并返回该行。

在调用方外层事务中，直接返回 `await ensure_guard()` 可省去一次重复 SELECT。`DatabaseConnection.transaction()` 检测到当前 session 已处于事务时，仅进入调用方事务，不额外提交，因此内部函数返回后 guard 行锁仍由外层事务持有。迁移的 `DMLDatabase.transaction()` 委托同一 owner，保持此语义。每次调用仍执行初始化与锁定校验，不缓存 guard 存在状态，不跳过恶意状态检测，也不改变事务锁顺序。

当前网络样本表明每次省一条 SQL 约可节省一轮 60 ms；这是量级估计，未用本次采样证明完整迁移的加速比例。该优化不能替代长时间运行与真实故障恢复验收。
