# MySQL 连接池销毁与容量滞留

2026-10-16 代码检视。分析时 HEAD：`5d6cdacea1903fc8079ec61826dcc6115021c341`；直接比较基线：`3fb4328ca2d8bb572fb67996e118bf77efacf98c`；同时追溯迁移提交 `94fcc1326`。下文故障与源码行号描述该基线，修复记录见下一节。

## 修复记录

按第 4 节的最小方案修复：`_destroy()` 对 pending 或不可用连接直接 shutdown；`pool.close()` 保存清空前的空闲集合，保留其健康标记，仍使租出的 Session 失效。已有销毁任务保留首次选定的策略，避免并发 close 重新把健康空闲连接标为不可用。保留正常 close 异常兜底、迟到握手二次关闭及未确认资源隔离。

正式测试移除隔离场景中人为注入的 close 异常，增加 ping/query/baseline 已完成失败、握手失败、健康空闲关闭和租约失效回归。修复前相关测试 6 failed、1 passed；独立审查补充的并发 close 测试也先确认失败。最终使用固定驱动 26.7.0 运行数据库及 Tracing 测试 **297 passed**，相关文件 pre-commit 全部通过，独立复审无阻塞问题。探针和内存候选属于修复前分析证据，不代替正式回归。

未部署，未连接真实 TDSQL 验收。

## 结论与缺陷

**[P1] `src/swe/database/pool.py:263` 丢失了不可用连接的强制关闭分支。**

失败的 ping/query 已经结束时，`entry.usable=False`，但 done callback 已从 `pending` 删除该任务。当前 `_destroy()` 只检查 `pending`，因此进入普通 `connection.close()`。该方法会关闭游标、发送 QUIT，然后才关闭 socket；同一条异常链路仍可能阻塞协议写入。槽位必须等 `_destroy()` 最后从 `_entries` 删除才能释放，后续获取可能耗尽等待期限。

本轮确认一项运行时回归，以及造成反复修补的状态建模和测试缺口。下述健康连接关闭超时属于现有回收策略的剩余边界，不另计为这次提交新引入的独立缺陷。

## 1. 完整故障链

```text
acquire() 探活失败 / Session 查询失败 / release() 基线校验失败
  -> run() 或上层将 entry.usable 标为 False
  -> 失败任务完成，pending.discard 回调移除任务
  -> discard() 创建 _destroy() 后最多等待 cleanup_timeout
  -> _destroy() 看到 pending 为空，调用 close()
  -> 游标关闭或 QUIT 写入阻塞
  -> discard() 的等待到期返回，但不会取消 _destroy()
  -> entry 继续保留在 _entries
  -> len(_entries) 达到 max_connections 时，新的 acquire() 等待并可能超时
```

关键源码：

- `pool.py:64–76`：操作任务登记、done callback 和失败时的不可用标记。
- `pool.py:146–197`：容量判断、探活失败后的丢弃与重试。
- `pool.py:224–255`：异常会话及基线不符的归还处理。
- `pool.py:257–283`：销毁分支与容量释放顺序。
- `pool.py:285–294`：仅限制调用者等待，不给后台销毁设置强制中止期限。
- `connection.py:98–106`、`session.py:36–58`：业务异常及查询异常使连接不可继续使用。

`pending` 表示操作任务是否仍被登记；它不是连接健康证明。`pending` 非空甚至可能暂时包含已完成、但回调尚未移除的任务。不能从“没有在运行的任务”推导出“协议状态可靠”。

## 2. 为什么连续修复仍出现问题

### 同一标记承担了不同含义

`usable` 一方面表示异常后的连接不可复用，另一方面又在 `ConnectionPool.close():303` 被统一改为 False，用来使租出的 Session 失效。

因此 `_destroy()` 单看 `usable`，无法区分“健康空闲连接随池退出”和“故障连接”。单看 `pending`，又无法区分“健康空闲”和“失败但任务已经退出”。

| 修订 | 解决的情况 | 留下或引入的情况 |
|---|---|---|
| `94fcc1326` | 不可用连接使用 shutdown；保留迟到任务与槽位 | pool.close 把健康连接也标为不可用，正常退出不走 close |
| `3fb4328ca` | 正常 close 抛错时补 shutdown 兜底 | 池退出仍无法进入正常 close 分支 |
| `5d6cdacea` | 用 pending 识别正在操作的连接，健康空闲关闭能进入 close | 失败且任务已完成的连接也进入 close，退化为本次 P1 |

修复一直在替换一个条件，没有同时固定健康状态、租约状态和池生命周期的关系。迁移设计第 8 节已要求“不确定状态优先 shutdown”，当前实现偏离了这一约束。

### 测试模拟抹平了驱动差异

`tests/unit/database/test_connection.py:81–85` 中，默认 fake 的 `shutdown()` 和 `close()` 都只是立即执行 `closed=True`。走错分支时，两者仍产生相同结果。

`test_sql_failure_preserves_error_and_discards` 等测试检查“已关闭、下一次可用”，但没有让普通 close 阻塞，因此无法证明失败连接无需再次进行网络协议交互。

### 原有回归的触发条件被改变

`5d6cdacea` 在 `test_shutdown_failure_quarantines_slot_and_refuses_reconnect` 中新增了 `conn.close` 抛错设置（当前第 439 行）。

原测试只需“ping 失败、shutdown 失败”即可验证强制关闭与隔离。现在还人为要求 close 失败，错误实现就能先进入 close，再靠异常兜底进入 shutdown，使测试保持通过。测试已不能锁住“坏连接直接 shutdown”这一契约。

close 失败兜底应有独立测试；不可把它变成故障连接强制关闭测试的前置条件。健康空闲关闭的测试也必须与故障连接测试同时保留。

## 3. 实测证据

### 现有测试

使用项目虚拟环境运行：

```bash
venv/bin/python -m pytest tests/unit/database -q
PYTHONPATH=/private/tmp/swe-mysql267:src venv/bin/python -m pytest tests/unit/database tests/unit/tracing -q
```

结果分别为 **39 passed** 和 **290 passed**。主虚拟环境实际安装 9.4.0；第二条命令显式使用此前已存在的隔离安装 26.7.0，与项目依赖 pin 一致，未改主虚拟环境。

### 黑盒故障注入

最大连接数设为 1；acquire/read/write 为 50ms，cleanup 为 10ms；让 close 等待一个可控 Event，shutdown 正常返回。操作完成后检查如下：

| 场景 | usable / pending | close / shutdown 次数 | 后续行为 |
|---|---|---|---|
| ping 立即失败 | False / 0 | 1 / 0 | 当前获取及后续获取超时 |
| query 立即失败 | False / 0 | 1 / 0 | 原 ValueError 保留，下一次获取超时 |
| 归还时 sql_mode 不符 | False / 0 | 1 / 0 | 原基线异常保留，下一次获取超时 |
| 查询被取消，销毁时尚有 pending | False / 0（回收后） | 0 / 2 | 容量恢复，下一次成功 |
| 健康空闲连接随池退出，close 立即完成 | False / 0 | 1 / 0 | 容量清空，可重新 connect |
| 健康空闲连接随池退出，close 阻塞 | False / 0 | 1 / 0 | close 调用有限返回；重新 connect 拒绝，直到资源回收 |

释放阻塞 Event 后，上述故障槽位均正常清空，后续操作恢复。这把故障定位到关闭等待与容量所有权，而非 Condition 丢失通知。

新增临时契约测试覆盖 ping/query/baseline 三类阻塞 close，以及“不额外注入 close 失败”的原隔离场景，结果 **4 failed**：前三项因后续获取超时失败，最后一项因未走 shutdown 隔离而失败。

临时证据文件：`/private/tmp/swe_pool_review_probe.py`、`/private/tmp/swe-pool-review-tests/test_pool_review_regressions.py`。它们未加入产品源码。复跑临时测试须限定 pytest 根目录，避免扫描整个系统临时目录：

```bash
PYTHONPATH=/private/tmp/swe-mysql267:src venv/bin/python -m pytest /private/tmp/swe-pool-review-tests/test_pool_review_regressions.py --rootdir=/private/tmp/swe-pool-review-tests --confcutdir=/private/tmp/swe-pool-review-tests -o asyncio_mode=auto -q --tb=short
```

### 真实驱动方法探针

调用 26.7.0 的真实 `ManagedMySQLConnection.close()` 和 `cmd_quit()`，只用可控 socket 替代网络，没有连接数据库。结果：

```json
{"version":"26.7.0","calls":[{"packet":[1],"write_timeout":30}],"usable":false,"pending":0,"entries":1,"next_acquire":"TimeoutError"}
```

`[1]` 是 QUIT 命令字节；在释放 socket 写入 Event 前，实际 socket 关闭方法尚未调用。该探针验证方法选择、QUIT 和写期限传递，不构成真实断网、TLS 或 TDSQL 集成验收，也不代表真实关闭一定耗时 30 秒。

依据：[26.7.0 connection.py](https://github.com/mysql/mysql-connector-python/blob/26.7.0/mysql-connector-python/lib/mysql/connector/aio/connection.py) 的 `shutdown/close/cmd_quit`，以及 [network.py](https://github.com/mysql/mysql-connector-python/blob/26.7.0/mysql-connector-python/lib/mysql/connector/aio/network.py) 的 socket 关闭实现。shutdown 避开 QUIT；底层仍有异步关闭等待，不能宣称任意异常驱动都能被立即强杀。

## 4. 修复应同时满足的状态矩阵

| 关闭前状态 | 销毁策略 | 槽位释放条件 |
|---|---|---|
| 健康、真正空闲、无 pending | graceful close；异常时 shutdown 兜底 | 确认关闭完成 |
| 不可用、无 pending | 直接 shutdown，禁止先调用 close | shutdown 成功返回 |
| 有 pending I/O 或握手 | 先 shutdown，等任务真正结束，再 shutdown 关闭迟到资源 | 所有任务结束且最终关闭确认 |
| shutdown 失败或操作仍不退出 | 保留隔离 entry 与任务引用、报告残留 | 不得提前释放 |
| 池已关闭但仍有租出的 Session | 使 Session 失效，禁止继续执行 | 与相应销毁路径一致 |

最小修复方向：

1. `_destroy()` 恢复 `pending or not entry.usable` 的强制关闭条件。
2. `pool.close()` 在清空 `_idle` 之前保存空闲集合；保留这些 entry 的既有健康标记，仍使非空闲租约失效。池 `_closed` 已阻止新 acquire；健康空闲 entry 不属于可继续使用的 Session。
3. 保留 close 异常兜底、单次取消、迟到握手二次 shutdown、未确认资源隔离及后台任务引用。

只做第 1 项会重新使正常池退出全部走 shutdown。不能直接删除所有 `entry.usable=False`：Session._check 当前不检查 pool._closed，仍持有租约的 Session 需要失效保护。

本轮在内存中加载两种候选、复跑同一组探针，未写入产品源码：

| 候选 | 故障连接恢复容量 | 健康空闲退出调用 close |
|---|---|---|
| 仅恢复健康判断 | 成功 | 失败，重新走 shutdown |
| 恢复健康判断＋保留关闭前空闲集合 | 成功 | 成功 |

这只是最小方向的对照验证，不等于候选实现已经通过完整回归或完成合入。

## 5. 不能用来绕过问题的改法

- 不要在 discard 等待到期后直接删除 `_entries`：尚未关闭的 socket 和迟到握手仍占物理资源，会突破连接上限。
- 不要把所有连接都改为普通 close：无法中止不确定协议状态的 I/O。
- 不要只依赖 close 抛错后 fallback：阻塞本身不会立即进入异常分支。
- 不要把 cleanup_timeout 解释成“销毁已完成”：当前它只是调用方等待预算。
- 不要通过修改 fake 的关闭结果来迁就分支；同时断言关闭方法选择、原异常保留和后续容量恢复。

健康连接在最后一次健康检查后仍可能失联。若要求其 graceful close 超过预算后也及时释放物理连接，需要另行明确后台关闭的升级中止策略：期限到达后先阻止/中断协议 I/O，再强制关闭，并在清理任务真正退出前保留容量。不要简单取消 close 后立刻释放槽位。本轮不将这一剩余边界包装成已修复。

## 6. 验收矩阵与审查范围

必须同时覆盖：健康空闲退出；失败且已完成的 ping/query/connect；归还基线异常；取消时任务迅速结束与拒绝取消两种情形；prepared cursor 关闭异常；正常 close 阻塞；shutdown 失败隔离；迟到握手；重复 close；后台异常消费；租出的 Session 在池关闭后不可继续执行。

每项至少观察关闭方法、容量、原始异常、下一次获取结果。故障测试要用可控 Event 与确定的进入信号，不能只靠长 sleep 或查询 `closed=True`。

本轮源码检查覆盖 `database` 全部实现文件、数据库测试、迁移设计及计划、最近两次销毁修复，并核对共享连接装配与 Tracing 所有权。影响不限于 Tracing：共享该数据库实例的 Goals、Readiness、Feedback、Workflow 等 Store 都可能受到可用容量下降影响。具体影响取决于故障连接数和请求并发，不意味着单条故障必然使整个默认十连接池停摆。

GitNexus 原索引落后一提交，按项目要求已刷新；刷新有 FTS 降级，随后 MCP transport 关闭。CLI context/impact 无法定位 `_destroy`，返回 UNKNOWN；detect-changes 还将数据库测试错误关联到 847 条、含前端刷新 token 的流程并给出 critical。该输出无法支撑真实影响范围，未当作可信风险评级；本轮依据源码与可执行探针，不能声称完成可靠的图谱影响验证。未进行 PDG/taint 验证，本次问题是资源生命周期控制，而非已证实的输入到敏感汇点缺陷。

现有 pending 保留、单次取消、最终 shutdown 和拒绝旧池未清空时重新建池等保护方向正确，应保留。没有证据支持为本次缺陷重写整个池或添加通用状态框架。优先统一上述状态矩阵，补能在错误分支下失败的测试，再完成最小修复。
