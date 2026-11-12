# 首轮会话逻辑分组失败诊断记录

日期：2026-11-12。首轮固定生产源码基线：`abe53af76b2ad008f91ed63c1d6edc81df8b6dd2`。本报告仅依据现有日志、JUnit、分组节点及源码进行只读分析，未新增数据库操作、未修改实现或测试。

**根因尚未确定。** 两组在接近的时间窗口失败，共享数据库服务或网络通路异常是候选解释；现有证据不能将其确诊为环境问题，也不能排除并发条件下的客户端或业务缺陷。失败项随后复跑通过不改变这一结论。

## 已确认的执行与异常证据

分组计划 `/tmp/conversation-current-shard-plan.json` 对应360条逻辑用例，明确排除 `test_migration_10k_mysql.py`。两个节点文件分别145条与215条，集合无重复、无遗漏，模块内顺序保持。两进程均在17:35:21开始记录启动日志。

| 证据 | 观察 | 能够说明的范围 |
| --- | --- | --- |
| `/tmp/conversation-current-shard-1.log`、同名前缀 `.xml` | 11 passed、1 failed、1 error，进程报告270.16秒。失败项 `test_atomic_rebind_and_busy_target_rejection` setup日志17:38:56；JUnit业务失败记录34.179秒、teardown错误记录21.986秒。 | 普通Chat仓库路径失败，尚未进入该项后续绑定竞争断言。 |
| shard 1业务堆栈 | `repo.upsert_chat` → `_publish_metadata` → `_ensure_no_pending_metadata` → `db.fetch_all` → `pool.release` → `get_autocommit`，30秒后 `TimeoutError: Database operation timed out`。 | 超时发生在SELECT租约归还的连接状态检查，而非已报告SQL owner冲突或迁移锁健康检查。 |
| shard 1清理堆栈 | fixture准备执行子库DROP时，连接池重新连接；MySQL握手读取返回EOF，驱动抛2013 `Lost connection to MySQL server during query`。 | 另一个控制连接的建连也失败，增强共享通路问题的可能性；不证明服务重启或网络故障。该段本身不能证明子库删除成功。 |
| `/tmp/conversation-current-shard-2.log`、同名前缀 `.xml` | 20 passed、1 failed，进程报告272.85秒。失败项 `test_http_context_numeric_stale_and_sql_failure_are_distinct` setup日志17:38:53，ChatManager日志17:38:58；JUnit记录61.456秒。第一次context-usage请求期望200、得到503。 | 失败发生在测试显式注入 `StorageUnavailable` 之前；该次503不是测试设计中的预期负例。日志未保留响应背后的异常cause。 |
| `/tmp/conversation-current-failure-repro.xml` | 两失败项单进程复跑2 passed，23.548秒。 | 降低稳定、无条件逻辑回归的可能性；不能排除并发软件缺陷或暂态故障，不能证明“环境原因”。 |

两项失败的setup相差3秒，整体进程结束时间接近。日志没有每次SQL和异常发生的精确时间戳，不能仅据这一关联断言是同一事件。

## 调用路径与排除范围

`ConnectionPool.release` 在连接归还时依次检查未结束事务、`get_autocommit` 和会话baseline。每项驱动操作由 `run` 施加配置的30秒读取预算，异常会将连接标记不可用并丢弃。首组堆栈显示真实驱动等待超时，不是等待迁移RPC队列的异常。

第二组HTTP路径为 `get_chat_context_usage` → `_indexed_history_call` → `history.context_usage`。后者在事务内查询Chat和Session数值快照，随后复核Chat。测试将bundle `read_extent` 替换为立即失败，因此不是成功依赖NAS读取的路径。`_indexed_history_call` 将 `StorageError`、`OSError`、`RuntimeError` 和MySQL驱动异常统一映射为503；这包含数据库超时，也包含独立完整性或运行时错误，现有503日志不足以区分。

这两个失败路径均未调用迁移 `MigrationCoordinator`、`MigrationLockGuard` 或命名锁健康探测。普通数据库事务的任务归属机制仍在，但没有任务归属冲突异常证据，不能据“有SQL owner”直接归因。

按实际节点顺序，首组只完成archive runtime的5项及Chat仓库的6项，第二组只完成API模块的20项。专门KILL持锁连接、COMMIT响应丢失、迁移全服务屏障等模块均尚未执行。已执行前序中的故障模拟为pytest局部monkeypatch、随机子库内UPDATE或本地正文操作；未发现前序KILL、SET GLOBAL、服务器关闭或跨schema删除路径。因此，“本次前序专门断连接测试误杀另一组”没有执行顺序支持。此排除范围限于这两份节点列表实际已执行的前缀，不覆盖外部进程或服务端活动。

runner为每进程生成随机 `test_conversation_acceptance_<uuid>` 父库，通过环境传给fixture；fixture验证测试库命名、拒绝业务schema、检查空父库，并为每case建立随机 `test_conversation_<uuid>` 子库。清理仅针对本fixture生成的子库，runner最终清理自己的父库。pytest monkeypatch和环境变量不跨进程共享；schema隔离也不隔离同一数据库服务器、同一账号、网络路径及服务端容量。不能因随机库名就排除共享资源问题。

## 四个可证伪候选

| 候选原因 | 当前支持与不足 | 最小区分证据 |
| --- | --- | --- |
| 网络短暂中断、代理重置或数据库服务重启 | 同窗发生驱动读超时及新连接握手EOF，与该候选相容；API503未记录cause，服务端事件未知。 | 查询同窗服务错误日志、uptime/重启事件或已有网络监控，确认是否有连接重置。若服务端持续稳定且客户端请求正常抵达并完成，应转向其他解释。 |
| 双进程共享服务器连接、握手或CPU/IO压力 | 两进程共用服务，短时资源压力可能影响普通查询与握手；当前无max_connections、连接峰值或资源饱和证据，不能直接称连接数耗尽。 | 对照同期Threads_connected、连接上限、Aborted_connects、CPU/IO及慢请求记录。低负载且无连接异常会削弱这一解释；串行/双进程差异仍需结合指标，而非仅看是否复现。 |
| 客户端连接池或驱动在并发、取消或协议状态处理中的缺陷 | 首次明确异常在归还连接时的驱动状态读取；堆栈并不证明是服务器无响应，也可能是客户端协议状态问题。独立进程也可能同时触发相同缺陷。 | 后续记录失败操作phase、异常类型/errno、连接ID及任务上下文；比较同两项串行与双进程运行，必要时取得受影响连接的协议/服务器侧观测。若服务端同窗异常已明确，也仍须核对客户端失败处理是否正确。 |
| API用例独立的完整性、作用域或运行时错误，与首组故障仅时间重合 | API503可由多类异常产生，当前没有cause；因此不能自动并入首组数据库超时。 | 捕获HTTP转换前的异常链。若为完整性/作用域/任务错误，应按独立软件问题定位；若为相同数据库读超时或2013，才增强共同通路解释。 |

## 最小后续验证

先等待当前剩余327项单进程运行退出并保存结果，不为本诊断中断或重启它。随后在固定源码、相同隔离库约束下，仅对两失败项做受控串行与双进程对照；记录每次异常的phase、类型、errno、连接ID和API503原始cause，配合同窗服务端只读指标。诊断记录不得包含凭据、SQL参数或会话正文。

单次未复现不足以关闭问题；若异常再次出现，以上信息应能区分服务器/网络、客户端连接状态与独立API错误。不得通过放宽超时、添加自动换连接续写、跳过断言或将历史通过数字合并为当前全量成功来消除失败记录。本报告不提供生产放行或万级/NAS性能验收结论。
