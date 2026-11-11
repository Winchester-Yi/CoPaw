# 新版定时执行绑定来源配置

> **For agentic workers:** 在当前 worktree 主线程按 executing-plans / TDD 执行；用户要求不派发子代理，自检不计作独立评审。

**Goal:** 配置未读阈值 20 时，新版自动任务第 10 次不暂停、第 20 次暂停；手动成功仍只增加未读、不触发自动暂停。

**Architecture:** 保留 /api/internal/ 的身份和配置中间件豁免。仅在新版 execute/recover 完成身份及执行记录校验后，通过既有 source_system_config_service 解析已验证来源，复制配置快照并在 ContextVar 作用域内初始化执行器、启动后台任务或补写结果。后台任务继承快照，HTTP 结束后仍有效；作用域退出恢复原上下文。

**Tech Stack:** Python、FastAPI、ContextVar、现有来源配置服务、pytest、SQLite 测试数据库。

**Spec:** 本次对话中已复现配置服务返回 20，而新版内部接口跳过来源中间件后在后台读取默认 10。用户要求先提交前端，再修复该问题。

## 边界

- 已将前端工作独立提交 a2c50d531，本修复不修改前端、Scheduler 或表结构，不新增配置项。
- 不修改默认阈值 10，不修改手动执行规则、已读行为、暂停后恢复规则或旧 CronManager。
- 配置服务缺失、解析抛错或来源不匹配时返回 503 并记录不含配置内容的错误日志，不能静默按默认值开始业务。已解析服务的正常缓存/兜底策略沿用既有实现。
- 日志只含来源、任务、配置版本、自动暂停开关和阈值，不输出配置正文或密钥。
- 本修复开发完成后先保留本地修改，后续仅按用户请求本地提交；不推送、不打包。

## Task 1：真实入口回归先失败

**Files:** tests/unit/routers/test_managed_cron_source_config.py；tests/fakes/managed_source_config.py。

- [x] 同时挂真实 TenantIdentityMiddleware、SourceSystemConfigMiddleware、新执行路由，以真实 SQLite 写入验证 9→10 不暂停、19→20 暂停；兼容普通/批任务。
- [x] HTTP 202 返回后再释放执行器，结果仍使用绑定的 20；并发不同来源不串配置、错误来源的外层上下文不被继承。
- [x] 配置关闭自动暂停后超过阈值不暂停；手动成功跨阈值仍不触发自动暂停。
- [x] recover 从已有结果凭证补写，读取当前来源配置，未再次运行执行器。
- [x] 配置不可用和来源错误不执行、不误记成功，保留可重试的受理记录；未授权请求不加载配置。

运行：`PYTHONPATH=scheduler/src:monitor/src:src /mnt/d/Projects/CoPaw/venv/bin/python -m pytest tests/unit/routers/test_managed_cron_source_config.py -q`。

## Task 2：最小配置作用域

**Files:** src/swe/app/crons/managed_execution_api.py。

- [x] 增加专用于新版执行的异步配置作用域；调用现有 resolve_config 与 bind_source_system_config，不改全局路由豁免。
- [x] execute 在作用域内创建后台任务，recover 在作用域内恢复结果；身份、派发租约和重复执行保护顺序不变。
- [x] 日志提供安全的有效配置诊断；新增测试转绿。

## Task 3：兼容回归与交付

**Files:** 既有 managed execution / fencing / dispatcher / recovery / HTTP chain 测试，补齐其最小 app 的来源配置服务；本计划。

- [x] 运行新旧定时执行与来源配置相关回归、Scheduler 回归及 HTTP chain。
- [x] pre-commit（复杂度 ≤15）和 git diff --check；自检跨来源、HTTP 返回后的配置存活、异常后的上下文恢复与秘密不出日志。
- [x] 记录测试结果和上线验证：同来源 effective 配置为 20；SWE 绑定日志显示 20；自动成功未读达到 10 不暂停，达到 20 才暂停。已自动暂停任务不会因修改阈值或升级自动恢复。

## 验证记录（2026-11-03）

- 前端改动先独立提交 a2c50d531；提交前新版前端 17 个文件、95 个用例通过，提交钩子（含 TypeScript）通过。
- 新回归 RED：最初 9 个用例均失败，确认阈值 20 被默认 10 替代、关闭开关无效、来源隔离与恢复补写缺少配置，以及配置不可用仍启动业务。
- 修复后首次执行/恢复/派发围栏/HTTP 链路定向回归 38 passed；随后扩展手动第 21 次、错误外层来源、未授权请求、两种入口加载失败等，共 15 个新回归用例。
- 最终相关大范围回归 1146 passed（83.90 秒）；232 条既有 SQLite 日期适配器及 TenantWorkspacePool 弃用提示。范围含 Scheduler 全目录、SWE managed cron 执行/资源/通知、来源配置、旧 scheduled boundary 和真实 ASGI HTTP chain。
- 8 个 Python 文件最终 pre-commit 全部通过：AST、编码、敏感信息、mypy、Black、flake8、pylint、修改函数复杂度上限 15；git diff --check 通过。
- 业务代码仅改 src/swe/app/crons/managed_execution_api.py；其他改动为测试、测试配置服务替身和本计划。没有修改身份豁免规则、默认阈值、手动暂停规则、Scheduler 或前端业务代码。
- 主线程自检覆盖授权后加载、配置快照生命周期、并发来源隔离、异常恢复原上下文、读取失败不执行业务和日志不输出配置正文；未派发子代理，不计作独立评审。
- 没有连接行内数据库或调度平台；开发完成时保留未提交修改，用户随后要求本地提交。未推送、未打包。

## 行内验证

1. 使用任务相同的 X-Source-Id 查询 `GET /api/source-system-config/effective`，确认 `config.cron_unread_auto_pause.threshold=20` 且开关开启。
2. 升级 SWE 后由调度入口触发任务，查日志“新版定时任务已绑定来源配置”：source_id 应匹配，`unread_auto_pause_threshold=20`；同时留意 version 和 config_stale。正常缓存/存储兜底仍沿用原来源配置服务策略。
3. 阈值验证使用普通/批调度触发，不使用手动立即执行；避免打开会话内的报告导致已读清零。可用 Scheduler 任务详情查询观察未读数，第 10 次不暂停，第 20 次产生自动暂停原因，随后 Scheduler 更新启停和旧表。
4. 手动成功仍累计未读但不直接触发自动暂停，即使达到或超过 20；这些未读会参与后续调度成功时的阈值判断。
5. 已自动暂停的任务不会因本次升级或调高阈值自动恢复；需要显式恢复后重新验证。结果凭证恢复补写也应出现正确来源配置日志，且不能再次执行业务。
