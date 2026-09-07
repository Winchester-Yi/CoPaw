# Cron 派发结果未知与重试排序计划

## 问题与边界

Scheduler 调用 SWE 时，响应超时或中断并不等于任务未被接收。当前统一回退 pending 会让已在 SWE 后台运行的任务再次执行。与此同时，重试到期后的领取仍按 due_at 排序，使同一批次原有 dispatch_order 被重试时间覆盖。

本次对传输结果进行分类：明确未受理继续重试；请求可能已送达但响应未知时，将 Intent 保持为 dispatched 并等待 execution feedback 或 stale recovery。due_at 继续作为退避门槛，门槛通过后的候选 Batch、Intent 领取和领取结果派发均只按 dispatch_order、id 排序。

## 验收要求

- SWE ReadTimeout、ReadError、WriteError 或协议中断不回退 pending，记录结果未知事件并视为已占用 worker。
- 连接建立失败、连接超时、连接池超时和非 2xx 响应仍进入现有 retry。
- 结果未知状态能被后续同 attempt execution feedback 正常推进。
- 结果未知长期没有 feedback 时，stale recovery 会释放 worker 槽：有剩余 attempt 的重新入队，达到上限的失败。
- due_at 退避门槛保持不变。
- 候选 Batch、Batch 内 Intent 与领取后的派发顺序不再使用 due_at。
- 不引入数据库 schema 变更。

## 实施单元

### U1. 分类 SWE 回调结果并保留未知派发

**Files:**

- `scheduler/src/scheduler/app/services/cron/scheduling_service.py`
- `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`
- `tests/unit/scheduler/test_cron_scheduling_service.py`

**Test scenarios:**

- ReadTimeout 将 Intent 标记为 dispatched/unknown，不调用 fail_intent。
- 明确连接失败和 HTTP 拒绝仍调用 fail_intent。
- unknown 事件包含异常类型和 dispatch attempt，避免空错误。
- stale unknown 不会因占满 worker 容量而永久阻塞自身恢复。
- 正常 2xx 路径保持原行为。

### U2. 将 due_at 限定为准入条件

**Files:**

- `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`
- `tests/unit/scheduler/test_cron_dispatch_intent_service.py`

**Test scenarios:**

- due_at 未到的 Intent 不可领取。
- 已到期 Intent 的候选 Batch 顺序只按最小 dispatch_order、id。
- 同一 Batch 的领取及派发顺序只按 dispatch_order、id。

### U3. 同步既有架构决策

**Files:**

- `docs/adr/0010-independent-cron-scheduling-service-owns-batch-dispatch.md`

**Approach:** 记录明确失败与结果未知的分流，以及 due_at 作为准入门槛而非优先级的约束。

## 验证

- Scheduler 调度与 Intent 服务定向 pytest。
- Scheduler 全量单元测试。
- Python 编译、差异检查，并确认 schema 未变化。
