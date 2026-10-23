# 内存存储运行指标补齐

状态：实现完成，本地验收及独立审查通过。范围沿用第二阶段已批准设计§7，本轮用户已明确要求补齐运行指标；不修改持久化格式或启用开关，不部署、不迁移生产数据。

## 方案和口径

复用进程内 `PerformanceMetrics` 及已有 RuntimeDiagnosticManager 的 `performance_metrics` 输出。与新增监控服务或按会话维护实时gauge相比，这一方案不引入外部依赖、scope索引或新的常驻对象持有链。旧workers/counters/stream_stages保持兼容，新增session_memory和storage_operations。

成功发布session v3根后，对提交后的热turn做一次观测，仅访问热dict，不扫描冷记录：

| 字段 | 含义 |
| --- | --- |
| session_hot_terminal_count/bytes | 热状态中completed/stopped/failed/cancelled的条数与UTF-8 JSON字节 |
| session_protected_count/bytes | 所有非终态，以及相对此前revision热快照新增/修改（含本次冷回填）、尚不可冷移出的终态 |

保护终态同时计入两个指标，因此两组不可相加。bytes复用冷存储预算的单项`{turn_id: turn}`编码口径，包含键和元数据，不表示Python容器或RSS。各指标保存最近2048次提交的last/avg/p95/max及sample_count、累计observed_count，不按session去重，不表示全量NAS会话或进程实时驻留总量。新提交根fsync确认失败、但读回nonce能确认发布时仍观测该根；根发布前失败不记录热状态。

storage_operations固定8项：session_commit、checkpoint_commit、session_recovery、checkpoint_recovery、session_migration及checkpoint_migration，另有两项migration_preview。每项保存最多2048个耗时数值，输出avg/p95/p99/max毫秒、sample_count及累计attempted/succeeded/failed/cancelled_count（attempted只计已结束尝试，不含在途操作）。counters增加相应`<operation>_failures`累计数；取消单列，不并入failure。

session_commit覆盖真实线程内ledger事务，含恢复检查、冷文件、根发布及收尾，排除排队和锁等待；checkpoint_commit覆盖initialize/append/publish/reset调用，条件重试无新根也计一次成功调用。迁移内部真实提交同样计入commit指标，评估在线延迟时应等迁移结束后的样本覆盖。session_recovery为写前恢复检查（含无pending）；checkpoint_recovery为追加前已提交active前缀验证和未提交尾部截断。嵌套耗时重叠，不可相加。

同步计时在工作线程实际执行时开始；异步迁移计时覆盖await到完成，异常/cancel后也记录。通过functools.wraps保持原函数名称，保留已有worker边界。计时使用单调时钟；所有聚合在既有threading.Lock下更新，snapshot仅复制数值后排序。指标记录异常不能掩盖业务异常或改变已提交结果，不记录正文、路径、tenant/session/msgid或异常文本。

## 验收

- API固定标签、窗口有界、并发更新不丢计数，snapshot与内部样本隔离。
- 成功/失败/取消及async实际完成计时；指标故障不影响业务。
- UTF-8字节与保护状态准确，失败根发布不增加状态观测；不读取冷正文。
- session/checkpoint恢复损坏、真实迁移失败与preview失败分别计数；既有故障/取消/并发回归不变。
- RuntimeDiagnosticManager既有输出自动含新增指标，专项pytest与改动文件pre-commit通过。


## 使用与限制

RuntimeDiagnosticManager的既有`diagnostic_flow`数据中读取：

- `performance_metrics.session_memory.session_hot_terminal_count.last`：最近一次已发布v3 session的热终态条数；同级`avg/p95/max`是最近2048次观测的分布。
- `performance_metrics.session_memory.session_hot_terminal_bytes`及`session_protected_count/bytes`同理；观察`observed_count`和`sample_count`判断是否已产生数据。
- `performance_metrics.storage_operations.session_commit.duration_p95_ms`及`checkpoint_commit.duration_p95_ms`：实际存储执行耗时，不含工作线程排队；原有workers指标继续提供queue/execution耗时。
- `performance_metrics.counters.session_recovery_failures`、`checkpoint_recovery_failures`、`session_migration_failures`、`checkpoint_migration_failures`：进程启动以来失败数。preview有独立`*_migration_preview_failures`，不会混进实际迁移失败率。

同一进程的维护迁移也产生真实commit样本；迁移结束后再等待样本覆盖以评估在线延迟。新格式默认关闭，v3热状态及v2提交指标仅在对应格式实际执行时更新。进程重启清零，SIGKILL不保证记录最后一次操作，不能替代持久化审计。last不是当前所有会话的gauge，热终态和protected存在交集，不应求和解释为RSS。

独立审查复跑最初25项指标测试通过，并以tracemalloc填满新增窗口，观测新增常驻约955200 bytes（0.91 MiB，仅该本地样本，不是跨环境硬上限）。已补线程提交外部取消专用回归：底层提交完成后记录真实耗时，结果仍为存储成功。采集只遍历本次hot，无新增冷目录扫描、后台任务或外部依赖。


## 本轮验证记录

- 初始基线：66 passed。新增聚合/装饰器测试6项先RED后GREEN；实际埋点测试6项先RED后GREEN。
- 存储、session事务、checkpoint、诊断及CLI联合回归：214 passed（12.82s）。SSE/S3指标消费者兼容回归：40 passed（1.19s）。
- 改动文件pre-commit的AST、mypy、Black、flake8、pylint及基础格式检查通过；diff check通过。
- 独立审查确认无明确缺陷；补齐其提出的同步worker外部取消专用回归并通过。无生产NAS/RSS或线上采样结果，不据本地单元测试承诺生产性能收益。
- GitNexus impact对共享类/部分checkpoint入口提示HIGH，已在编辑前告知；局部方法含UNKNOWN/静态调用缺口，源码复核直接worker/CLI消费者。最终detect_changes为MEDIUM，4条受影响流程均在append/校验链，未扩展到新业务流程。
