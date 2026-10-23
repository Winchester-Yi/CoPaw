# SSE 分阶段指标设计与实施计划

> 执行：superpowers:executing-plans；先写失败测试，再实现和验证。用户已授权继续指标开发，明确暂不考虑压测。

**目标：** 将 SSE 延迟拆分为可解释的固定阶段，接入现有 runtime_diagnostic，保留发布、取消、配额和默认开启行为。

**方案：** 扩展现有 PerformanceMetrics，不另建指标系统，也不记录每事件日志。新增 `performance_metrics.stream_stages`，固定八个阶段，每阶段最多2048个浮点毫秒样本，累计 observed_count；快照输出 sample_count、duration_avg_ms/p95_ms/p99_ms/max_ms。无租户、路径、文本或动态标签。

| 阶段 | 起止与样本单位 |
|---|---|
| admission | offer进入到成功接受；每事件，包含容量等待，取消/拒绝不采样 |
| batch_wait | 成功接受到该批提交worker；每事件，包含聚合和前批占用，不能解释为纯定时器等待 |
| accepted_to_committed | 接受到成功公布committed；每成功事件，包含排队、写入及loop恢复；取消写入即使物理成功也不补记此样本 |
| write_queue / read_queue | 提交入口到实际线程开始；每批，包含许可等待与executor排队 |
| write_execution / read_execution | 实际线程开始到操作退出；每批，失败与取消等待中的真实完成均计入 |
| read_total | read入口到返回/异常；每批，包含worker排队/执行和loop恢复 |

累计阶段存在嵌套，分位数不可相加。计量不代表客户端收到、HTTP发送完成或物理fsync完成；原写入没有每事件fsync。所有线程计量在真实结束时记录，执行许可仍在finally释放。仅存浮点数，不保留payload引用。

方案比较：逐事件日志会放大I/O与高基数；独立追踪系统增加依赖；复用现有有界容器最小且自动进入既有诊断日志。当前采用第三种。

## 实施步骤

- [x] `tests/unit/test_performance_metrics.py`：先测试固定阶段、2048上限、累计次数、空样本与p95/p99。
- [x] `src/swe/performance_metrics.py`：增加固定阶段容器、record_stream_timing及快照字段；保持现有workers/counters不变。
- [x] `tests/unit/app/test_sse_stage_metrics.py`：真实线程阻塞验证提交前后、许可等待、失败和取消计量；确认预算归零与事件顺序。
- [x] `src/swe/app/runner/sse_replay.py`：在offer/writer和读写边界记录，close不混入读写阶段；不改批次策略或协议。
- [x] 回归 metrics、SSE、TaskTracker、diagnostic、ASGI；修改文件pre-commit、diff check。
- [x] 更新Playbook，写明日志位置、单位、样本口径以及本轮未做压测。

验证：新增指标测试先因缺少方法/字段失败；实现后相关十个测试文件 135 passed（19.78s）。修改文件的 AST、mypy、Black、flake8、pylint 及其余适用 pre-commit 检查均通过，`git diff --check` 通过。提交前另执行 GitNexus staged detect_changes 核对范围。

范围排除：Linux/Kubernetes压测、压测脚本扩展、批次调优、Shell、S3恢复计划复用。
