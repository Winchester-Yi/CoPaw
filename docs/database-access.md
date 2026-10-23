# 核心数据库接入

核心 `src/swe` 使用 `mysql-connector-python==26.7.0` 的异步驱动。安装 `pip install '.[mysql]'`，从应用依赖注入取得共享 `DatabaseConnection`；Store 不自行创建连接池，不导入驱动或操作游标。独立 market、monitor、scheduler 服务不在本次迁移范围。

## 增删改查

SQL 由模块维护，数据始终通过 `%s` 参数传入。表名、列名不能来自用户输入。JSON、模型转换和租户过滤由业务 Store 负责。

```python
from swe.database import DatabaseConnection

async def save_note(db: DatabaseConnection, source_id: str, text: str):
    note_id = await db.insert(
        "INSERT INTO notes (source_id, text) VALUES (%s, %s)",
        (source_id, text),
    )
    return await db.fetch_one(
        "SELECT id, text FROM notes WHERE source_id = %s AND id = %s",
        (source_id, note_id),
    )

affected = await db.execute(
    "UPDATE notes SET text = %s WHERE source_id = %s AND id = %s",
    (text, source_id, note_id),
)
affected = await db.execute(
    "DELETE FROM notes WHERE source_id = %s AND id = %s",
    (source_id, note_id),
)
rows = await db.fetch_all(
    "SELECT id, text FROM notes WHERE source_id = %s ORDER BY id LIMIT %s",
    (source_id, 100),
)
```

| 方法 | 返回值 |
|---|---|
| `execute(sql, params=None)` | 影响行数（upsert 可能为 0、1、2） |
| `insert(sql, params=None)` | 驱动 lastrowid，未生成 ID 时保留 `None` / `0` |
| `execute_result(sql, params=None)` | 不可变 `ExecutionResult(affected_rows, lastrowid)` |
| `execute_many(sql, list_of_tuples)` | 总影响行数，空列表返回 0 |
| `fetch_one(sql, params=None)` | 字典或 `None` |
| `fetch_all(sql, params=None)` | 字典列表，空结果为 `[]` |

## 事务与连接所有权

```python
async with db.transaction() as tx:
    note = await tx.fetch_one(
        "SELECT id FROM notes WHERE source_id = %s AND id = %s FOR UPDATE",
        (source_id, note_id),
    )
    await tx.execute("UPDATE notes SET text = %s WHERE id = %s", (text, note["id"]))
    await db.execute("INSERT INTO note_events (note_id) VALUES (%s)", (note["id"],))
```

事务内通过 `tx` 或同一 `db` 发出的操作使用同一物理连接。成功退出后提交，异常或取消时有界清理并丢弃不确定连接；提交响应丢失直接报错，数据库层不自动重放 SQL。嵌套事务加入外层，不创建 savepoint，也不提前提交。

`db.acquire()` 也返回同一套 `DatabaseSession` 查询接口，但它只固定连接，不开启事务。Session 只属于创建它的任务，退出后失效；禁止把它交给子任务或在事务中并发使用同一连接。需要独立并行工作时在事务外发起，由各任务独立借用。

`async with db.named_lock(key, timeout=10)` 用 MySQL 命名锁固定连接；内部可开启事务。释放锁无法确认时关闭连接。锁等待同时受数据库读期限限制。

迁移既有精确锁键可使用 `exact_name=True`，保留旧版本实例使用的名称；名称必须为 1–64 个 UTF-8 字节。普通调用保持默认哈希命名。

不支持业务代码跨租约保留任意会话状态（用户变量、临时表、手动锁等）。需要此类行为时须在同一租约内完整恢复；常用 sql_mode、时区、字符集、当前库、事务及 autocommit 基线会在归还时校验。不要通过 SQL 自行 BEGIN/COMMIT，使用 `transaction()`。

## 固定模板 prepared 批量写入

```python
from swe.database import PreparedInsert

NOTE_INSERT = PreparedInsert(
    "INSERT INTO notes (source_id, text) VALUES ",
    "(%s, %s)",
)
count = await db.insert_prepared(
    NOTE_INSERT,
    ((source_id, text) for text in texts),
    max_rows=100,
    max_bytes=256 * 1024,
)
```

模板应为模块级常量，只包含可信 SQL；参数行可用迭代器。数据库级入口将所有传输块放在同一事务中；Session 的同名方法必须在事务内使用。返回处理行数，成功返回仅表示当前事务内执行成功；若加入已有外层事务，仍需等待外层提交才可确认持久化。

每块执行一次 binary prepared execute，按行数、估算字节、65535 参数与服务端包限制分块；单行超过批次预算可独占，超过服务端保守包预算则报错，不截断。每物理连接最多缓存 4 种模板/行数形状，独占借用，SQL 对象复用；不使用 prepared executemany 的逐行循环。

## 配置与验证

沿用 `SWE_DB_HOST/PORT/USER/ACCESS/NAME/MIN_CONN/MAX_CONN` 及原有密码解析。新增秒级环境变量：

| 变量 | 默认秒数 |
|---|---:|
| `SWE_DB_ACQUIRE_TIMEOUT` | 30 |
| `SWE_DB_CONNECT_TIMEOUT` | 10 |
| `SWE_DB_READ_TIMEOUT` | 600 |
| `SWE_DB_WRITE_TIMEOUT` | 600 |
| `SWE_DB_CLEANUP_TIMEOUT` | 1 |

连接参数按当前部署的 JDBC 配置设置，支持以下环境变量覆盖：

| 变量 | 默认值 | 含义 |
|---|---|---|
| `SWE_DB_CHARSET` | `utf8mb4` | UTF-8 字符集，支持 emoji |
| `SWE_DB_USE_UNICODE` | `true` | 文本解码为 Unicode |
| `SWE_DB_SSL_DISABLED` | `true` | 对应 `useSSL=false`；需要 TLS 的部署设为 `false` |
| `SWE_DB_TIME_ZONE` | `+08:00` | 数据库会话使用北京时间，无需服务端命名时区表 |

布尔值接受 `true/false`、`1/0` 等 Pydantic 支持的形式；空字符串沿用默认值，
其他无法识别的值会在配置校验时报错。读写超时同时用于驱动和连接池外层操作期限；
连接超时保持 10 秒，连接池获取超时仍为 30 秒。

`serverTimezone=Asia/Shanghai` 是 JDBC 对时间值的解释配置；这里显式设置的是
MySQL 会话时区，会影响 `NOW()` 与 `TIMESTAMP` 的会话转换，不转换已保存的
`DATETIME` 数据。`passwordCharacterEncoding` 与 `tdsqlSendClientInfoEnable`
没有可直接照搬的官方 Python 驱动参数，本次不适配。

期限须为有限正数；池大小满足 `0 <= min <= max` 且 `max > 0`。取消不代表驱动已完成清理，迟到任务保留引用并消费异常，连接实际退出前仍占池容量。关闭在清理预算后返回，残留连接计数写日志。

若驱动 shutdown 本身失败，连接保持隔离并继续占容量，同一数据库对象拒绝在残留连接存在时重连；不能据此认为底层 socket 已关闭。应排查失败原因，并通过进程生命周期最终回收资源。

隔离库 smoke test 位于 `tests/integrated/test_database_mysql.py`，默认跳过。显式设置 `SWE_TEST_DB_HOST`、`SWE_TEST_DB_NAME`、`SWE_TEST_DB_USER`、`SWE_TEST_DB_PASSWORD` 后运行。测试会创建并删除唯一命名的表，必须使用隔离测试库。此处测试密码直接使用，不套生产前缀解析。

真实 TDSQL 的代理 prepared、认证/TLS、故障注入、容器和 Python 矩阵、A/B 性能仍是上线验收项；本地单元/协议测试不能代替这些结果。
