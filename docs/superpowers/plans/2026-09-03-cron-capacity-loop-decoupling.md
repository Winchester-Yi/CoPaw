# Cron 容量调整循环解耦计划

## 问题与边界

当前生产 `run_loop()` 先串行派发 Intent，再执行容量调整，导致派发量大或回调缓慢时，300 秒容量调整下限被放大为几十分钟甚至数小时。本次恢复 ADR-0010 已定义的 separate loops：派发继续每 30 秒扫描，容量调整独立每 60 秒检查。同时把调整间隔判断移到 scope lease 之前，避免未到期时产生周期写锁。

本次不改变派发异常状态语义，也不改变 Intent 的 `due_at` 排序；这两个状态机提议仅完成分析，等待单独确认。

## 验收要求

- 派发调用阻塞时，容量调整循环仍能独立运行。
- 派发与容量循环的异常互相隔离，关闭事件可以结束两个循环。
- 容量循环默认每 60 秒检查，既有 300 秒策略间隔继续作为实际调整下限。
- 未到调整间隔时不获取 scope lease。
- 到期后先获取 lease，再重新读取最近容量记录并复核，防止多实例重复写入。
- `run_scheduler_once()` 保留现有组合语义，避免破坏测试和可能的维护调用。

## 实施单元

### U1. 用测试固定独立循环和低成本间隔门禁

**Files:**

- `tests/unit/scheduler/test_cron_scheduling_service.py`

**Test scenarios:**

- `dispatch_ready_once()` 阻塞时，容量循环仍立即进入检查。
- 最近容量记录未满 300 秒时，不调用 `acquire_scope_lease()`。
- 到期后获得 lease，再次读取 latest；如果另一实例已写入，则跳过反馈统计和容量写入。
- stop event 能让两个循环正常结束。

### U2. 解耦生产循环并调整 lease 顺序

**Files:**

- `scheduler/src/scheduler/config/constant.py`
- `scheduler/src/scheduler/app/services/cron/scheduling_service.py`

**Approach:** 增加固定的 60 秒容量检查默认值；`run_loop()` 并行运行派发循环和容量循环，各自处理异常与停止等待。调整路径先读取策略和 latest 判断是否到期，到期后再抢 lease并二次读取复核。

## 验证

- Scheduler 调度服务定向 pytest。
- Scheduler app 生命周期测试。
- Python 编译、格式、静态检查和 `git diff --check`。
- 最终差异不得包含派发失败状态和 Intent 排序变化。
