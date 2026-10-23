# MySQL 启动连接报 cipher None 解包失败

2026-10-19 分析；仓库 `CoPaw`，HEAD `49de0401196c9770184a4803f6097d97f6d56ff6`。
GitNexus 主工作区索引与 HEAD 一致；通过 query/context 核对启动入口与连接池。
初次分析未修改产品代码，未连接生产数据库。后续修复记录如下。

## 修复记录

按用户提供的 JDBC 参数接入核心数据库配置与连接工厂，默认 `ssl_disabled=True`、
`use_unicode=True`、`charset="utf8mb4"`、`time_zone="+08:00"`，连接超时 10 秒、
读写超时 600 秒。驱动与连接池外层共用读写期限。支持环境变量覆盖：
`SWE_DB_SSL_DISABLED`、`SWE_DB_USE_UNICODE`、`SWE_DB_CHARSET`、
`SWE_DB_TIME_ZONE` 及既有 timeout 变量。

需要 TLS 的部署可设 `SWE_DB_SSL_DISABLED=false`；该选项只恢复 TLS 协商，
不等于开启证书或服务器身份验证。会话时区会影响 SQL 时间函数和 TIMESTAMP
的会话转换，不负责改写历史 DATETIME。

回归入口 `tests/unit/database/test_config.py` 覆盖默认值、环境覆盖、驱动参数与外层期限，
并通过真实驱动 connect 方法和无 TLS 元数据的模拟 socket 复现后验证 cipher 分支跳过。
未进行生产数据库连通性验证。完整环境变量说明见
[数据库访问文档](../../docs/database-access.md)。以下保留修复前的原因分析。

## 症状与故障链

```text
_initialize_database_connection
  -> DatabaseConnection.connect -> ConnectionPool.initialize
  -> _create -> _connect_reserved -> mysql.connector.aio.MySQLConnection.connect
  -> cipher, tls_version, _ = writer.get_extra_info("cipher")
  -> TypeError: cannot unpack non-iterable NoneType object
```

项目 mysql extra 固定 `mysql-connector-python==26.7.0`。该版本
`aio/connection.py:179` 与报错完全吻合，但不能仅凭行号确认容器实际版本。

驱动在握手、认证及 `_post_connection()` 之后，为生成 TLS 弃用警告读取 cipher。
条件只检查 `not self._ssl_disabled` 和 writer 方法是否存在，没有检查实际 TLS 状态或返回值。
普通 TCP 连接的 cipher 为 None，直接解包因此失败。

服务端握手没有 `ClientFlag.SSL` 时，驱动清除客户端的 SSL 标志，但不会同步
将 `_ssl_disabled` 设为 True；在未要求证书校验的默认配置下，它可以完成非 TLS
认证和会话设置，再触发上述错误。因此最可能的环境条件是 MySQL/TDSQL 或入口代理
未提供 MySQL 协议 TLS 能力。日志尚不足以排除异常 transport 元数据等其他原因。

修复前 `pool.py::_new_entry()` 没有传 SSL 参数；`DatabaseConfig` 和环境变量加载器
也未提供 SSL 配置入口。仅增加任意 `SWE_DB_SSL_DISABLED` 环境变量目前不会生效。
连接池只是传播驱动异常；启动函数又将它包装为数据库必需的 RuntimeError，导致启动失败。

## 验证证据与边界

对 9.4.0 和独立安装的 26.7.0 调用真实 `MySQLConnection.connect()`，仅替换网络
socket、握手、认证和会话初始化，不连接数据库。先设置模拟服务端字符集版本，再比较：

| ssl_disabled | cipher 返回值 | 两版本结果 |
|---|---|---|
| False | None | 复现完全相同的 TypeError |
| True | None | connect 返回成功 |
| False | 有效 TLSv1.3 三元组 | connect 返回成功 |

这证明末尾 TLS 警告分支的触发条件，不证明生产端认证、TLS 或 SQL 的实际状态。
临时探针 `/private/tmp/mysql_cipher_probe.py`；复跑命令：

```bash
venv/bin/python /private/tmp/mysql_cipher_probe.py
PYTHONPATH=/private/tmp/swe-mysql267 venv/bin/python /private/tmp/mysql_cipher_probe.py
```

## 部署侧核对

1. 在报错容器核对实际驱动版本：
   `python -c 'import mysql.connector; print(mysql.connector.__version__)'`。
2. 使用同一数据库 host/port 和 MySQL CLI 测试；以 `--ssl-mode=REQUIRED` 强制 TLS
   检查该入口是否支持 TLS。密码使用交互输入，不放入命令或日志。
3. 在同一 CLI 会话执行 `SHOW SESSION STATUS LIKE 'Ssl_cipher'`；空字符串表示该
   CLI 会话没有使用 TLS。CLI 成功不代表 Python 驱动已通过，须对照连接地址和模式。
4. 若入口支持 TLS，继续核对代理是否终止或透传 MySQL TLS，以及应用实际 TLS 状态；
   此时不要直接断定为服务端未启用 TLS。

## 修复方向

### 已提供的 JDBC 参数与 Python 对应关系

用户后续提供的连接串明确包含 `useSSL=false`，说明目标连接要求非 TLS。
Python 连接工厂必须显式传入 `ssl_disabled=True` 才能表达该设置；JDBC 查询参数
不会自动被 `mysql.connector.aio` 或应用的数据库配置读取。

- `characterEncoding=UTF-8`、`useUnicode=true`：现有 `charset="utf8mb4"` 可保留，
  驱动 `use_unicode` 默认 True。
- `connectTimeout=10000`：Python `connection_timeout=10`，应用现有
  `SWE_DB_CONNECT_TIMEOUT=10`，单位为秒。
- `socketTimeout=600000`：Python 可分别设置 `read_timeout=600`、
  `write_timeout=600`；应用外层池的 `SWE_DB_READ_TIMEOUT` 与
  `SWE_DB_WRITE_TIMEOUT` 也要一致，否则先被默认 30 秒期限中断。两者并非与 JDBC
  单一 socketTimeout 在所有行为上完全等价。
- `serverTimezone=Asia/Shanghai`：JDBC 时间解释参数，不能机械等同于 MySQL 会话
  时区。若业务还要求 SQL 会话使用北京时间，可显式配置驱动 `time_zone="+08:00"`；
  命名时区依赖服务端时区表；修复后已透传该参数。
- `passwordCharacterEncoding=latin1`：官方 Python aio 驱动没有同名配置，核对的
  mysql_native_password / caching_sha2_password 插件使用 UTF-8 编码密码。ASCII
  密码两种编码一致；非 ASCII 密码应实际验证，不能靠修改连接字符集解决。
- `tdsqlSendClientInfoEnable=true`：TDSQL JDBC 扩展，不能原样传给 Python 驱动；
  如部署依赖该客户端信息，需要核对具体 TDSQL 驱动约定，不能假定普通 conn_attrs
  就是等价实现。

- 环境要求 TLS：使应用连接的实际入口支持并正确协商 MySQL TLS，并根据要求接入 CA
  与身份校验配置，最后验证真实驱动连接和启动。
- 环境明确使用非 TLS：在数据库配置模型、环境加载器及连接工厂显式透传
  `ssl_disabled=True`，保留默认行为，不应无条件关闭所有部署的 TLS。
- 需要同时兼容两种模式：修复或选择已验证修复的驱动，让 TLS 警告检查依据实际 TLS
  状态并处理缺失元数据；不要用捕获所有 TypeError 后当作连接成功的方式绕过检查。
- 9.4.0 与 26.7.0 均包含该问题，不能把升级到项目 pin 版本本身当作修复。

源码依据：[26.7.0 connection.py](https://github.com/mysql/mysql-connector-python/blob/26.7.0/mysql-connector-python/lib/mysql/connector/aio/connection.py)。
