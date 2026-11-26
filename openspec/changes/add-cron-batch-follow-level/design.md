## Context

### 已有的关注链路

关注目前只是父任务状态行上的布尔，且只影响两条旁路：当日失败补跑的范围筛选、招乎批调度简报的批次筛选。派发链路完全没读它。

```
Console ──PUT /batches/{id}/follow──▶ swe_cron_job_states.batch_followed
                                            │
                        ┌───────────────────┼───────────────────┐
                        ▼                   ▼                   ▼
                 当日失败补跑过滤器      批调度简报过滤器     ★ 派发排序（未接入）
```

### 派发链路的现状

- 派发主循环在 `scheduler/src/scheduler/app/services/cron/managed/recovery.py`：每 2 秒一轮，按 scope 轮转（`source_id + provider_id + model_id`）。
- 每个 scope 取最多 `limit`（默认 32）条候选，按 `managed/work_query.py` 的 `_batch_order()` 排序后逐条尝试派发，容量不够时交由既有 `next_dispatch_at` 退避。
- 现有排序键：`dispatch_lane='urgent'` → `batch_created_at` → `batch_queue_order` → `dispatch_order` → `queue_order`。
- `dispatch_lane='urgent'` 是重跑/补跑的既有加急通道（`managed/retry_priority.py` 判定，服务端写入）。
- 批次顺序键全部来自入队时冻结的 `dispatch_meta_json`（`managed/scheduling.py`），刻意避免在排序里做关联。
- 容量按 `(source_id, provider_id, model_id)` 记账并加 scope 租约（`services/cron/capacity_claim.py`），派发无抢占。

## Decisions

1. **等级挂在父任务上**。字段放在 `swe_cron_job_states`，语义与现有 `batch_followed` 一致且互斥：同一父任务的所有批次共用一个等级。批次级单独调级不在本次范围。
2. **排序位置选 ①**：`urgent` 重跑通道保持最优先，其后才按等级降序。理由是重跑是故障恢复、量小且有时效性；若让高等级新批压过重跑，一次失败批次的补跑会被持续到来的高等级批次饿死，反而放大故障。
3. **接受饥饿**。不实现老化、不实现分级配额。等级是运营排序手段，不是可用性保证；这条要写进文档，避免被当成 SLA。
4. **派发候选范围**。共享 Worker 的批次在各自模型池比较等级，文本批次在直派候选列表内比较等级。
5. **默认 0（未关注）**。新建与存量父任务一律 0，不设隐式默认等级；只有显式设置才参与优先。

## Data model

关注布尔列尚未部署（仍是本地未提交改动），因此用它替换而不是叠加，避免两个真相源：

```sql
-- scripts/sql/cron_batch_follow.sql（改写现有未部署脚本）
ALTER TABLE swe_cron_job_states
  ADD COLUMN batch_follow_level TINYINT NOT NULL DEFAULT 0
    COMMENT '父任务关注等级：0未关注，1-5越高越优先',
  ADD COLUMN batch_follow_updated_at DATETIME(6) NULL COMMENT '等级最近切换时间，UTC',
  ADD COLUMN batch_follow_updated_by VARCHAR(128) NULL COMMENT '最近切换等级的操作人';
```

- 唯一约束：`0 <= level <= 5`，由写入接口严格校验（`int` 类型，拒绝布尔与字符串）。
- 若已在某环境用布尔版建过表，迁移脚本按 `batch_followed=1 → 3` 映射一次，随后删除旧列，避免长期双写。
- 关注语义统一定义为 `level >= 1`：补跑范围、简报范围都改用它。

## Dispatch ordering

在 `_batch_order()` 里插入一个键（`level` 从 `dispatch_meta_json` 读取，与 `batch_created_at` 同源）：

```
ORDER BY
  CASE WHEN r.dispatch_lane='urgent' THEN 0 ELSE 1 END,   -- ① 不变：重跑优先
  CAST(JSON_UNQUOTE(JSON_EXTRACT(r.dispatch_meta_json,'$.batch_follow_level')) AS UNSIGNED) DESC,  -- ② 新增
  {batch_created_at},                                     -- ③ 原有
  {batch_queue_order},                                    -- ④ 原有
  r.dispatch_order,                                       -- ⑤ 原有
  r.queue_order                                           -- ⑥ 原有
```

- 未设置等级（`NULL` 或缺失）按 0 处理，排在关注批次之后。
- 共享 Worker 的批次按模型池轮转，等级在同一 `(source_id, provider_id, model_id)` 内比较。
- 文本批次走 `recovery.py` 的直派查询，直接在直派候选列表内排序，无需按模型分池。独立直派执行排在文本批次前。
- 排序决定符合派发条件的候选先后。容量核算与退避决定候选何时可以派发。

## Operations

恢复巡检的单轮工作量上限由环境变量 `SCHEDULER_MANAGED_RECOVERY_LIMIT` 控制，默认 `32`，取值 `1`–`200`：

- 每个模型池和直派队列每轮各最多取这么多条派发候选，广播、删除、暂停也使用该值作为分页上限。 [派发查询](../../../scheduler/src/scheduler/app/services/cron/managed/work_query.py)
- 当日失败补跑的每页上限取该值与 `100` 中的较小值。例如配置为 `150` 时每页最多扫描 `100` 条。 [补跑分页](../../../scheduler/src/scheduler/app/services/cron/managed/recovery.py)
- 有效 Worker 大于 32 的模型池需要多轮才能填满，可用该变量把单轮工作量放大到不小于池容量。
- 缺失或非法值回退 `32` 并记录一条告警，不阻断启动；校验上限仍是 `200`。
- 巡检间隔由 `SCHEDULER_MANAGED_RECOVERY_INTERVAL_SECONDS` 控制，默认 `2` 秒、取值 `1`–`60`；实际周期为本轮耗时加该值，缺失或非法值回退并告警。

## Board filtering

批次清单的关注等级筛选在看板查询接口上完成，避免只过滤当前页：

- 查询参数：`follow_min` / `follow_max`，取值 `0`–`5`，可单独或同时提供，同时提供时取区间。
- SQL：在 `batch_where` 里对父任务有效等级做区间比较，表达式与读模型投影一致——父任务已删除或非 Scheduler 管理时该值为 `NULL`，因此这类批次不进入任何等级筛选结果。
- 概览统计、列表和失败明细使用同一关注等级区间，语义与现有的状态、成员、关键字筛选一致。
- Console 选项映射：`已关注 → follow_min=1`、`未关注 → follow_max=0`、`关注 Ln → follow_min=Ln & follow_max=Ln`、`全部 → 不带参数`。

## Level change propagation

改级必须对**已经排队**的成员生效，因此写入路径要做两件事：

1. 更新父任务状态行的等级与审计字段（沿用现有 `batch_follow.py` 的锁顺序：先锁定义行，再重读回执、锁状态行）。
2. 在同一事务内刷新该父任务下仍在排队成员的执行元数据：

```sql
UPDATE swe_cron_execution_runs r
  JOIN swe_cron_managed_batch_members m
    ON m.current_execution_id=r.execution_id AND m.source_id=r.source_id
  JOIN swe_cron_managed_trigger_receipts c
    ON c.batch_id=m.batch_id AND c.source_id=m.source_id AND c.status='accepted'
  JOIN swe_cron_job_states s ON <父任务完整身份>
SET r.dispatch_meta_json = JSON_SET(COALESCE(r.dispatch_meta_json,'{}'), '$.batch_follow_level', %s)
WHERE s 身份 = 父任务 AND r.status IN ('queued','dispatch_failed');
```

- 只刷未派发成员；已派发/运行中的执行不重新排序（也符合「不抢占」）。
- 影响行数受一个父任务在排队中的成员数限制，属于管理操作，可接受；写入失败时整笔事务回滚，等级与排队快照不会分叉。
- 新触发批次在 `scheduling.py` 生成 `dispatch_meta` 时直接写入当时等级。

## Rerun inheritance

当日失败补跑、人工批次重跑和自动重跑在入队事务内按批次回执的父任务身份读取当前等级，写入 `dispatch_meta_json.batch_follow_level`。人工传输重试和派发未确认后的自动回队都复用原执行，在持有父任务锁的事务内刷新等级。重跑沿用原有 `batch_created_at` 和 `batch_queue_order`。

新触发批次由 `managed/scheduling.py` 读取父任务状态，`managed/executions.py` 透传由调用方构造的 `dispatch_meta`。

## Rejected alternatives

| 方案 | 为什么不用 |
|---|---|
| 排序时动态关联父任务状态行取等级 | 每 2 秒每 scope 的热查询多三层关联，排序表达式带子查询；性能风险大且可避免 |
| 只做入队快照、改级不刷新 | 进行中批次改级无效，与「当前有更高等级就优先」直接冲突 |
| 等级映射到 `dispatch_lane`（`urgent`/`normal` → 多个 lane） | 只有一个 lane 键可用，塞 5 档会破坏既有重跑加急语义；整数键更简单 |
| 用等级参与 worker 容量分配（抢占/预留） | 现有模型无抢占，改动量远大于本需求；饥饿已明确接受 |

## Failure modes

- **等级写入成功、刷新失败**：同一事务内完成，不会出现。
- **旧版 Scheduler 派发**：不识别该键，按原顺序派发——降级表现为「等级暂不生效」，不影响正确性。
- **旧版 Console 读新 Monitor**：`batch_follow_level` 是整数，旧前端按布尔渲染会显示异常，因此 Console 与 Monitor 需同批上线。
- **`dispatch_meta_json` 非法 JSON**：沿用现有 `JSON_EXTRACT` 宽容行为，取不到等级即视为 0。
- **等级被设为 0**：等价于取消关注，同时从补跑与简报范围中移出。

## Verification

- 排序：在 sqlite 查询测试中构造同 scope 下 L5/L3/L0 的排队成员与一个 urgent 重跑，断言派发顺序为 `urgent → L5 → L3 → L0`。
- 传播：改级后断言已排队成员 `dispatch_meta` 中的等级被刷新，已派发成员不变；事务失败时等级与快照都不变。
- 重跑：先变更失败批次的父任务等级，再验证三条重跑路径按当前等级入队。
- 读模型：Monitor 投影返回整数等级；简报与补跑过滤按 `>=1` 命中。
- 隔离 MySQL 集成测试覆盖真实 SQL（`JSON_SET` 与排序表达式）。
- Console：批次行右键菜单设置等级、任务名下方星标展示、旧响应（字段缺失）不崩。
- 上线顺序：DDL → Scheduler → Monitor → Console；回退时保留列，旧 Scheduler 忽略新键。
