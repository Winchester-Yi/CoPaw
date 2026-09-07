# Scheduler 双状态扫表实施计划

> **For agentic workers:** 按 superpowers:executing-plans 顺序实施；仓库 AGENTS.md 将子代理任务映射为主线程顺序执行。先验证失败用例，再实现、验证和审查。

**Goal:** Scheduler 按当前派发轮次读取 Agent `status` 与 Monitor `async_status`，两者成功才完成，明确失败按现有策略重试。

**Architecture:** SWE 回执继续落库并校验派发身份，回执受理不再等同于 Intent 完成。现有调度循环先汇总当前轮次的执行结果，再回收失联任务、计算名额和派发。保持 `dispatched`，复用原 `locked_at` 超时预算，不增加接口、字段或等待状态。

**Tech Stack:** Python、FastAPI、MySQL/aiomysql、pytest；查询测试使用 SQLite 内存表执行兼容 SQL，验证数据关联及条件更新。

## 已确认边界

- SWE、Monitor、Console 生产代码不改；Monitor 现有 `async_status` 语义直接沿用。
- 主任务失败，或者主任务成功且子任务失败：本次失败；剩余次数允许时进入 pending，否则 failed。
- 主任务与子任务共用默认 7800 秒的派发超时预算；回执不得刷新 locked_at。
- 未收到主结果、子结果为空时继续 dispatched；超时回收仍释放名额。
- 当前轮次必须按 intent、batch、attempt、job、tenant 关联；旧轮次、普通执行不参与。
- 子任务失败文案为“子任务执行失败”；主成功但子结果未确认的失联文案为“获取子任务状态超时”。
- 不提交、不推送；保留现有 Console 修改。
- 部署需让 Scheduler 访问 Monitor 更新的同一执行表；本次开发库连接失败，不能宣称已经完成真实数据库联调。

## 任务 1：保存回执与最终调度结果分离

**Files:** `scheduler/src/scheduler/app/services/cron/scheduling_service.py`、`scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`、`scheduler/src/scheduler/app/routers/cron.py`；测试 `tests/unit/scheduler/test_cron_scheduling_service.py`、`tests/unit/scheduler/test_cron_dispatch_intent_service.py`。

- [x] 增加失败测试：Agent success 回执受理后不完成、不释放名额；身份校验仍拒绝错误 job/tenant/attempt，重复回执可受理。
- [x] 验证用例在现有代码失败，原因是回执直接完成 Intent。
- [x] 保留回执落库和身份校验，移除直接由回执 status 决定最终结果的路径。
- [x] 更新回执测试的受理语义，保留数据库失败返回失败的覆盖。

## 任务 2：当前轮次双状态扫描

**Files:** `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`、`scheduler/src/scheduler/app/services/cron/scheduling_service.py`；新增 `tests/unit/scheduler/test_cron_execution_reconciliation.py`。

- [x] 使用真实内存表写入当前/旧轮次、不同 batch/job/tenant、成功/失败/待定记录，先验证新增扫描方法不存在或行为不满足要求。
- [x] 增加有界扫描；只读取 dispatched 且有明确终态的当前轮次执行；复用现有次数上限、退避、条件更新和事件记录。
- [x] 双成功完成；主失败保留原错误；子失败使用固定文案；待定保持 dispatched。
- [x] 最终完成和重试时间使用扫描时间，避免 Agent end_time 早于子任务完成导致退避失效。
- [x] 验证重复执行记录/重复扫描仅转换一次，扫描后 attempt 已改变时不更新。
- [x] 将扫描安排在 stale recovery、容量门槛之前；合并刷新受影响批次后按当前循环补位。

## 任务 3：回收和领取保持一致

**Files:** `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`；测试 `tests/unit/scheduler/test_cron_execution_reconciliation.py`、`tests/unit/scheduler/test_cron_dispatch_intent_service.py`。

- [x] 写入超时但已有当前轮次终态的记录，验证现有回收会错误重新入队的失败用例。
- [x] 超时回收和领取兜底排除已有当前轮次终态的执行，交给状态扫描处理；分页扫描剩余结果不能被误回收。
- [x] 无结果保持原失联处理；Agent success 且子任务未终结时使用“获取子任务状态超时”。
- [x] 验证重试上限、旧 attempt 不阻止当前轮次超时，以及名额占满时仍能汇总和回收。

## 任务 4：文档、审查与交付验证

**Files:** `docs/adr/0010-independent-cron-scheduling-service-owns-batch-dispatch.md`、`wiki/cron/cron-batch-dispatch.md`、`analysis/playbook/location-paths.md`。

- [x] 同步回执仅受理、双状态扫描、共享超时和共享执行表的约束及排查 SQL 关联入口。
- [x] 顺序审查：需求符合性、轮次/并发安全、扫描/回收覆盖、兼容性与测试有效性；不宣称独立代理审查。
- [x] 运行定向和 Scheduler 全量测试、目标文件格式检查、git diff --check。
- [x] 检查最终修改范围，确认没有 SWE、Monitor 或数据库 schema 改动；不创建提交。

## 验证命令与预期

PowerShell：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'scheduler/src')
& .\.venv\Scripts\python.exe -m pytest tests/unit/scheduler/test_cron_execution_reconciliation.py -q
& .\.venv\Scripts\python.exe -m pytest tests/unit/scheduler -q
git diff --check
```

新用例在实现前必须因缺少行为而失败；实现后全部通过。起始基线为 80 passed。提交检查点仅核查目标 diff，不执行 commit/push。

## 实施与审查记录

- 起始 Scheduler 基线：80 passed。
- 新用例首次运行：24 failed、1 passed；失败覆盖缺少扫描、Agent success 回执提前完成、已有终态被失联回收以及子任务超时文案。
- 顺序审查一：确认回执受理与 Intent 完成分离，等待过程不刷新 locked_at。
- 顺序审查二：发现先落库后拒绝的回执可能污染扫表结果，增加失败用例后将派发身份校验提前至落库前。
- 顺序审查三：验证当前 attempt/job/tenant/batch 关联、重复结果、扫描中 attempt 或状态变化时条件更新无效；扫描分页剩余终态被回收和领取兜底保护。
- 顺序审查四：验证满并发仍先扫描、重试从扫描时间退避、次数耗尽失败、共享超时文案、文档及修改范围。以上为主线程顺序审查，不是独立代理审查。
- 定向及 Scheduler 全量测试通过，最终全量为 121 passed；目标 Python 文件 flake8、差异空白检查通过。
- GitNexus detect-changes 已运行，报告 low；输出包含用户原有 Console 修改，目标源码 AST 差异仅涉及已确认的 Scheduler 回执、扫描和回收方法。
- 未修改 SWE、Monitor 或数据库 schema，未提交、未推送。真实 MySQL 联调未完成，开发库连接阶段返回 2013；SQLite 用例验证查询/更新行为，不证明 MySQL 锁语义的实库执行。
- 后续按用户要求将 Scheduler 调度循环默认间隔从 30 秒改为 60 秒；状态扫描、超时回收、重试领取和常规补位共享该循环间隔。新增默认间隔测试先失败后通过，后续全量 122 passed，Scheduler 源码与测试 flake8、差异检查通过。
