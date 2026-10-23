# 核心 MySQL 驱动迁移设计

日期：2026-10-16。源码基线：`a13bbfa88`。状态：已完成本地实施与单元/协议回归；真实 TDSQL、部署矩阵及性能验收待执行。

本文应用 brainstorming 与 GitNexus exploring 流程，核对项目源码、既有方案、Oracle 官方文档及固定版本驱动源码。用户已确认范围为核心 `src/swe` 共享数据库层，并确认生产 TDSQL MySQL 兼容版可以使用官方驱动。独立 market/monitor/scheduler 服务不迁移。

## 1. 目标与边界

迁移到 `mysql-connector-python` 的 `mysql.connector.aio`，对 Tracing Span INSERT 使用服务端 prepared 多行写入，移除该路径 aiomysql/PyMySQL 的 SQL 字符串转义和数据内嵌 SQL。

保持现有表结构、SQL 业务语义、JSON 表示、脱敏、source_id 隔离和所有已接纳 Span 的确认语义。保留当前单 writer、容量背压、版本屏障、幂等重试、关闭期限及清理异常日志。不新增 payload 截断、磁盘 spool、死信或一般 SQL 自动重放。

迁移不会把全部 CPU 工作移到线程：JSON、深拷贝、UTF-8 及二进制类型编码仍可能运行在 event loop。优化目标是消除特定转义成本，并通过测量确认净收益。

## 2. 当前事实与证据

| 位置 | 当前行为 | 对迁移的含义 |
|---|---|---|
| `src/swe/database/connection.py` | aiomysql 池，autocommit=True；高层查询/写入封装及 acquire/transaction/named_lock | 保留高层接口；替换池、游标和取消清理实现 |
| `src/swe/tracing/store.py:451` | 同步组装 23 字段参数并 json.dumps，事务内 execute_many，mutable-field upsert | prepared 替换执行方式，保留列、JSON、upsert 与事务确认 |
| `src/swe/tracing/writer.py` | 普通批次最多100行/估算256KiB；队列10000条/8MiB；超大行独占 | 不恢复旧方案的1MiB批次，保留现有预算 |
| `src/swe/app/_app.py` | 创建主共享数据库连接 | 各使用该连接的 Store 一并迁移；连接所有权不变 |
| `src/swe/app/async_tasks/db.py` | 缺省主连接时另建连接 | 必须走同一驱动配置及关闭契约 |
| 直接 acquire 的 Store | 使用 conn.begin、同步返回 cursor context、get_autocommit 等 aiomysql 接口 | 不能只替换 import，需要明确适配 |
| `pyproject.toml` | Python >=3.10,<3.14，未声明数据库驱动依赖 | 补齐明确安装入口与版本 pin |
| `deploy/Dockerfile.local` | 单独安装 aiomysql/pymysql | 与正式 Dockerfile 的依赖入口统一 |

本地虚拟环境已有 Connector/Python 9.4.0、aiomysql 0.3.2、PyMySQL 1.1.2、asyncmy 0.2.11，未为设计修改环境。已离线调用本地官方二进制协议编码器验证 None、bool、数字、带微秒 datetime、中文、emoji、NUL 与反斜线参数可构造执行包；没有真实数据库往返，因此不构成 TDSQL 兼容性或性能验收。

最新公开发布候选为 `mysql-connector-python==26.7.0`（本次核对的 PyPI 页面及源码 tag）。建议以该固定版本进入验证；本地9.4.0只能作为已有实现参考，不默认为交付版本。若候选未通过矩阵，重新评审具体版本，不能自动降级。

已核对 26.7.0 的 aio cursor/pooling/connection 源码：

- prepared execute 走 cmd_stmt_prepare/cmd_stmt_reset/cmd_stmt_execute；prepared executemany 循环 execute。
- prepared SQL 复用判断使用字符串对象身份，不能每批构造内容相同但对象不同的 SQL。
- 官方异步池固定容量、上限32，耗尽时 get_nowait 后抛 PoolError；默认归还 reset_session。
- close 会尝试关闭游标、发送 QUIT；shutdown 则关闭 socket，适用于连接状态不确定时的中止。
- 普通游标仍调用 converter.escape。替换驱动但继续普通 executemany，不能实现本次转义优化目标。

GitNexus 绑定仓库 `/Users/shixiangyi/code/Swe`（CoPaw），已运行 index-only 刷新。MCP 某些查询仍返回旧快照/不相关前端关联，且 FTS 构建有降级提示；调用清单以源码搜索交叉验证，不据图谱宣称影响范围完整。

## 3. 备选方案与选择

| 方案 | 收益与代价 | 决策 |
|---|---|---|
| 官方 aio 驱动＋核心共享层迁移＋多行 prepared | 直接避开 Span SQL 转义；统一连接、事务、锁语义；需要池和直接连接调用适配 | 推荐，符合已确认范围 |
| 仅 Tracing 使用独立官方驱动池 | 影响面较小，但增加第二套池、额外数据库连接预算和生命周期 | 本轮不选 |
| 改为 asyncmy 或同步 C 驱动＋线程池 | asyncmy 现有文本游标仍需转义；同步线程方案需额外解决事务线程亲和、取消与许可生命周期 | 不作为本次路线 |

最终进程仅选择一个核心驱动，不做运行期间自动切换或 SQL 失败后回落旧驱动。灰度使用不同镜像版本；回滚恢复旧镜像，不引入长期双后端抽象。

## 4. 适配层与改动范围

保留 DatabaseConnection/TDSQLConnection 导出、配置加载优先级及原有高层方法。按用户补充要求收拢业务 Store 的原始连接操作：新增不可变 `ExecutionResult(affected_rows, lastrowid)`、`execute_result`、`insert`；`acquire` 与 `transaction` 返回 `DatabaseSession`，提供同一套参数化 SQL 接口，不向 Store 暴露驱动游标、begin、commit、rollback。接入示例见 [数据库操作](../../database-access.md)。

七处 Store（skill_readiness、featured_case、workflow/config_store、goals、skill_result、asset_upload_record、feedback）使用共享 facade；Goals 删除自建事务包装。读取返回字典；插入保留 lastrowid 的 None/0 语义；写入返回影响行数。未引入 ORM、通用条件构造器或动态标识符拼接。Readiness 使用 `named_lock(..., exact_name=True)` 保留混合版本 Pod 之间原有的锁键，默认命名锁的哈希行为不变。

`insert_prepared` 接受模块级可信 `PreparedInsert(prefix, row_template, suffix)` 和参数行迭代器；数据库级入口包裹一次事务，Session 入口要求已有事务，错误或输入迭代失败使会话不可继续提交。业务 SQL、租户过滤、JSON 和模型映射仍由 Store 负责。普通 `execute_many` 保持 affected-row 语义。

普通查询使用 buffered dictionary cursor，维持已缓冲结果语义。实际游标创建为 `await conn.cursor(...)`，事务使用 `start_transaction`，均封装在数据库包内部。结果兼容、异常/取消和清理行为通过对应契约测试验证；真实数据类型及服务端协议仍需隔离库验收。

## 5. 最小异步连接池

使用官方裸 MySQLConnection，由项目管理有界租约；不直接套官方池。理由是需要既有 min/max 动态创建、池满可取消等待、取消损坏连接可丢弃，以及 prepared handle 不被默认 session reset 失效。

池只负责创建、独占借用、归还、丢弃与关闭，不重放 SQL、不自动故障转移。沿用现有 min_connections=2、max_connections=10；校验 0<=min<=max 且 max>0。通过 Condition/可取消等待和显式 creating/idle/leased/closing 计数维护状态，网络 await 在全局锁外执行。

不变量：creating＋idle＋leased＋尚未实际销毁的 discarded/closing 连接不超过 max_connections。调用者取消只撤回等待，已经创建/清理中的连接须实际结束后才释放其容量；否则反复取消可能突破连接预算。close 停止借出、唤醒等待者，不允许关闭后迟到的新连接重新入池。

建连先构造连接对象再 await connect，保留握手失败/取消时的 socket 所有权。闲置连接借出前执行有界 ping(reconnect=False)；失效连接丢弃后重建，不能在持有 prepared/事务状态的对象上偷偷 reconnect。ping 的额外往返必须纳入性能测量。未来是否节流健康检查由测量决定，本次不添加复杂保活任务。

成功归还前确认无未结束事务、无未消费结果、autocommit 回到基线、命名锁已释放；异常或不确定时丢弃连接。不给每次归还无条件 reset_session，以保留 prepared；只允许现有受管理会话状态，业务若需要任意 session 变量必须显式恢复，否则丢弃。字符集/时区/sql_mode 的变更纳入会话基线管理，不跨调用方泄漏。

## 6. 事务、锁与请求上下文

所有事务 SQL 绑定同一物理连接；嵌套 transaction 加入现有事务，不引入 savepoint，不额外提交。内层异常传播到外层时由外层回滚；调用方吞掉内层异常的行为不借迁移修改。

ContextVar 绑定需包含 DatabaseConnection 实例标识、owner task、连接及是否开启事务。不同数据库实例不能误用同一连接；子 task 继承父任务事务时拒绝并发使用，不能把同一个 MySQL socket 交给多个协程。writer 和共享关闭任务继续使用空 Context。

named_lock 持有同一租约执行 GET_LOCK/RELEASE_LOCK。命名锁固定连接与“已经启动事务”分开记录：只持有锁不代表已经 begin；锁内 transaction 应在同连接启动事务。释放锁失败、取消状态不明时丢弃连接，不能把仍持锁会话交给下一请求。

只在 SQL 尚未执行时重建失效的空闲连接。不自动重放普通 INSERT/UPDATE/COMMIT。commit 响应丢失属于结果未知，异常向上传播；Tracing 继续依靠稳定 span_id＋幂等 upsert 重试，其他业务沿用自身决策。

## 7. Prepared 批量写入

保留 Span 的23列与现有 ON DUPLICATE KEY UPDATE mutable 字段，参数行按需序列化，不先构造超大无界 params_list。SQL 形如固定 INSERT prefix＋N组23个占位符＋固定 upsert suffix，展平当前分块参数，调用一次 prepared execute。

普通批次行上限沿用 TracingConfig.batch_size，字节预算沿用 max_batch_bytes=256KiB；传输层再受服务端 max_allowed_packet 的保守可用预算约束。每物理连接初始化读取 packet 上限；将协议固定开销、NULL bitmap、类型表、长度前缀和UTF-8数据计入估算。当前保守队列估算不因换成二进制协议立即放宽。

参数总数不超过65535；23列理论最多2849行，实际默认100行远低于它。单条超大 Span 仍可独占 writer，但若超过服务器包上限，明确报错，不截断、不降级拼接 SQL；这类永久错误仍会触发既有重试/背压/超时，本次不增加丢弃政策。

所有传输分块置于一次 transaction 内，全部成功提交后才返回确认数量；外层事务存在则复用且不提交。第二块失败应回滚前块。writer 的版本确认只发生在其独立事务提交后，不因 execute 成功或 upsert affected_rows=0 而误判。

不能使用 prepared executemany。对于N块，要求恰好N次 COM_STMT_EXECUTE；首次形状可增加 PREPARE，官方实现可能每次还发送 RESET，BEGIN/COMMIT/PING也计数。不得宣传“整批只有一次数据库请求”。

每物理连接最多缓存4个 INSERT prepared cursor，LRU 淘汰；SQL键由固定模板身份和行数组成，缓存存储原字符串对象，复用时传同一对象。固定占位符在项目层直接转换为原生 `?`，避开官方 `%s` 转换正则对长 SQL 的重复扫描；不改写任意 SQL。禁止游标跨物理连接复用，禁止并发使用同一游标；断连、会话重置、重新建连时整体失效，淘汰/关闭时释放 handle，释放失败则丢弃连接。缓存上限因此为4×每Pod连接上限；还需计算Pod数量与其他服务占用，验收 max_prepared_stmt_count。

缓存在物理连接租约内，prepared cursor 生命周期长于单次借出；不得在每批 async cursor context 退出时把它关闭。库内部字段只用于源码核对，不直接修改 _executed/_prepared 来绕过 RESET。

26.7.0 的 prepared cursor.reset 会吞掉驱动 Error 类型的 statement-close 异常。为保证 LRU 淘汰失败可触发隔离，使用最小 `ManagedMySQLConnection` 子类覆盖公开 `cmd_stmt_close`，把这类失败包装为保留 cause 的 RuntimeError；没有改写私有游标字段或协议编码。

## 8. 取消、期限与资源回收

引入明确的 acquire/connect/read/write/cleanup 期限，建议初始分别30/10/30/30/1秒，作为数据库配置而非隐式无限等待。它们是迁移建议值，测试应允许短期限注入；named_lock 的服务端等待仍使用原timeout。确认屏障与 writer shutdown 继续使用 TracingConfig 的现有期限，两者不互相替代。

一旦查询/事务被取消或读写超时，先标记物理连接不可复用，再作有界最佳努力回滚；协议状态不明时优先 shutdown socket，避免正常 close 先发送 QUIT 又卡住。cleanup 采用 asyncio.wait 等硬截止等待，不使用会等待取消完成的无界 wait_for(task) 组合。无法及时退出的清理任务保留引用、记录异常及资源占用，不能虚假释放容量。

清理会先 shutdown 帮助中断已有 I/O，等待已记录的操作实际退出；存在迟到操作时再 shutdown 一次，防止握手在首次关闭之后才创建 socket。最终关闭失败保留隔离槽位，同一数据库对象不能在残留连接存在时重新建池。取消请求按任务只发送一次，重复 close 不会再次打断清理。

保留原 CancelledError 或主业务异常；rollback/close 的次级异常记录 traceback，不覆盖原异常。所有 done task 都读取结果；迟到异常也由持有任务的清理路径消费，不能产生无人读取的任务异常。

writer 排空超时后仍只等待 min(shutdown_timeout,1秒) 取消宽限。若仍运行，报告未确认数据并返回超时；manager 延后存储关闭直至 writer 退出。驱动迁移不能重新引入无限 await、重复取消破坏清理、连接提前复用或吞掉普通清理异常。

Python协作式取消不能强杀任意不响应的协程。pool close 也应有有限总预算并报告残留租约，剩余资源按异常状态隔离；不承诺所有异常驱动均能被进程内彻底回收。Kubernetes最终终止仍可能丢失内存数据。

## 9. 依赖、部署、灰度与回滚

建议新增 `mysql` 安装 extra，包含精确 pin 的 mysql-connector-python==26.7.0；两个核心 Dockerfile 使用 `.[ollama,mysql]` 同一入口。core 不再要求 aiomysql/pymysql；独立服务按自己的依赖保留，不能仓库全局删除。是否进入基础 dependencies 应按数据库是否可选的现有产品契约实施，本设计选择extra以保留无数据库模式。

验证 Python3.10–3.13；生产主要Python3.12、Linux容器，具体内核3.15不能推导glibc版本。候选C wheel的manylinux标签需核对实际镜像；aio使用纯Python路径，不能把C扩展性能当作异步性能。可用通用wheel，但应实际构建镜像、检查TLS/CA/认证插件及启动入口，不为驱动迁移顺带升级基础镜像。

连接环境变量/密码解析保持现有契约，连接参数映射 db→database；不增加敏感配置输出。驱动版本、后端类型及计数可记录，SQL参数和payload不进入诊断标签。

先在隔离TDSQL库验收，再以少量Pod新镜像灰度，不双写、不同时在单进程维护两套核心池。数据协议/表结构不变，可回滚到a13bbfa88对应旧镜像；回滚前尽量排空writer，超时须报告内存数据风险。回滚不能解释为撤销已经提交的数据。

## 10. 验收矩阵

| 类别 | 必须验证的情景 | 通过标准 |
|---|---|---|
| 协议 | 真实官方编码器＋协议计数；100行及尾块 | 数据不进入SQL文本；Span路径escape调用0；每块1次EXECUTE，无逐行退化 |
| prepared生命周期 | 同连接同形状、不同尾块、LRU淘汰、重建 | 同形状不重复prepare；每连接最多4handle；无跨连接复用 |
| 数据 | 中文/emoji/引号/反斜线/换行/NUL/NULL/日期微秒/Decimal/bytes | 新旧写入后的值一致；JSON格式、source_id和用户字段保持 |
| 普通Store | read/write/execute_many/lastrowid/rowcount/百分号/DDL | 现有业务回归通过，异常errno兼容 |
| 原子性 | 第二块失败、嵌套事务、命名锁内事务 | 不部分提交，不提前提交，不换连接 |
| 连接池 | 饱和等待、等待取消、握手取消、健康检查失败、归还取消 | 容量不超限、不丢slot、不把坏连接归还 |
| 故障 | wait_timeout、断网、commit响应未知、rollback卡住/失败 | 异常边界正确；无普通SQL自动重放；Tracing幂等确认 |
| 关闭 | 排空、宽限内普通异常、宽限外迟到失败、重复取消 | 有限返回；保留未确认数据；所有任务异常被消费 |
| TDSQL | 实际认证/TLS、代理prepared、事务/GET_LOCK、包限制 | 真实隔离库测试通过；确认连接绑定/会话行为及错误码 |
| 部署 | Python版本、生产架构、正式/本地镜像、无数据库模式 | 依赖可安装、启动/关闭正常、旧镜像可回滚 |

## 11. 性能实验与决策门槛

先在隔离环境固定旧基线a13bbfa88与候选镜像。分别记录现有SQL编码离线CPU、prepared编码离线CPU，以及真实TDSQL端到端数据，不能把离线结果当吞吐提升。

输入覆盖小Span、中文嵌套大输入、emoji/引号密集输入、单条超大输入；稀疏与持续负载、多租户1/8/32并发。突发、固定时长稳态、故障恢复分别测量；使用完全相同的脱敏与JSON配置。

记录进程CPU秒/千Span、loop p95/p99/max、吞吐、批次P50/P95、接纳等待、确认等待、RSS/容器内存、池等待/创建/丢弃、PREPARE/RESET/EXECUTE/COM_QUERY次数、失败与重试率、数据库CPU/锁等待。连接健康检查、BEGIN/COMMIT和RESET不能从成本中扣除。

正确性与资源有界为硬门槛。建议性能准入：目标大payload的客户端CPU/千Span至少降低20%；相同吞吐下小payloadCPU与loop p99不回退超过10%；持续吞吐不降低超过5%。至少5轮报告分布；这些是建议验收阈值，不是收益承诺，噪声较大时延长测量而非选择性摘取样本。

若协议正确但净收益不达标，先定位JSON/UTF-8/二进制编码、RESET/PING额外往返或服务端prepare成本；不以“escape消失”单独宣布成功，也不擅自改payload或启用线程池补救。

## 12. 实施顺序与交付物

1. 固定候选依赖，补高层数据库与直接连接Store契约测试；建立TDSQL隔离库验收及旧镜像基线。
2. 实现最小池、官方游标适配与事务/锁/取消规则；保留普通execute_many；核心业务回归。
3. 实现Span多行prepared、每连接有界缓存和传输分块；协议与原子性测试。
4. 重跑完整tracing生命周期及相关SDK/runner/API回归，覆盖最近两轮关闭修复。
5. 真实TDSQL故障注入、容器/Python兼容矩阵和同负载A/B；提交原始数据及结论。
6. 通过准入后小流量灰度，观察后扩大；任一正确性问题立即回滚镜像。

已交付本地迁移实现、固定依赖入口、接入文档和默认跳过的隔离库 smoke test；未部署。具体TDSQL内核版本、是否经过代理、认证/TLS参数和服务端限制由隔离环境预检记录；用户已确认官方驱动可用，但这不代替prepared及故障验收。

## 来源

- 项目历史方案：`analysis/playbook/performance-optimization-plan.md` 与 `skill-freshness-and-span-write-performance.md`。其中writer尚未实施、1MiB分块的旧描述以当前源码和本文为准。
- [Oracle异步连接说明](https://dev.mysql.com/doc/connector-python/en/connector-python-asyncio.html)
- [Oracle版本支持矩阵](https://dev.mysql.com/doc/connector-python/en/connector-python-versions.html)
- [PyPI固定候选26.7.0](https://pypi.org/project/mysql-connector-python/26.7.0/)
- [26.7.0 aio cursor源码](https://github.com/mysql/mysql-connector-python/blob/26.7.0/mysql-connector-python/lib/mysql/connector/aio/cursor.py)
- [26.7.0 aio pooling源码](https://github.com/mysql/mysql-connector-python/blob/26.7.0/mysql-connector-python/lib/mysql/connector/aio/pooling.py)
- [26.7.0 aio connection源码](https://github.com/mysql/mysql-connector-python/blob/26.7.0/mysql-connector-python/lib/mysql/connector/aio/connection.py)
- [asyncmy 0.2.11 cursor源码](https://github.com/long2ice/asyncmy/blob/v0.2.11/asyncmy/cursors.pyx)
