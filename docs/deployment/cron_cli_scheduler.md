# Cron CLI 切换 Scheduler

## 范围

`swe cron` 的 list/get/state/create/update/delete/pause/resume/run 只操作 Scheduler 新任务，新增 operation/retry/execution 命令。SWE 旧管理器、存量迁移、服务端 API、前端和旧平台定时器不在本次改动范围内。

## 部署与身份

- 不新增表和配置项。SWE 执行 Agent shell 命令的进程复用现有 `SWE_SCHEDULER_API_URL`，例如 `http://scheduler:9100/api`；未配置时默认本地 `http://127.0.0.1:9100/api`，跨 Pod 部署必须设置正确地址。
- 仅 cron 子命令改变默认服务；其他 SWE CLI 不变。不再隐式使用上次 SWE 的 8088 地址。
- 地址优先级：子命令 --base-url > 显式全局 --host/--port > SWE_SCHEDULER_API_URL > 本地 Scheduler 默认地址。
- Agent shell 已由运行时注入 `SWE_TENANT_ID`、`SWE_SOURCE_ID`，CLI 使用这些身份；手工运行时显式传 tenant/source，或设置同名环境变量。agent_id 始终按内置技能要求显式传入。
- 操作者默认来自当前用户上下文，否则使用定义中的操作者提示或租户身份；可在子命令前使用 `swe cron --user-id actual-user ...` 区分 actor 和 tenant。不生成管理员 Header，也不在本次增加认证 Token 配置。
- 原提交将内置 cron 技能从 1.1 升至 1.2；1.3补充末尾年份、W/LW、日期区间和独立通知规则；1.4将nW说明更新为“n号（含）及之前最近工作日、不跨月、无候选跳过”。已有工作区应通过现有内置技能升级机制更新受管副本，不覆盖用户自行修改的技能文件。

## 手工验证

以下所有命令带相同的 `--agent-id default --tenant-id alice --source-id RMASSIST`。实际使用替换为测试身份，命令内的 job_id、operation_id、execution_id 从响应取得，不能使用示例伪造。

```bash
swe cron --request-id create-example create --type agent --name "CLI测试" --cron "0 9 * * *" --timezone Asia/Shanghai --channel console --text "输出测试报告" --agent-id default --tenant-id alice --source-id RMASSIST
swe cron operation JOB_ID OPERATION_ID --agent-id default --tenant-id alice --source-id RMASSIST
swe cron list --agent-id default --tenant-id alice --source-id RMASSIST
swe cron get JOB_ID --agent-id default --tenant-id alice --source-id RMASSIST
swe cron state JOB_ID --agent-id default --tenant-id alice --source-id RMASSIST
swe cron update JOB_ID --name "CLI测试修改" --agent-id default --tenant-id alice --source-id RMASSIST
swe cron pause JOB_ID --agent-id default --tenant-id alice --source-id RMASSIST
swe cron resume JOB_ID --agent-id default --tenant-id alice --source-id RMASSIST
swe cron run JOB_ID --agent-id default --tenant-id alice --source-id RMASSIST
swe cron execution JOB_ID EXECUTION_ID --agent-id default --tenant-id alice --source-id RMASSIST
swe cron delete JOB_ID --agent-id default --tenant-id alice --source-id RMASSIST
```

核对点：

1. 网络请求路径均为 `/api/scheduler/cron/jobs...`，缺来源时请求不发送；任务不存在不回退 SWE。
2. list 获取完整分页；get 返回详情，state 返回 state/task 和管理信息。更新名称、cron 时原 job_id、request.session_id、task.chat_id 不变，未修改的多模态输入和 workflow 绑定保留。
3. create/update/delete/启停的 operation.status=completed 才表示管理动作完成；202 pending/running 只是已受理。run 的 execution=queued 同样不表示业务成功。
4. 503 可能已经保存原操作；CLI 输出操作回执后非零退出，不自动重发。先 operation 查询，问题解决后用 `swe cron retry JOB_ID OPERATION_ID ...` 续办原操作。
5. 传输超时输出稳定 request_id，不暴露底层异常中的私密字符串。创建响应丢失可用完全相同的请求参数和原 request_id 重提；其他动作先查询详情及原操作，不用新 ID 盲目覆盖。
6. update 中 --enabled/--no-enabled 如果试图改变当前状态会被拒绝，使用 pause/resume；会话绑定/所属用户不通过 update 迁移。

## 验证边界

本地使用 CliRunner 和 httpx.MockTransport 验证命令实际发送的 HTTP 请求，并用真实 Scheduler Pydantic/定义规范验证请求结构；不连接行内数据库或真实平台。不把命令示例自检当作独立 Agent 压力测试。部署验收仍需实际环境执行上述链路。
