# 外部 jobs 列表：不初始化租户

外部系统只需读取已有定时任务时，调用 `GET /api/external/cron/jobs`。Console 继续调用原来的 `GET /api/cron/jobs` 或 Agent scoped jobs 接口。

## 入参和 Header

与原 jobs 列表相同，没有业务查询参数或请求体。不要套用 chats 的 `user_id` 筛选或分页规则；多余的查询参数与原接口一样被忽略。

```http
GET /api/external/cron/jobs
X-Tenant-Id: alice
X-Source-Id: portal
X-Agent-Id: default
Authorization: Bearer <token>
```

- `X-Tenant-Id`、`X-Source-Id`：沿用现有租户/来源身份要求。
- `X-User-Id`：可选，只影响 `task.visible_in_my_tasks` 等任务展示信息，不按该用户筛选 jobs。
- `X-Agent-Id`：可选，省略时选择持久化配置里的 active Agent。租户没有配置时尝试其默认工作区路径，不创建目录。
- `X-User-Name`、`X-Bbk-Id` 等原有可选身份头仍可传入；新接口不利用它们补写用户资料。
- `Authorization`：沿用部署环境的认证策略。新接口只豁免工作区加载，不加入身份、来源或认证公共白名单。

## 返回

直接复用 `list[CronJobListItem]`：保留 job 的所有原有字段及 `state`、`task` 结构。不存在 jobs 文件时返回 `[]`。

- `state`：当前进程已经加载该租户/Agent 的 CronManager 时，复制其已有状态；没有运行时则使用现有 `CronJobState` 默认值。不会为了读取运行状态启动 Workspace。
- `next_run_at`、`next_run_times`：使用和老接口相同的 cron 计算函数，计算后续三次展示时间。只更新响应副本，不改变已有运行时的 state。
- `task`：与老接口共享任务视图转换规则，读取已保存的绑定、摘要、未读数、暂停原因等。
- 新接口不补建任务会话绑定。历史 job 没有 `task_chat_id` / `task_session_id` 时，对应返回字段保持 `null`，不生成一个尚未存在的 Chat ID，也不写回 `creator_user_id`。

因此，输入、Header、返回字段和类型与原列表一致；对于未初始化运行时或缺失任务绑定的数据，返回已有信息，不承诺执行老接口的自动补全副作用。

## 为什么不能直接调用原列表

原列表先由 `TenantWorkspaceMiddleware` 调用 `TenantWorkspacePool.ensure_bootstrap()` 初始化租户，再通过 `get_cron_manager()` 按需启动完整 Workspace。

进入列表后，`_ensure_task_binding_for_read()` 还可能调用 `create_or_replace_job()`，补建会话、写回 job，并触发调度同步。新接口必须避开这条写入链路：直接读配置与 `jobs.json`，调用纯任务视图函数，不构造 CronManager，不注册调度、不发 Monitor 同步，也不修复或备份配置文件。

## 错误和定位

| HTTP 状态 | 含义 |
|---|---|
| 400 | 租户或来源身份头缺失/无效 |
| 401 | 未通过现有认证策略 |
| 403 | Agent 被禁用，或工作区/文件路径越出租户边界 |
| 404 | 指定 Agent 不存在 |
| 503 | 配置/jobs 文件损坏、不可读取，或来源配置服务不可用；不会伪装为空列表或自动修复 |

实现：[external_jobs.py](../../src/swe/app/routers/external_jobs.py)。共享转换：[task_view.py](../../src/swe/app/crons/task_view.py)。原列表：[api.py](../../src/swe/app/crons/api.py)。测试：[test_external_job_list.py](../../tests/unit/app/test_external_job_list.py)。

```bash
venv/bin/python -m pytest tests/unit/app/test_external_job_list.py tests/unit/app/test_cron_task_view.py tests/unit/app/test_cron_utils.py tests/unit/app/test_cron_json_repo.py -q
```
