# Cron CLI Scheduler 切换实施计划

> 按用户 AGENTS 约定在当前 v1.0.0 工作区由主线程顺序执行；不启动子代理，不自动提交、推送或打包。仅自检，不冒充独立审查。

**Goal:** `swe cron` 和 Agent 内置 cron 技能只管理 Scheduler 新任务，不再访问 SWE 旧管理接口。

**Architecture:** 保留既有 Click 命令和定义构造/局部编辑工具；新增小型 Scheduler HTTP 协议适配器处理分页、详情版本、操作回执及错误。命令使用显式身份或运行时身份；内置技能使用同一命令并按操作/执行回执确认结果。现有未提交的业务解耦、迁移等改动保持不动。

**Tech Stack:** Python、Click、httpx、pytest，现有 Scheduler API；不增加依赖。

**Spec:** 用户选择剩余事项第 2 项：切换 CLI 和 Agent 内置 cron 技能。旧迁移依赖的 CronManager 保留，所有服务仍共用数据库，迁移任务/会话 ID 不变。

## 全局约束

- 不修改服务端接口、旧管理器启动、迁移实现、前端、数据库或外部平台；未迁移任务不回退旧接口。
- URL 顺序：命令 --base-url、显式全局 --host/--port、SWE_SCHEDULER_API_URL、Scheduler 本地默认地址。其他 SWE CLI 的默认地址不变。
- Header 携带真实 actor、tenant、source、agent，不设置管理员角色。来源必须明确；运行时使用已有 SWE_TENANT_ID/SWE_SOURCE_ID。可通过 cron 组 --user-id 显式区分操作者和租户。
- 创建不自行生成 job_id，Scheduler 准备会话；旧 --target-session 只作为兼容输入，不承诺自定义新任务的会话绑定。更新不主动改任务 ID 或已绑定会话；禁止用内容更新偷偷改变 enabled，提示使用 pause/resume。
- 每次写请求附 request_id，支持 cron 组 --request-id 供创建超时复用；不自动重发或绕过版本冲突。失败保留返回的操作 ID/请求 ID，不打印私密底层异常。
- list 完整翻页并校验归属/身份/重复游标；get 只接受 Scheduler 详情；state 展示运行状态、task 和管理状态。
- delete 使用带 JSON body 的 DELETE；pause/resume 用 PUT enabled 和 management_version；run 返回 202 execution，不等待业务执行完成。
- 所有新增/修改函数复杂度最多 15；只格式化本轮文件。

## Task 1：HTTP 协议与 CLI 切换

**Files:** 新建 src/swe/cli/cron_client.py；修改 src/swe/cli/cron_cmd.py；新建 tests/unit/cli/test_cli_cron_scheduler.py；更新 tests/unit/cli/test_cli_cron_tenant.py 与 tests/unit/workspace/test_cli_agent_id.py 的旧响应夹具。

- [ ] 写 CliRunner + httpx.MockTransport 测试，断言真实发出的 URL/Header/Body 和 CLI 输出。
- [ ] RED：验证 list 尚不翻页、仍走旧 URL；写入没有 request_id/版本；缺来源没有拒绝。
- [ ] 实现 SchedulerCronClient(http, headers, request_id=None)：list_jobs、detail、state、create、update、delete、set_enabled、run、operation、retry、execution。

关键协议例子：

```python
# 创建：body 不是裸 spec。
{"request_id": "stable-request", "spec": spec, "dispatch_state": "ordinary"}
# 更新：版本来自同身份详情，绑定和 enabled 不在本地替换。
{"request_id": "stable-request", "spec": spec, "expected_definition_version": 7}
# 单任务启停与手动执行使用不同版本。
{"request_id": "stable-request", "enabled": False, "expected_management_version": 12}
{"request_id": "stable-request", "expected_definition_version": 7}
```

- [ ] list/get/state/create/update/delete/pause/resume/run 全部切换 /scheduler/cron/jobs。
- [ ] operation JOB_ID [OPERATION_ID] 查当前未完操作或指定操作；retry JOB_ID OPERATION_ID 续办原操作；execution JOB_ID EXECUTION_ID 查询一次执行。
- [ ] 增加超时、503 携带操作回执、409 冲突、不合法身份、分页循环、删除 JSON、禁用更新、原 session ID 保留的回归。
- [ ] GREEN：`wsl --cd /mnt/d/Projects/CoPaw env PYTHONPATH=src:scheduler/src venv/bin/python -m pytest tests/unit/cli/test_cli_cron_scheduler.py tests/unit/cli/test_cli_cron_tenant.py tests/unit/workspace/test_cli_agent_id.py -q`。

## Task 2：内置技能与使用说明

**Files:** src/swe/agents/skills/cron/SKILL.md，docs/deployment/cron_cli_scheduler.md。

- [ ] 先复现旧技能所缺的情形：202 回执不是执行成功、503 操作已落库、不能直接重建；用 Task 1 的实际命令场景验证。
- [ ] 更新技能版本，明确 Scheduler 入口、Agent/来源/用户身份、默认非工作日不执行、任务 ID/会话由服务端管理。
- [ ] 给出创建→获取 operation.job_id→operation 查询→get→update/pause/resume/delete 的命令，提供 run→execution 查询；超时不盲目重建。
- [ ] 以同一组 CliRunner 场景执行说明里的命令；不声称完成独立 Agent 压力测试（用户要求主线程顺序）。

## Task 3：复验与交付

- [ ] 主线程检查作用域、身份隔离、失败回执、ID 保留和生命周期；不把自检计作独立审查。
- [ ] 定向 CLI 测试，必要的 Scheduler 合约回归，局部 pre-commit（含复杂度、类型、格式）。
- [ ] `git diff --check`；核对未改旧工作区和迁移文件；GitNexus impact 当前被 lbug WAL/shadow 异常阻塞，以源码调用核对并披露限制。
- [ ] 告知命令语法变化和验证边界；无真实 Scheduler/行内平台联调，无自动提交、推送和行内包。

## 本地实施结果

- 已完成 Task 1、Task 2 和主线程自检。初始协议用例 19 项失败，新增身份/绑定/回执边界回归后，最终 CLI 与 Scheduler API 合约定向测试 61 项通过。
- 新增行为逐项先观察失败再实现，包括目标用户不能静默修改、错误归属回执拒绝、原输入保留、workflow 元信息更新及 retry 不显示伪造的新请求 ID。
- 本轮文件的 pre-commit 全部通过，包含复杂度上限 15、mypy、Black、Flake8、Pylint；git diff --check 通过。
- 内置技能元数据使用实际 frontmatter 库直接加载校验；版本 1.2。Windows PowerShell 管道方式会改变中文输入编码，最终不以该管道结果判断源文件有效性。
- 未运行独立 Agent 压力测试、真实平台/数据库联调；没有提交、推送、打包或停用 CronManager。
