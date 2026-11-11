# 新版任务优先领取共享模型容量

> **For agentic workers:** 按 executing-plans / TDD 在当前隔离 worktree 主线程执行；用户要求不派发子代理，自检不冒充独立评审。

**Goal:** 同一模型池内优先给当前可执行的新版任务留出空闲名额，旧队列只领取剩余容量，不抢占既有执行。

**Architecture:** 旧领取事务沿用共享 scope 行锁及新旧在途统计，再读取同池新版候选，按现有新版顺序模拟单任务并发占用，扣除其需要的空闲名额。新版自身领取仍使用原容量接口，不扣除自己的等待名额。候选判定复用新版工作查询，不增加表、缓存、运维开关或调度循环。

**Tech Stack:** Python、MySQL 8、现有 asyncio 调度循环、SQLite 真实 SQL 回归、pytest、pre-commit。

**Spec:** 本次对话已确认新版优先，旧版使用剩余容量；接受新版持续满载时旧版可能长期等待。

## 约束

- 仅相同 source_id/provider_id/model_id 之间竞争；新旧已领取/已运行任务不取消、不抢占。
- 尚未到期、租约未到期、任务停用/删除、日期范围外、暂停及过期队列不能阻挡旧队列。
- 同任务多条排队记录受冻结 runtime.max_concurrency 限制，不能把每条记录都算成一个可用名额；候选按现有新版顺序模拟，不改变批内/批间/加急顺序。
- 不阻断结果巡检及已受理请求恢复，不改变业务重试、结果、通知、外部平台接口。
- 不修改 SWE、Monitor、前端或 DDL。开发完成先保留本地修改，后续仅按用户单独请求提交，不自动推送或打包。

## Task 1：先验证容量让渡

**Files:** 新建 tests/unit/scheduler/test_managed_legacy_priority.py；复用现有 ExecutionDatabase、configure_pool 及真实领取事务测试替身。

- [x] RED：容量 10、3 条不同任务新版候选时旧版可领取 7；新版占位后旧版剩余仍为 7，新版自身可领取原空闲容量。
- [x] 覆盖已运行旧任务、不同领取者、不同模型/来源、禁用新版运行时；旧部署不能查询新版表，原 test_capacity_claim 保留过期 scope 租约覆盖。
- [x] 覆盖等待重试、未到/已过日期、停用、删除、暂停、队列失效、活动派发租约，以及单任务并发已满。
- [x] 同任务多条等待只留实际能运行的名额，并测试不同冻结并发上限的候选按顺序模拟；SQL/关系读取失败必须失败而不是放行旧领取。
- [x] 通过旧版 _claim_due_intent_ids 实际事务验证接线、共享锁先于候选读取，不能只测试独立 helper。

运行：`PYTHONPATH=scheduler/src:monitor/src:src /mnt/d/Projects/CoPaw/venv/bin/python -m pytest tests/unit/scheduler/test_managed_legacy_priority.py -q`（WSL，当前 worktree）。

## Task 2：最小实现

**Files:** scheduler/src/scheduler/app/services/cron/capacity_claim.py、dispatch_intent_service.py、managed/work_query.py、managed/runtime.py；tests/unit/scheduler/test_managed_cron_runtime_lifecycle.py。

- [x] 提取工作查询可复用的时间/身份/有效候选片段，保留原新版包含失效队列清理和 accepted 恢复的行为。
- [x] 新增旧队列专用容量入口，先调用原 available_capacity 获取共享锁及真实空闲容量，随后仅为当前可执行新版候选扣除名额；原新版容量入口不变。
- [x] 把旧领取调用切到旧队列专用入口；候选读取失败沿用事务回滚，不加吞异常降级。
- [x] 只在新版后台就绪时启用待运行优先，停止/异常退出时关闭；真实占用统计保持独立，不能因新版客户端配置失败而忽略已运行新任务。此状态不是新增配置。
- [x] GREEN：新测试与已有容量、批次顺序、重试、日期规则均通过。

## Task 3：文档与交付校验

**Files:** scheduler/CONTEXT.md、docs/deployment/cron_enhancements_validation.md、本计划。

- [x] 记录“新版优先领取”仅争取空闲容量、新版持续满载可能使旧任务等待，以及按事务当次候选快照判断，后来入队不抢占。
- [x] 全量 Scheduler 回归；Windows 项目虚拟环境运行个人 pre-commit 配置，保持函数复杂度不超过 15；git diff --check。
- [x] 主线程自检 scope/UTC 与北京时间换算/新旧占位去重/并发与失败路径；不以 SQLite 锁测试替代真实 MySQL 多实例压测。部署仅 Scheduler，预发布验证 10/3/7、已有旧执行不抢占、未来任务不阻挡。

## 验证记录（2026-11-03）

- 开发前将已有修改拆为 0299fb651（Monitor 聚合复用）与 421989047（日期维护异步），提交前 56 个定向用例和个人 pre-commit 通过，提交后工作区干净。
- 优先级 RED：实际旧领取事务观察到 10 条全部被领取，期望只领取 7 条；接入后通过，并继续验证后续领取者不能拿走预留的 3 个名额，执行释放后旧队列可继续领取。
- 后台就绪 RED：关闭优先状态后仍扣一个等待名额、生命周期就绪未启用优先，两例失败；补充内部就绪状态后通过。实际占用统计不受该状态影响。
- 最终 Scheduler 全目录回归：862 passed，54.09 秒；231 条现有 SQLite datetime adapter 弃用警告。一次中途测试与格式器并行引起依赖源码行号的测试失败，结束格式化后独立重跑全目录通过。
- 6 个 Python 文件及 3 个文档的最终 pre-commit 通过，包括复杂度上限 15、AST、mypy、Black、flake8、pylint。原大型 dispatch_intent_service.py 的附带排版由仓库格式工具整理，行为仅修改容量调用。
- 主线程自检核对身份隔离、容量占用不重复、按冻结上限模拟、北京时间跨日、锁在快照读取之前、读取异常回滚、新后台未就绪回退；没有派发子代理，不计为独立评审。
- 未连接真实 MySQL、未进行多实例压测。候选排序查询在共享 scope 锁内运行，大积压时应验证查询计划及锁持有耗时，不承诺行内性能结果。
- 开发完成时本功能保留为 codex/cron-task-tracking 的未提交修改；用户随后单独要求本地提交。没有推送、打包或修改前端讨论方案。
