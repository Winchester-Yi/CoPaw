# 批调度优先策略与按类型补跑 Implementation Plan

**Goal:** 一级分行/用户有序名单控制批次内顺序；失败批次展示结果数量；失败类型支持整 intent 单次人工补跑且受共享容量限制。

**Architecture:** SWE 管理源任务优先配置并代理管理员操作；Scheduler 固化 intent 优先依据，负责失败分类、人工入队及原子容量领取；Monitor 查询排序快照，Console 展示和交互。复用 jobs.meta、intents.payload 和 dispatch_events，无新数据库字段。source/provider/model 为共享容量边界，跨批次排序不变。

**Tech Stack:** Python / FastAPI / MySQL / React / TypeScript / Ant Design。

## 已确认需求与行为边界

- 有序用户名单优先于有序一级分行名单，同级沿用阅读热度与稳定排序；只影响实际接收者，不增加接收者。
- 首次批次创建固定排序及依据。重复 timer 回调不得覆盖已有 intent 的顺序、重试预算或 payload。
- 批次 failed 显示成功/失败数量，运行中继续显示进度；补跑复用 intent，不增加任务总数。
- 失败分类由 Scheduler 统一产生。历史报错按固定映射分类，未知保留原文。分类统计覆盖整个批次。
- 人工重试仅接受 failed 和预览时的 attempt，max_attempts 设置为 attempt_count+1；attempt 不清零，不直接调用 SWE。
- 每次最多处理 200 个预览条目，显示本次条数与分类总数；通过预期 attempt 防止重入、迟到和重复点击。
- 配置类错误需要确认已修复；超时/结果未知需要确认旧执行已停止。这些确认由后端校验，记录操作者与原错误。
- 鉴权过期不自动重试且不计 worker 失败；人工重试事件不是 retry_scheduled，不计一次新失败。
- 重试历史与状态更新同事务；分类结果和优先标签不得泄露原始 payload 中的回调 header。
- 管理操作经 SWE 现有 manager/admin 权限与 source 边界，再用内部 token 调 Scheduler；缺少内部 token 时拒绝代理。
- 领取阶段在共享 scope lease 行锁内校验租约、读取容量并统计在途、领取；HTTP 在事务外。旧 claimed 超时也必须先回收，防止满额无法接管。

## Task 1: Scheduler 排序快照

Files: `scheduler/src/scheduler/app/services/cron/dispatch_intent_service.py`, `scheduling_service.py`, new `batch_priority.py`, `tests/unit/scheduler/test_batch_priority.py`.

- [x] 写测试验证低热度优先用户先于优先分行，未配置保留原顺序，payload 不变异；运行失败。
- [x] 从父任务 meta.batch_dispatch_priority 读取 user_ids/branch_ids；按 job bbk_id（已归并一级）写 payload.dispatch_priority 的 basis/user_rank/branch_rank/branch_id。
- [x] 排序使用 `(user_rank or infinity, branch_rank or infinity, existing keys)`；重复入队只回传已有 id，不覆写历史。
- [x] 运行 `.venv/Scripts/python.exe -m pytest tests/unit/scheduler -q`，记录通过和需回归项。

## Task 2: 失败分类、预览和单次补跑

Files: new Scheduler `batch_operations.py`, `routers/batch_operations.py`; SWE `crons/batch_operations.py`; related router registrations; `tests/unit/scheduler/test_batch_operations.py`, `tests/unit/app/test_cron_batch_operations.py`.

- [x] 用持久化测试验证失败分类、拒绝过期 attempt、重复提交、跨 source、确认标志、事务失败回滚及 attempt 上限。
- [x] `GET /cron/dispatch/batches/{batch_id}/failures?failure_type=...` 返回 counts 和至多 200 个 `{id,attempt_count,error_message,failure_type}` 预览。
- [x] `POST /cron/dispatch/batches/{batch_id}/retry` 接受 candidates、confirm_resolved、confirm_stopped；代理到内部 `/scheduler/cron/dispatch/batches/...`。
- [x] Scheduler 在同事务写 manual_retry_requested 事件（旧错误/attempt/操作者）、failed->pending，max_attempts=attempt_count+1，清完成时间与锁，刷新 batch counts。
- [x] 分类和预览可读取标准配置错误、鉴权、模型限流、连接/服务异常、Agent 超时、子任务失败/超时及 unknown；保留原文，不重写执行记录。

## Task 3: 共享容量严格领取

Files: Scheduler `dispatch_intent_service.py`, `scheduling_service.py`, capacity/reconciliation tests.

- [x] 重现两次领取用相同空闲值的超额场景。
- [x] scope lease 行先锁；容量预算及所有在途数在同事务重查，限制实际领取条数。租约过期或 owner 不符不领取。
- [x] 容量调整写入与领取复用 scope 行锁；缩容允许已有任务排空，不撤销运行任务。固定锁顺序为 scope -> batch -> intent。
- [x] 验证无槽位、剩余槽位、跨 source/model 隔离、租约失效、失联 claimed 接管。

## Task 4: 配置保存和结果页

Files: SWE `crons/api.py`, `manager.py`; Monitor `models/cron.py`, `services/cron/query_service.py`; Console `Control/CronJobs/index.tsx`, new priority editor, `Monitor/CronBatchDispatch/index.tsx`, new failure panel, API types/modules and tests.

- [x] 配置保存使用独立 PUT priority API，复用广播操作锁、父任务权限、持久化与 Monitor 同步；策略单独修改不初始化所有子租户。
- [x] Console 使用有序可上移/下移名单，一级分行选择器复用现有常量，用户标识可从现有接收者选择；保存状态和错误提示完整。
- [x] Monitor 安全投影 payload.dispatch_priority，不返回完整 payload；列表按 dispatch_order/id，不强制父任务置顶。
- [x] 失败 batch 行显示成功/失败数量；详情提供分类选择、预览、确认和入队结果。请求代次防止切换 batch/source 的迟到结果污染。
- [x] `npm run test:run -- <affected tests>`、`npx tsc -b --noEmit`、相关 ESLint/Prettier、浏览器验证。

## 交付与审查

- [x] 分别审查：排序配置隔离；失败/重试身份；容量事务与租约；UI 状态/权限；整体回归。
- [x] 依据仓库工具映射在主线程顺序执行，不把自审记作独立 subagent 审查。
- [x] 更新 CONTEXT.md 和 ADR-0010/运行手册，列明人工确认语义与实际部署验证限制。
- [x] GitNexus impact 后改符号，完成 detect-changes 和 diff check；不自动提交或推送。

## 本地交付记录（2026-09-08）

- 实际文件布局：SWE 的配置/代理集中于 `src/swe/app/crons/batch_operations.py`，无需修改 CronManager；Scheduler 分类与人工入队为 `batch_operations.py`，容量事务为 `capacity_claim.py`，优先快照为 `batch_priority.py`。
- 排序、重试、容量、权限与 UI 分项均已实现；按仓库工具映射在主线程完成分项自审，不计作独立 subagent 审查。
- Scheduler：139 passed；Monitor 批调度查询：14 passed；SWE 管理/批调度 API：59 passed；Console 相关组件：27 passed。
- Console 生产 build、TypeScript、相关 ESLint/Prettier 通过；新 Python 模块及新增测试 Flake8 通过。已有依赖提示 Starlette TestClient 弃用、jsdom 伪元素不支持和前端 bundle 体积告警。
- Playwright 使用实际页面组件和隔离的验收数据，验证优先名单重排/保存、失败数量、优先依据、鉴权修复确认和单次入队交互；检查 1280x720、1440x900、1920x1080 视口并保留截图于 `output/playwright/`。没有对真实任务发起重试。
- 临时 UI 验收入口已清理。改动未提交、未部署，无数据库字段迁移。

### 部署环境验证（未执行）

- [ ] 在目标 MySQL 上用两个 Scheduler 实例验证 scope 行锁、重复人工提交及接管；SQLite 用例只验证 SQL/状态行为，不能证明 MySQL 行锁。
- [ ] 确认 SWE、Monitor、Scheduler 指向同一批调度数据，内部 token 一致，并使用同版本 Scheduler 写入者。避免新旧领取代码混跑。
