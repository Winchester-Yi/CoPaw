---
status: completed
---

# 新任务工作日、通知和系统调度迁入计划

> 按用户约定在已有 `codex/cron-task-tracking` 工作区由主线程顺序执行，使用 executing-plans；不调用子代理。自检不能称为独立评审。未经另行要求，不提交、推送或打包，不访问行内数据库/调度平台。

**Goal:** 迁入用户选定的 1～7 项能力，兼容当前年份 Cron、运行日期区间及新版管理页，并证明新任务完成通知按独立的来源工作日开关过滤。

**Architecture:** 以已提交对象为迁移源，保留当前分支新增行为；Scheduler 负责定义、日历和平台调度，SWE 执行业务并直接写共享数据库。实际工作日判断与通知过滤独立，沿用现有日历缓存及降级，不重写第三方 croniter。

**Tech Stack:** Python/FastAPI/croniter/MySQL、React/Ant Design/TypeScript、pytest/Vitest/Playwright。

**Spec:** 本轮用户确认迁入以下七个提交的功能，并要求新任务支持工作日规则、分析非工作日不通知的实际效果：

| U | 来源提交 | 范围 |
|---|---|---|
| U1 | `64702c141` | Scheduler 新任务对外查询、共享日历及预览底座 |
| U2 | `49e561747` | SWE 完成通知工作日过滤、Scheduler 日历接口 |
| U3 | `f12cece36` | 新任务工作日执行、W/LW、列表/详情预览及表单 |
| U4 | `1faeec8bf` | 精选案例改走 Scheduler 创建 |
| U5 | `c579f893c` | 心跳、梦境、会话清理、归档系统调度迁移 |
| U6 | `3a1e41855` | 来源设置页加载请求竞态修复 |
| U7 | `16570e8ac` | CLI 与内置 cron 技能改走 Scheduler |

## Global Constraints

- 基线 `ba57565b6`；主工作区 `v1.0.0` 的提交和未提交修改都不动。仅取上述提交，不带入其后的业务解耦、聊天只查新任务、Workflow 路由拆分、存量接管及统一接口日志。
- 保留五段 Cron 和末尾年份的六段 Cron；年份支持单值、列表、范围及现有边界。秒固定为 0，平台星期转换不变。
- 保留北京时间日期区间、父子继承、23 点次日预开和维护异步接口。工作日规则不能覆盖日期约束或人工停用，预览必须同时满足年/日期段/日历。
- `schedule.run_on_non_workday` 缺省 false；自动普通及批入口使用日历，手动执行不增加工作日门禁。批次按父任务逻辑日期判断一次，不给内部成员增加二次散列或工作日门禁。
- 来源 `cron_notifications.skip_weekend_zhaohu_enabled` 与执行开关独立；沿用现有成功 agent/workflow 的招呼通知范围，text/失败/删除任务不新增通知；手动执行保持豁免且完成时到期。
- 非工作日通知判断使用原定通知的北京时间日期，保留批提前量、通知延迟及当日失败补跑的 notification_at。不开启来源过滤时不访问日历，不清除结果、未读或改变 is_final。
- 保留 `c993baba3` 的新版执行来源配置绑定、20 次等来源阈值、执行恢复上下文、当前重试/优先级、JSON 物化查询。
- 前端沿用现有 Ant Design、管理页设置页签、日期只改父组和多小时/年份字段；不改旧任务页面行为，不引入新依赖。兼容 Chromium 108，真实基线浏览器不可用时明确记录。
- 三张 SQL 表由环境负责人手动准备：`swe_workday_calendar`、`swe_cron_system_jobs`、`swe_cron_system_executions`；应用不执行 DDL，不增加必配内部 Token。
- 旧用户任务及 jobs.json 链路保留。系统任务切换需停用旧平台定时器，旧系统回调返回 410，新系统任务按需刷新；不得宣称全量自动迁移系统配置。

## U1～U3：日历、预览、通知和自动准入

**Files:**
- 新增来源提交中的 `scheduler/.../cron/calendar_schedule.py`、`workday_calendar.py`、`managed/external_query.py`、`managed/calendar_policy.py`、`managed/calendar_views.py`、路由 `calendar.py` 和 `external_cron.py`。
- 合并 `managed/definition.py`、`cron_syntax.py`、`views.py`、`repository.py`、`callback_contract.py`、`scheduling.py`、`platform.py`、`scatter.py`、分发子任务写入处。
- 合并 `src/swe/app/crons/models.py`、`managed_history.py`、`managed_results.py`；新增 `calendar_client.py`。
- 保留并扩展 `tests/unit/scheduler/test_managed_cron_year.py`、日期相关回归；迁入来源提交的日历、外部查询、通知测试。新增 `test_managed_workday_year_dates.py` 与 `test_managed_notification_calendar_context.py`。

**Interfaces:** 保留已有 HTTP 请求响应，新增的日历/外部查询采用来源提交契约；`validate_cron` 接受五段及年份六段，`calendar_rule`/`candidate_cron` 不丢年份。列表与详情的异步日历预览使用同一份有效区间。

1. 先迁入测试和适配断言，验证缺失能力失败。重点反例：`0 9 LW * * 2027` 不应丢年份，年份过期返回空预览；预览日期必须位于 start/end 内；周末调休可执行、工作日法定假期跳过。
2. 只对选定源提交做三方内容合并，用 apply_patch 写入；记录冲突文件，不用源文件整覆盖当前新增逻辑。
3. 将日历校验与本分支 `cron_syntax` 年份解析统一；普通表达式复用已有迭代器，W/LW 以日历选择日期、保留月份及年份，不放宽未支持语法。
4. 串联当前日期门禁与来源日历准入；批逻辑日期、实际日期窗口、提前触发及平台表达式分别测试，不把通知时间当执行时间。
5. 保留来源绑定并从事务外取得通知日历；真实 ExecutionWriter 写历史断言：过滤开时假日 `not_required`、调休 `pending`；过滤关不调用日历；手动、失败、text、删除、跨日提前批及补跑快照保持原语义。
6. 检查 Monitor 通知 claim 与 SWE 通知发送 worker 的筛选，确认 `not_required` 不会被再次领取，保留现有发送重试机制，不扩大为发送时重新过滤积压记录。

**Verification:** `PYTHONPATH=scheduler/src:monitor/src:src venv/bin/python -m pytest tests/unit/scheduler -q --tb=short`；SWE 日历通知、managed_results、来源绑定、补跑及旧通知专项。预期全部绿色；假外部仅替代日历/平台/模型网络，结果写入使用真实测试库适配器。

## U4、U6：前端及精选创建

**Files:** 来源提交的 `ScheduledTaskPopup`、`CronJobFormBody`、`SchedulerCronJobs/ManagedJobEditor.tsx`、`scheduleForm.ts`、`CaseDetailDrawer`、`useManagedShortcutCreation.ts`、`useCronContext.ts`、`SystemConfigPage` 及相邻测试。

**Interfaces:** 表单保留当前 hours/custom/year/run_date_range；非工作日字段只在新模式使用，精选入口隐藏假期配置并明确 false。使用当前 Scheduler 异步创建/查询/重试 API。

1. 先迁入相关测试并补年份+W/LW往返及父组日期受限编辑用例；验证当前缺失字段/控件失败。
2. 按现有视觉体系移入工作日选择与服务端预览，保留用户刚确认的新管理页布局和只改日期交互。
3. 移入精选案例创建和固定快照续办，拒绝迟到身份响应；保留来源设置页已有修复及本轮 pendingLoad 防重入。
4. 运行 Vitest、tsc、生产构建，并用既有 Playwright 工具检查新任务编辑、快捷创建、系统同步状态及嵌入布局；不拿 UA 模拟冒充 Chromium 108。

**Verification:** `pnpm --dir console exec vitest run` 指定相关表单、快捷入口、来源设置及系统配置测试；`pnpm --dir console exec tsc --noEmit`；`pnpm --dir console build`。

## U5：系统调度迁移

**Files:** 来源提交的 `scheduler/app/services/cron/system/`、`routers/system_cron*.py`、应用生命周期；`src/swe/app/crons/system_*.py`、`managed_session_cleanup.py`、配置保存路由、来源维护与旧 manager 系统注册处；来源提交的两表 SQL 及前端 `SystemCronSyncStatus`。

1. 先迁入系统管理/执行/配置回归，验证本分支缺少新协议的断言失败，再移入实现。
2. 保留四种业务动作与原看板，调度交 Scheduler，业务动作仍由 SWE 执行；本轮不提前迁第 8 项解耦。
3. 检查配置保存、异步平台同步、旧系统回调 410、新受理围栏、执行终态及 unknown 人工核实，保持共享数据库事实。
4. 核实 source 级清理/归档不混入用户任务列表，不新增全量注册扫描。手动 SQL 只交付不执行。

**Verification:** 来源提交的 `test_system_cron_*`、`test_source_system_*`、`test_managed_cron_session_cleanup.py`、系统 HTTP 链测试；不连接实际平台。

## U7：CLI 和内置技能

**Files:** `src/swe/cli/cron_client.py`、`cron_cmd.py`、`src/swe/agents/skills/cron/SKILL.md`、CLI 测试及部署说明。

1. 先迁入 CLI 测试验证请求仍打旧服务等反例，再合并来源提交实现。
2. 保留租户/来源/Agent Header、版本和幂等请求号；支持当前年份 Cron，未修改的任务日期和工作日开关必须保留，创建缺省非工作日不执行。
3. 确认内置技能升级方式及旧任务 ID 不回退旧管理 API，文档不声称已迁移存量任务。

**Verification:** `pytest tests/unit/cli/test_cli_cron_scheduler.py tests/unit/cli/test_cli_cron_tenant.py tests/unit/workspace/test_cli_agent_id.py -q`，通过 HTTP MockTransport 检查实际协议。

## 最终五轮顺序自检及交付

1. 范围：只带入 1～7，核对基线新能力未被覆盖，主工作区指纹未变。
2. 排程：五段/年份/W/LW/日期区间/提前批次/跨年/过期/日历降级，补真实输入输出反例。
3. 通知：来源上下文、事务外日历、普通/批/手动/补跑/失败/删除、未读与 is_final、领取筛选，补反例后复跑。
4. 系统及前端：数据库身份和围栏、旧入口切换、异步失败恢复、表单往返、权限/迟到响应、Chromium 108 限制。
5. 最终差异与回归：相关 pytest、Vitest、tsc、构建、pre-commit（复杂度≤15）和 diff 检查；记录未能验收的真实平台与浏览器项。

将部署前置条件、工作日通知生效条件及非目标写入配套部署文档。实际验证记录另存，不在执行中重写本计划决策。用户未请求提交，因此完成后保留工作区修改，交付文件范围和测试结果。
