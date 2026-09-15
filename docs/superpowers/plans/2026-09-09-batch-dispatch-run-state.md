# Batch Dispatch Run State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Repository AGENTS.md maps subagent work to sequential work in the main thread; do not present self-review as independent agent review.

**Goal:** 增加独立的批调度暂停／恢复，保守迁移旧批调度父任务，明确结算关闭任务，同时保持普通任务行为不变。

**Architecture:** 整批运行状态由 Scheduler 在独立控制表中持久化，不写回会被 SWE／Monitor 定义同步覆盖的任务元数据。批次创建、领取及交接前均检查该状态；SWE 对关闭任务返回明确的跳过结果。Monitor 只读投影，Console 通过 SWE 的管理员接口操作 Scheduler。

**Tech Stack:** Python、FastAPI、现有异步 MySQL 连接层、pytest；React、TypeScript、现有 Ant Design 组件、Vitest。

**Spec:** `docs/brainstorms/2026-09-09-batch-dispatch-run-state-requirements.md`

## Global Constraints

- 首先根据已保存的调度模式与父子关系判断归属。普通任务不初始化整批状态，不改变其开关、普通定时器或运行方式。
- 批调度子任务没有独立的整批开关，不从子任务的启停状态推导整批状态。
- 已有批调度父任务缺少新状态时，首次保守继承：父任务开启则批调度运行，父任务关闭则批调度暂停，不区分手动或未读自动关闭。
- 该初始化必须持久化且幂等；已有明确状态不得被覆盖。
- 已运行任务自然收尾；暂停本身不是执行失败，不占用新的 Worker 名额。
- 暂停期间错过的定时时点不自动补跑。
- 不改变跨批次优先级、模型 Worker 调整策略或普通任务调度模式。
- 不提交、不推送、不执行生产迁移、不重新打包；这些操作需要用户另行要求。
- 保留现有暂存和未暂存修改。开发留在当前工作区，不分离已经互相依赖的本地改动。
- 每个待修改符号先执行 GitNexus upstream impact；新模块没有索引时用源码列出调用方。所有测试使用项目 `.venv`；Windows `fcntl` 阻塞时使用已有 Linux 环境或如实报告，不能用未验证的桩冒充兼容。

---

## Source-grounded decisions

1. `CronManager.is_batch_dispatch_parent` 的现有运行判断受环境开关影响。兼容迁移使用**持久化模式与父子关系**，不能因某实例暂时关闭 runtime 而把批调度任务误判为普通任务。迁移不通过遍历所有租户工作区初始化来实现。
2. `CronManager._sync_batch_dispatch_scheduler_job` 目前按 `spec.enabled` 暂停物理批定时器；`_fetch_parent_job_for_callback` 同时要求父定义 `enabled=1/status=active`。两处必须一起解耦。
3. 任务元数据经过 `_preserve_batch_dispatch_meta_on_save` 和 Monitor 全量定义同步。独立控制表避免旧客户端或延迟定义同步覆盖新状态，不引入 SWE 与 Scheduler 的 Python 包依赖。
4. 现有领取事务已经持有模型 scope lease 行，再锁 batch、intent。新增控制行必须有统一锁顺序；不能只在事务外检查一个布尔值。
5. `run_job` 对关闭任务直接返回 `None`，HTTP callback 仍可返回成功。新协议只为明确拒绝执行的关闭任务返回 `skipped=job_disabled`，普通成功和历史 2xx 响应保留兼容。
6. Scheduler `_app.py` 的 lifespan 目前只初始化连接，并未自动调用 `init_database_tables`。迁移必须有独立入口，运行循环和回调需检查所需 schema 是否就绪，不能等待第一个派发 SQL 才报缺表。
7. 预检 GitNexus：`run_job` 为 LOW，1 个直接调用方；`_claim_due_intent_ids` 为 LOW，1 个直接调用方；`_sync_batch_dispatch_scheduler_job` 为 LOW，3 个直接调用方、共 18 个上游节点。图索引只覆盖已索引代码，整体交付仍按跨服务并发变更验证。

## Persistence and API contracts

### Scheduler-owned control

新增 `swe_cron_dispatch_controls`，主键 `(source_id, tenant_id, parent_job_id)`。只为有效、未删除的批调度父任务插入；父任务 `paused` 仍是有效定义。不得仅凭历史 batch、`enabled` 或存在广播关系插入。

```sql
CREATE TABLE IF NOT EXISTS swe_cron_dispatch_controls (
    source_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    parent_job_id VARCHAR(64) NOT NULL,
    paused TINYINT(1) NOT NULL,
    version BIGINT NOT NULL DEFAULT 1,
    resumed_at DATETIME(6) DEFAULT NULL,
    updated_by VARCHAR(64) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id, tenant_id, parent_job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- 初始化用唯一键冲突不更新的插入，并记录 `batch_dispatch_initialized` 事件；已有值不重新读取父任务 `enabled` 推导。
- 使用当前定义的完整身份，不接受客户端伪造 tenant/source。缺失身份、损坏元数据或归属不确定的旧行进入迁移报告，不默认启用或复制到多个来源。
- 新建、显式切入批调度而无控制行时同样建立明确状态，初始值保守取该父任务自身开关；已有控制行在模式切换后保留。控制行存在不能代替当前批调度模式校验。
- 退出批调度模式不删除历史控制状态，也不以“暂停”替代现有普通模式切换流程。旧模式批次的未交接意图不能继续以批调度身份执行，明确以 `dispatch_mode_disabled` 跳过；普通任务定义和普通定时器行为不受此结算影响。
- 时间沿用现有 DB 的北京时间 naive datetime；HTTP 返回带时区 ISO 时间。`resumed_at` 只在暂停→运行时更新，重复恢复不能移动时间边界。
- 复用 `swe_cron_dispatch_events` 记录控制变更：`batch_id=''`、`intent_id=NULL`，携带准确 parent/job/tenant/source、actor、version 和前后状态。不写凭据。

### Additional existing-table fields

- `swe_cron_dispatch_intents.claim_token VARCHAR(36) NOT NULL DEFAULT ''`：每次领取生成新的 UUID，所有读取和交接检查必须验证 token，避免退回后同一 worker、同一 attempt 的旧协程误用新领取。
- `swe_cron_dispatch_batches.skipped_count INT NOT NULL DEFAULT 0`：明确跳过计数，不能算入 completed 或 failed。Scheduler 与 Monitor 的共享表定义保持一致。
- 意图终态采用 `skipped`。历史 `cancelled` 保留原状态，在新增 skipped_count 中统一统计为“跳过／取消”，详情仍区分两种原因。批次结果状态保留 existing received/pending/running/completed/failed；所有意图终结且无失败时 batch completed，成功数与跳过／取消数分别展示。整批运行状态是单独投影，不将历史 completed batch 改写为 paused。

### Management APIs

SWE：`GET/PUT /api/cron/jobs/{job_id}/batch-dispatch/run-state`。

Scheduler 内部：`GET/PUT /api/scheduler/cron/dispatch/parents/{parent_job_id}/run-state?tenant_id=...`。

```json
{"paused": true, "expected_version": 3}
```

成功返回完整身份、`paused`、`version`、`resumed_at`、`updated_at`，以及该父任务所有批次的 `claimed_count` 和 `inflight_count`。来源从认证请求解析，tenant 由 SWE 已校验的父任务定义确定，不能把操作人的 user_id 当作父任务 tenant。

- SWE 复用 `manager_identity`、源父任务校验和 `_claim_dispatch_mode_operation`；仅 manager/admin 可操作。Scheduler 复用内部 token 校验，再按精确 source/tenant/parent 查定义。
- 普通任务、子任务返回明确的非适用错误且不插入控制行；删除或无权访问的父任务不泄露跨来源信息。
- `expected_version` 冲突返回 409 和刷新提示；相同请求在状态已相同且 version 恰为旧 version+1 时允许返回当前结果，不重复写审计或移动 resumed_at。其余过期版本不静默覆盖。
- GET 只读；控制行尚未初始化时返回明确的初始化未就绪错误，不能在前端从 enabled 假装推导成功。模式开启流程与初始化入口负责保证可读。
- PUT 在事务提交后才成功；Scheduler 不可达返回 502/503，不本地“乐观保存”开关。操作不触发广播或全租户模式同步。

## Concurrency and handoff contract

### Lock order

领取：`model scope lease → control → batch → intent`。

暂停、人工重试、交接检查：`control → batch → intent`；释放占用不反向获取 scope lease。结果结算保持现有短事务，在持有 intent 锁的事务提交后再刷新 batch，不能反向请求 control。

候选选择在应用 LIMIT 前排除暂停控制行，避免前 N 个暂停批次挡住其他运行批次。候选控制行使用非等待锁或逐候选短事务，不能在两个模型池中按相反顺序等待多个 control 行。所有读取须在正确锁之后取最新状态，不能使用更早的 REPEATABLE READ 快照授权派发。

### States

```text
pending → claimed(token, attempt+1) → acknowledged → dispatched → existing settlement
              │                           │
              ├─ batch paused → pending   └─ callback says job_disabled → skipped
              │  refund unstarted claim
              └─ own job disabled → skipped
```

- `acknowledged` 明确定义为“已提交本次交接许可”，不是执行成功。在控制行锁内从当前 claimed/token 转换，再在事务外发送 HTTP。暂停与该许可线性排序。
- 暂停返回以后不会再发放新许可；暂停之前已获许可的回调可能仍在路上或刚被 SWE 接收。UI 提示“已暂停后续调度；交接中及运行中任务自然收尾”，不能承诺分布式瞬间撤回已发放的执行许可。
- 暂停事务按 batch_id、intent id 的稳定顺序退回带新 token 的 claimed 行，清空 token/锁并返还这次未开始的领取次数。CAS 同时检查 token、owner、attempt、status；旧协程不能交接退回后的行。等待恢复不增加 max_attempts。
- legacy 空 token 的 claimed/acknowledged 不能证明未交接，不做次数返还或立即重跑，按已有执行反馈与超时规则收敛。迁移不可批量把历史在途行改成 pending。
- 新 token 的 claimed 过期且从未 acknowledged：安全退回并返还领取次数；acknowledged 使用交接后不确定结果的回收规则，不走“未启动”回收分支。
- 校验关闭的自身任务发生在交接前；SWE 本地再次检查是抵御定义同步延迟的最后一道检查。控制状态读取失败时不派发、不伪造成功，也不作为模型错误样本。
- 暂停不阻止已运行任务结果结算及合理的重试入队，只阻止后续领取。人工重试仍需原有授权与修复确认，入队反馈应明确“批调度暂停，等待恢复”。
- 暂停先于单任务跳过检查：暂停期间未领取的行保持等待；恢复后再依据自身状态判断是否跳过。终结后的 skipped 不被恢复操作重新入队。

### New-batch admission

父定义允许 enabled=false/status=paused，但仍校验当前 batch mode、未删除、准确身份。控制行锁内验证 paused，再原子写 batch 与初始 intents；暂停回调返回显式 skipped 原因，不创建空批次或失败记录。

物理批定时器在 batch mode 下独立于 `spec.enabled` 保持注册并启用，作为唤醒源；整批暂停由 Scheduler 的持久门控生效，不依赖跨服务暂停物理定时器的双写。普通定时器仍按现有 batch mode 规则暂停，退出模式时仍走原恢复流程。

恢复不扫描补建漏过的时点。对于迟到的旧物理回调，用实际计划触发时间（业务 scheduled_fire_at 减去已保存的 batch offset）与 `resumed_at` 比较：早于恢复时刻的回调不创建新 batch；已存在 batch 的队列不受该时间过滤。采用已保存 offset 和现有 callback 时间校验，不能信任任意输入覆盖偏移量。重复父回调不能覆盖 intent 或重复增加总数。

---

## Task 1: Classification, durable initialization and schema readiness

**Files:**
- Create: `scheduler/src/scheduler/app/services/cron/batch_run_state.py`
- Modify: `scheduler/src/scheduler/app/database/schema.py`
- Modify: `monitor/src/monitor/app/database/schema.py`
- Modify: `scheduler/src/scheduler/app/_app.py`
- Create: `scheduler/src/scheduler/migrate_batch_run_state.py`
- Create: `tests/unit/scheduler/test_batch_run_state.py`

**Interfaces:** `is_batch_parent_definition(row: Mapping[str, Any]) -> bool`; `initial_paused(row: Mapping[str, Any]) -> bool`; `initialize_missing_controls(db) -> dict[str, int]`; `assert_run_state_schema_ready(db) -> None`。迁移入口提供 `--dry-run` 和 `--apply`，默认只报告，不改数据。

- [x] 写失败测试：普通任务、普通广播父/子、批调度子、批调度父、已删除、暂停父、损坏元数据。测试不依赖本进程 runtime 环境变量。

```python
def test_normal_job_is_not_a_batch_parent():
    row = {"id": "p", "enabled": 1, "status": "active", "meta": {}}
    assert is_batch_parent_definition(row) is False

def test_disabled_batch_parent_is_still_a_batch_parent():
    row = {"id": "p", "enabled": 0, "status": "paused",
           "meta": {"broadcast_dispatch_intents_enabled": True}}
    assert is_batch_parent_definition(row) is True
    assert initial_paused(row) is True
```

- [x] 执行 `$env:PYTHONPATH='scheduler/src'; .\.venv\Scripts\python.exe -m pytest tests/unit/scheduler/test_batch_run_state.py -q`；预期新 helper 未实现而失败。
- [x] 实现归属解析：meta 支持 dict 和 JSON 文本；模式接受 boolean true、数字 1 和明确字符串 true/1，false/0 不是开启；未知字符串进入诊断报告。父身份由非空 broadcast_source_job_id 排除。有效定义限 active/paused 且 deleted_at 为空。
- [x] schema 按本文 DDL 增量创建及 ALTER，仅处理明确的重复列错误。迁移 CLI 仅执行本功能所需的新表与两项新增列，不借机执行其他历史 ALTER。添加缺 schema 时禁用新派发与父回调的 readiness 检查，不把所有服务读接口一并停掉。
- [x] 分页扫描当前定义，先归属校验再幂等插入。报表输出 scanned、eligible、initialized、existing、ignored（普通／子任务等）、invalid；迁移期间冻结任务定义写入。

```sql
INSERT INTO swe_cron_dispatch_controls
  (source_id, tenant_id, parent_job_id, paused, updated_by)
VALUES (%s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE parent_job_id = parent_job_id;
```

- [x] 迁移应用与运行时初始化调用同一实现；后者在首次新父回调、模式开启完成或候选批次初始化阶段触发，不能通过 GET 隐式写入。重复并发初始化用唯一键保持第一次持久化结果。
- [x] 增加真实持久化测试证明：初始化后更改定义 enabled，再次初始化不覆盖控制值；dry-run 不写；普通任务与子任务无记录；缺表或权限不足 readiness 明确失败。
- [x] 重跑本任务测试为绿，记录 diff/checkpoint；不执行 git commit。

## Task 2: State mutation API and lifecycle isolation

**Files:**
- Extend: `scheduler/src/scheduler/app/services/cron/batch_run_state.py`
- Create: `scheduler/src/scheduler/app/routers/batch_run_state.py`
- Modify: `scheduler/src/scheduler/app/routers/__init__.py`
- Modify: `src/swe/app/crons/batch_operations.py`
- Modify: `src/swe/app/crons/api.py`
- Modify: `src/swe/app/crons/manager.py`
- Create: `tests/unit/scheduler/test_batch_run_state_api.py`
- Modify: `tests/unit/app/test_cron_batch_operations.py`
- Modify: `tests/unit/app/test_tenant_cron_api.py`
- Modify: `tests/unit/app/test_cron_task_view.py`

**Interfaces:** `get_run_state(source: str, tenant: str, parent: str) -> dict`; `set_run_state(source: str, tenant: str, parent: str, actor: str, paused: bool, expected_version: int) -> dict`。后者原子变更与审计，并按交接契约释放未启动领取。

- [x] 增加 API 失败测试，覆盖 manager/admin、普通用户、跨 source、伪造 tenant、普通任务、批调度子、404、409、未初始化、Scheduler 超时。

```python
def test_run_state_request_forbids_extra_fields():
    with pytest.raises(ValidationError):
        RunStateUpdate(paused=True, expected_version=1, tenant_id="other")
```

`RunStateUpdate` 在 Scheduler router 定义，配置 `extra='forbid'`，paused 使用 StrictBool，expected_version 为正整数；SWE 请求模型采用同一字段契约但不 import Scheduler 包。

- [x] 执行 Scheduler 新 API 测试与 SWE `test_cron_batch_operations.py`，确认未实现接口失败。
- [x] 实现身份校验、内部 HTTP proxy、控制行 FOR UPDATE、版本比较、状态写入与事件同事务。GET 不创建记录；PUT 的 parent 必须已完成初始化，初始化失败返回可重试提示，不从请求随意创建。
- [x] 调整 `_sync_batch_dispatch_scheduler_job`：batch mode 的物理定时器不再跟随 enabled。普通 pause/resume、自动未读 pause、恢复定时器路径都须保留 parent normal timer 在 batch mode 下关闭，不能恢复普通与批两套定时器。
- [x] 新开启批调度在定义同步确认后调用初始化，完成后 UI 才能操作 run-state。修改失败沿用已有操作失败记录，禁止把失败流程报告为整批已运行。只针对源父任务同步，不重新初始化所有接收租户。
- [x] 原有完整任务保存、启停及模式切换 API 回归通过；独立控制表不参与 job 定义同步。初始化后修改 enabled 再初始化的持久化测试证明已有运行状态不被覆盖。
- [x] 重跑上述测试，检查普通任务 pause/resume 调用未改变，记录 checkpoint。

## Task 3: Atomic queue gates, reservation fencing and paused retries

**Files:**
- Modify: `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`
- Modify: `scheduler/src/scheduler/app/services/cron/scheduling_service.py`
- Modify: `scheduler/src/scheduler/app/services/cron/batch_operations.py`
- Extend: `scheduler/src/scheduler/app/services/cron/batch_run_state.py`
- Modify: `tests/unit/scheduler/test_cron_dispatch_intent_service.py`
- Modify: `tests/unit/scheduler/test_cron_scheduling_service.py`
- Modify: `tests/unit/scheduler/test_cron_execution_reconciliation.py`
- Modify: `tests/unit/scheduler/test_batch_operations.py`
- Create: `tests/unit/scheduler/test_batch_run_state_races.py`

**Interfaces:** `ClaimedDispatchIntent` 增加 `claim_token: str`。`claim_due_intents` 为一次领取生成 UUID，将 token 传给 `_claim_due_intent_ids` 和 `_fetch_claimed_intents`；后者必须按该 token 读取，不能只按 worker_id 读取。新增 store 方法 `prepare_handoff(row: ClaimedDispatchIntent, worker_id: str, now_utc: datetime) -> str`，返回 `ready/paused/skipped/stale`，自行持久化状态；只有 ready 能调用 HTTP。

- [x] 写失败测试：父 disabled 但 control running 仍建 batch；control paused 不建 batch；恢复后的旧时点不建新 batch；现有 batch 继续；paused 候选不挡住同模型其他父批次；所有重试也遵守暂停。
- [x] 用 asyncio.Event 测试屏障固定先后次序，并通过真实 MySQL 的 data_lock_waits 确认竞争。10 个并发／事务场景在 MySQL 8.1.0 REPEATABLE READ 下连续 10 轮通过，覆盖控制锁、CAS、容量限制、旧快照与初始化死锁；不以 SQLite 冒充行锁验证。

```python
assert after_pause["status"] == "pending"
assert after_pause["attempt_count"] == before_claim["attempt_count"]
assert after_pause["max_attempts"] == before_claim["max_attempts"]
assert after_pause["claim_token"] == ""
assert old_claim_token != new_claim_token
```

- [x] 运行 `$env:PYTHONPATH='scheduler/src'; .\.venv\Scripts\python.exe -m pytest tests/unit/scheduler/test_batch_run_state_races.py tests/unit/scheduler/test_cron_scheduling_service.py -q`，确认 gate 缺失时失败。
- [x] 将父批次 admission 与初始 intent 写入纳入同一短事务；可为现有 upsert/enqueue 提取接收 cursor 的私有实现，保留外部入口兼容。锁内复核 control 及当前父定义，所有 HTTP 和耗时外部调用在事务外。
- [x] 候选查询先筛控制状态再 LIMIT，真实锁后再次检查。初始化缺失控制的候选用独立前置步骤，不在已经锁多个 batch 后插入 control。
- [x] 交接前执行 guarded transition；SQL 条件须包含以下字段，后续标记 dispatched/unknown/fail 也传递当前 token 防止旧协程误写。终态执行回执保留现有 intent/batch/job/tenant/attempt 校验。

```sql
UPDATE swe_cron_dispatch_intents
SET status='acknowledged', acked_at=%s, locked_at=%s
WHERE id=%s AND status='claimed' AND lock_owner=%s
  AND attempt_count=%s AND claim_token=%s;
```

- [x] 确认 mark_intent_dispatched 接受 acknowledged 而非仅 claimed；对新 acknowledged 使用交接后回收预算，不能在 600 秒 claimed 预算内误重跑。已持久执行结果优先结算，暂停状态不得阻塞结果扫描。
- [x] 人工重试事务按 control→batch→intent 锁序，不改变原有一次额外尝试授权。paused 状态允许排队，API 在原 queued/skipped 字段之外返回等待暂停信息，不假装已开始执行。
- [x] 完整 Scheduler 测试通过，真实锁测试无数据库时明确列为未验证门禁，不宣称并发完全验证。

## Task 4: Disabled-job skip protocol and terminal settlement

**Files:**
- Modify: `src/swe/app/crons/manager.py`
- Modify: `src/swe/app/routers/internal.py`
- Modify: `scheduler/src/scheduler/app/services/cron/scheduling_service.py`
- Modify: `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`
- Modify: `scheduler/src/scheduler/app/services/cron/batch_operations.py`
- Create: `tests/unit/app/test_cron_disabled_dispatch.py`
- Modify: `tests/unit/scheduler/test_cron_scheduling_service.py`
- Modify: `tests/unit/scheduler/test_cron_execution_reconciliation.py`

**Interfaces:** `CronManager.run_job(...) -> bool | None`：仅未启动且已关闭时返回 False，现有成功 None 保留；`_run_job_callback` 只对明确 False 生成关闭响应。`SweCronCallbackClient.dispatch_job(...) -> str | None`：识别受支持的关闭标记并返回 `job_disabled`，历史 2xx 正常返回 None。

- [x] 写失败测试覆盖 Scheduler 最新定义已关闭、检查后 SWE 本地变关闭、旧成功响应无 skipped、旧普通 callback skip 标记、未知 skip 标记、超时未知结果、旧尝试迟到结果。

```python
assert response.json()["skipped"] == "job_disabled"
assert stored_intent["status"] == "skipped"
assert stored_intent["error_message"] == "任务已关闭，跳过执行"
assert feedback["success_count"] == 0
assert feedback["failure_count"] == 0
```

- [x] 执行 `.\.venv\Scripts\python.exe -m pytest tests/unit/app/test_cron_disabled_dispatch.py -q`，确认当前回调仍成功但不说明 skip 的失败。
- [x] 在 run_job 原 enabled guard 返回 False，并在 callback 用 `result is False` 判断，不能用 `if not result` 误判成功 None。返回 `{"status":"ok","skipped":"job_disabled","job_id":job_id}`。
- [x] Scheduler 仅明确的 job_disabled 转 skipped；旧 batch_managed_external_callback 等标记属于配置／协议不匹配，不能算 accepted 或成功跳过。未知非空 skipped 标记保守保持交接未知并记录协议原因，不立即重试可能已执行任务。
- [x] pre-handoff skip 和 callback skip 均 CAS 结算当前身份，清锁并记录 `intent_skipped`；不生成 retry_scheduled 或模型失败事件。关闭不将 attempt_count 改成 3，不修改 max_attempts 掩盖终态。
- [x] 更新 batch 聚合包含 skipped_count（status 为 skipped 或 cancelled）；`done+failed+skipped==total` 才是全终结，失败存在则 batch failed。all-skipped batch completed 但成功数 0。历史 cancelled 行保留原状态，计入“跳过／取消”终结数量，不算成功，不能留下永久 received。
- [x] 已有鉴权过期、LAILGW0429/LAILGW0433、真实失败重试测试不变为绿，记录 checkpoint。

## Task 5: Monitor projection and independent Console switch

**Files:**
- Modify: `monitor/src/monitor/app/models/cron.py`
- Modify: `monitor/src/monitor/app/services/cron/query_service.py`
- Modify: `console/src/api/modules/batchOperations.ts`
- Modify: `console/src/api/modules/monitor.ts`
- Create: `console/src/pages/Control/CronJobs/components/BatchRunStateControl.tsx`
- Create: `console/src/pages/Control/CronJobs/components/BatchRunStateControl.test.tsx`
- Modify: `console/src/pages/Control/CronJobs/index.tsx`
- Modify: `console/src/pages/Monitor/CronBatchDispatch/index.tsx`
- Modify: `console/src/pages/Monitor/CronBatchDispatch/FailurePanel.tsx`
- Modify: `console/src/pages/Monitor/CronBatchDispatch/index.test.tsx`
- Modify: `tests/unit/monitor/test_cron_dispatch_monitor.py`

**Interfaces:** `BatchRunState` 使用上述 API 的完整身份与版本；`batchOperations.getRunState(jobId)` 和 `setRunState(jobId, paused, expectedVersion)`。Monitor batch item 增加 `dispatch_paused: boolean | null`、`skipped_count: number`；stats 增加 skipped_intents。旧响应缺字段：skipped_count 显示 0，dispatch_paused 显示状态未知，不能以 parent enabled 推导。

- [x] 实施前加载 `copaw-f2e-review` 及 `console/DESIGN.md`，保持当前管理后台样式，不重构整页。
- [x] 失败测试：普通任务、普通广播任务和批调度子任务不显示整批开关且不请求接口；只有已保存 batch mode 的父任务显示。仅在弹窗里切换尚未保存的 mode 不发 run-state 请求。
- [x] 失败测试覆盖加载、状态未知、保存中、409 刷新、502 保留原值、关闭弹窗后旧请求到达、切换 jobId 后旧响应到达；暂停时按钮文案与父任务 enabled 不联动。

```tsx
expect(screen.getByText("批调度运行状态")).toBeVisible();
expect(screen.getByRole("switch", { name: "批调度运行状态" }))
  .not.toBeChecked();
expect(screen.getByText("已暂停后续调度；交接中及运行中任务自然收尾"))
  .toBeVisible();
```

- [x] 执行 `npm --prefix console run test:run -- src/pages/Control/CronJobs/components/BatchRunStateControl.test.tsx`，确认组件缺失或行为不符而失败。
- [x] 在现有广播设置弹窗中，与调度模式、优先策略分开展示该控件；独立保存不触发表单广播提交、不改变 priorityDirty。使用已存在 source/tenant 身份链，按钮加载期间禁止重复变更；未取得服务端确认不展示成功。
- [x] Monitor 查询按 batch 的准确 source、父 tenant、parent_job_id 关联控制表，不用子 tenant 关联；控制状态批量 join，禁止每张卡额外请求。历史 completed/failed 保持原结果状态，只有有排队意图时显示“暂停等待”的附加状态。
- [x] failed 卡仍不展示进度，增加“跳过／取消”数量；其他卡进度终结数包括 skipped_count。详情保留 skipped/cancelled 原状态并显示原因；FailurePanel 不提供这两种终态重试，paused 时人工失败重试返回“已入队，等待恢复”。
- [x] 执行 UI 测试、`npm --prefix console run typecheck`、`npm --prefix console run build`；浏览器核验普通任务、关闭父任务但整批运行、整批暂停、跳过详情，至少 1280px 和 1440px，保存截图但不自动打包。

## Task 6: Deployment, rollback and final verification

**Files:**
- Create: `docs/deploy/cron-dispatch-run-state-upgrade.md`
- Modify: `docs/adr/0010-independent-cron-scheduling-service-owns-batch-dispatch.md`
- Modify: `analysis/playbook/location-paths.md`
- Modify: `wiki/cron/cron-batch-dispatch.md`
- Update: this plan and its spec with resolved technical decisions and actual evidence.

- [x] 为迁移 CLI 写失败测试：默认 dry-run；apply 必須显式选择；重复运行不覆盖；普通任务无记录；数据库异常返回非零退出码。测试用隔离数据库，不能从用户环境默认连接生产。
- [x] 新增只针对本功能的迁移 CLI，不调用带有整库恢复语义的 `scripts/restore_rmassistdata_schema.py`。迁移计划命令如下；本轮未对业务数据库执行：

```powershell
$env:PYTHONPATH='scheduler/src'
.\.venv\Scripts\python.exe -m scheduler.migrate_batch_run_state --dry-run
.\.venv\Scripts\python.exe -m scheduler.migrate_batch_run_state --apply
```

- [x] runbook 写明：暂停外部批回调入口和旧 Scheduler 派发；冻结任务配置写入并确认 Monitor 定义同步无积压；在已授权窗口运行 dry-run、备份、增量 schema 与保守回填；升级 Scheduler/SWE/Monitor；核验全部旧派发实例已退出，再开启批回调、升级 Console。使用 Task 4 聚合规则定向刷新存在历史 cancelled/skipped 的批次，补齐新增计数而不重置意图。保留在途任务结果接收，不能为了升级把它们改为 pending。
- [x] SWE 的物理批定时器修复只能对已经确认的 batch parent 执行；升级后一次性核查或定向同步这些物理批定时器，不对所有普通任务执行 resume。旧父关闭且 control paused 的物理 callback 被新 Scheduler 拒绝建 batch；新父关闭但 control running 仍可唤醒其他子任务。
- [x] 对尚未迁移、旧后端 404、内部 token 缺失和缺表，UI 显示不可用并禁止操作；不能承诺旧 Scheduler 遵守新控制表。旧 Console 不显示新开关但不能覆盖状态。
- [x] 回滚必须先停止批回调和新版派发，保留控制表、token、skipped 数据；不能直接让旧 worker 接手。旧版不知道 paused 和 acknowledged 新交接含义，因此仅回滚展示层是安全子集；执行层回滚需先清点／收敛在途并保持批调度停用。
- [x] 提供迁移后只读核验 SQL，检查普通/子任务没有控制行、关闭旧父的初始 paused、孤立控制记录、正在 paused 父任务下的新领取及 skipped 聚合。归属解析需与迁移 helper 一致，SQL 诊断遇到非标准 meta 时不能用 LIKE 判断真实归属。

```sql
SELECT c.source_id, c.tenant_id, c.parent_job_id, c.paused,
       c.version, j.enabled, j.status, j.deleted_at, j.meta
FROM swe_cron_dispatch_controls c
LEFT JOIN swe_cron_jobs j ON j.id=c.parent_job_id
  AND j.tenant_id=c.tenant_id AND j.source_id=c.source_id;

SELECT b.batch_id, b.total_count, b.completed_count, b.failed_count,
       b.skipped_count, b.status
FROM swe_cron_dispatch_batches b
WHERE b.status IN ('completed','failed')
  AND b.total_count <> b.completed_count+b.failed_count+b.skipped_count;
```

- [x] 最终新鲜验证：Scheduler 整个单元目录；SWE 本任务涉及的 Cron API、task view、run_job headers、关闭交接测试；Monitor 批次查询测试；Console 相关 Vitest、typecheck、build；`git diff --check`。运行 MySQL 两连接竞态专项或明确列出未验证限制。
- [x] 顺序自审：① R1–R10 覆盖与普通任务隔离；② source/tenant 权限与旧客户端；③暂停/领取/交接竞态及次数返还；④ 超时、旧记录、反馈及聚合；⑤ 迁移/回滚与 UI 错误态。修复后重跑相应验证；不称为独立代理评审。
- [x] 执行 GitNexus detect-changes 检查预期受影响符号及进程；保留现有其他修改。不提交、不推送、不更新旧行内包。

---

## Coverage and execution handoff

### 2026-09-10 真实 MySQL 验证续作

使用本机 MySQL 8.1 二进制另起 loopback-only 临时实例，独立数据目录、端口和随机密码；不使用现有服务或业务库。测试文件为 `tests/integrated/scheduler/test_batch_run_state_mysql.py`，默认跳过，只有显式提供测试实例端口、数据目录和密码且服务端数据目录校验一致才运行。

验收覆盖：并发保守初始化、暂停先于交接、交接先于暂停、同模型并发领取容量、旧 token 拒绝、批次事务回滚。用事件屏障固定交错次序，并查询 MySQL 实际锁等待验证阻塞。发现问题先保留失败测试，再修改对应生产代码；测试完关闭隔离实例并更新验收记录。

| Requirements | Tasks |
|---|---|
| R1–R3 归属和旧任务保守初始化 | 1、2、6 |
| R4–R5 独立状态与新模式建立 | 1、2、3 |
| R6–R7 暂停／恢复及在途边界 | 2、3、5、6 |
| R8–R9 跳过、统计、不误取消 | 3、4、5 |
| R10 权限、UI、旧客户端 | 2、5、6 |

已在主线程顺序实现并完成本地回归、自审及隔离浏览器验证；未提交、推送、打包或执行业务数据库迁移。2026-09-10 已补齐真实 MySQL 双连接竞争验证，修复初始化间隙锁死锁和交接／人工重试旧快照问题，10 个场景连续 10 轮通过。实现另外补上了定向恢复旧物理批定时器，以及回调已接受／跳过后落库失败不立即重试的保护。详细上线与回滚要求见 `docs/deploy/cron-dispatch-run-state-upgrade.md`。

最终定向验证：Python 312 passed、Console 79 passed，TypeScript、构建、定向 ESLint/Prettier、新生产模块 Flake8 和 diff whitespace 检查通过。GitNexus 对包含此前本地改动的 tracked diff 报告 medium；新文件未完全进入索引，另以源码和测试核对。最后一次锁序自审移除了领取事务内的冗余超时清理，统一保留在前置回收阶段，并用旧 claimed/acknowledged 终态回执测试补强了防重复领取。
