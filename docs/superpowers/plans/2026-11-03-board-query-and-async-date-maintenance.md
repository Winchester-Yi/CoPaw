# 批次列表聚合复用与简单异步日期维护

> **For agentic workers:** 在现有隔离 worktree 中按 executing-plans / TDD 主线程执行；用户指令禁止派发子代理，自检不冒充独立评审。

**Goal:** 不拆批次列表接口，消除重复重聚合；日期维护立即返回 202，后台同步外部入口，保留人工启停和调度模式。

**Architecture:** 将带 GROUP BY 的批事实作为共用 CTE，统计与分页从同一结果读取，一条 SQL 返回摘要及当前页；详情保持原查询。维护复用 DateMaintenanceService.apply，使用 FastAPI BackgroundTasks，后台记录完成/部分失败/异常，无调用进度存储或恢复。

**Tech Stack:** Python、FastAPI、MySQL 8 CTE、SQLite 真实 SQL 回归、pytest、现有 pre-commit。

**Spec:** 本次对话确认的方案 1：不改页面 enabled/dispatch_state；不处理通知孤立记录；不新增表、缓存、接口进度存储或自动恢复机制。

## Task 1：列表只聚合一次

**Files:** monitor/app/services/cron/managed_board_sql.py、managed_board.py、tests/unit/scheduler/test_managed_board_query.py。

- [x] RED：用 SQLite 实际执行追踪，列表即使只取一条也只能执行一次 GROUP BY；参数化覆盖状态筛选、空页、无匹配数据，统计不是当前页求和。
- [x] GREEN：新增仅供列表使用的 CTE 查询，重聚合 CTE 自身含 GROUP BY，防止仅外层 CTE 合并而重复内部计算；保持原始身份、成员有效性和 unavailable 检查。
- [x] 保持详情 aggregate_query 不变，接口 items/total/stats/page/page_size、全局坏关联失败及排序不变；不做 JSON_TABLE 或展示字段拆分等额外优化。

## Task 2：简单异步维护

**Files:** scheduler/app/routers/managed_cron_date_ranges.py、日期 API/预开测试、新增异步 API 测试、docs/deployment/cron_enhancements_validation.md。

- [x] RED：通过 ASGI 响应发送事件确认平台维护阻塞时 HTTP 响应已发送；失败/部分失败均不改变 202 受理响应，缺 source_id 仍为 422。
- [x] GREEN：POST 原路径返回 202、accepted=true、source_id；后台调用原维护流程，记录 source、日期、计数和异常诊断，不增加轮询接口。
- [x] 原服务测试继续验证次日预开、保留今天、关闭过期、人工暂停/批暂停不覆盖、源共享入口去重与队列失效。
- [x] 更新 Postman 文档：202 不是完成，结果看后台日志/任务同步标记和外部平台；进程中断后人工重调，同来源不要重叠调用。

## 验证与交付

- [x] 执行批次查询和异步维护定向回归，再执行 Scheduler 全目录及相关 Monitor 测试；MySQL 性能必须在隔离环境/行内用实际执行计划核验，不能以 SQLite 计时替代。
- [x] 现有 pre-commit 检查本次 Python 文件，复杂度最大 15，git diff --check；确认未修改通知、前端、SWE、DDL 或用户数据。
- [x] 保留本地修改，不提交、推送、更新分支或打包。

测试使用项目 WSL 虚拟环境：PYTHONPATH=scheduler/src:monitor/src:src /mnt/d/Projects/CoPaw/venv/bin/python -m pytest。

## 验证记录（2026-11-03）

- 列表 RED：6 个参数化用例均观察到真实 SQL 执行了两次聚合；改写后批查询与接口 19 个用例通过。
- 维护 RED：3 个用例失败，分别证明旧接口在后台未完成前未发送响应、部分失败返回 200、异常返回 503。改写后真实 ASGI 发送顺序、后台失败记录及原日期服务规则通过。
- 最新定向回归：56 passed。最终 Scheduler 全目录加原通知服务回归：843 passed（63.28 秒），231 条现有 SQLite datetime adapter 弃用警告。
- 本次 7 个 Python 文件的 pre-commit 最终全部通过：AST、编码、敏感信息、复杂度上限 15、mypy、Black、flake8、pylint 等；格式工具仅整理本次文件。git diff --check 通过。
- 主线程自检覆盖 CTE 聚合复用、过滤参数绑定、空页摘要、坏关联失败、来源隔离、详情查询不变及 API 202 不承诺完成；实际服务 API 回归验证平台入口关闭而 enabled/dispatch_state 不变。
- GitNexus 影响查询尝试失败，当前 worktree 未注册；不使用其他 worktree 的索引冒充验证。按用户指令未派发子代理，源码自检不计为独立评审。
- 未连接行内 MySQL、未做实际性能压测或平台联调；不能据本地测试承诺消除所有 15 秒 504。已有部署文档给出 EXPLAIN 和真实范围回归要求。
- HEAD 保持 82454397f；没有执行 git add/commit/push，没有打包。新建计划及异步测试文件在执行期间被观察为暂存，未调整其来源或暂存内容。
