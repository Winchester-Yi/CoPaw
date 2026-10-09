# Workflow 类型定时任务

`workflow` 是第三种 Cron 任务类型。它根据第一个绑定技能选择已发布的
workflow 配置，按目标用户的结构化身份调用同步 JSON 接口。执行不进入
AgentRunner；任务会话、trace、Monitor execution、未读暂停和完成通知仍走
Cron 的既有收尾链路。

## 配置与发布

先由数据库迁移账号在共享 SWE/Monitor/Scheduler 数据库执行
`scripts/sql/cron_workflow_bindings.sql`。SWE 启动时不会创建或校验绑定表；
缺失的 `swe_cron_jobs.workflow_binding_id` 也由该脚本补齐。
三个服务必须指向同一份配置数据。

部署时设置 `SWE_WORKFLOW_ALLOWED_HOSTS` 为允许调用的主机名，多个值用逗号
分隔，例如 `workflow.example,workflow-internal.example`。没有允许的主机名时，
配置发布与执行均会拒绝接口调用。

Manager/Admin 可调用 `PUT /api/cron/workflow-bindings/{skill_id}` 发布配置；
请求所在的 `X-Source-Id` 决定绑定所属 source。每次发布产生不可变版本。
`GET` 同路径读取当前版本。下面是配置体示例：

```json
{
  "url": "https://workflow.example/run",
  "method": "POST",
  "timeout_seconds": 60,
  "headers": {
    "Authorization": {
      "source": "secret",
      "key": "auth_token",
      "prefix": "Bearer "
    }
  },
  "body": {
    "inputParams": {
      "sapId": {"source": "env", "key": "sapId"},
      "bbkorgId": {"source": "env", "key": "bbkOrgId"},
      "brnorgId": {"source": "env", "key": "brnOrgId"},
      "resposId": {"source": "env", "key": "rtlPstId"},
      "scene": {"source": "literal", "value": "daily"}
    }
  },
  "success_rule": {"path": ["code"], "equals": "OK"},
  "result_fields": {"result": ["data", "result"]},
  "renderer_key": "direct",
  "provider_id": "provider-a",
  "model_id": "model-a"
}
```

`headers`、`path_params`、`query` 和 `body` 支持固定值、目标用户 `envs.json`、运行时字段及
目标用户凭据。`auth_token`、`cookie` 可从目标用户 Cron 授权状态解析；其他
`secret` key 从目标用户 `envs.json` 读取。测试时 `API-Key` Header 可以配置
固定值并保存在绑定表中；Cookie、Authorization 等敏感 Header 仍不接受固定明文。
URL 路径中的 `{name}` 与 `path_params.name` 对应；JSON 请求体也可为顶层数组。
`path` 是 JSON 字段/数组索引组成的列表。`renderer_key=direct` 直接展示提取
结果；特制渲染器必须先由服务端代码注册，不从数据库执行脚本。

## 任务定义和执行

创建 workflow 任务时设 `task_type=workflow` 和至少一个 `skill_ids`；服务端
使用首个技能 ID 解析绑定并填入 `workflow_binding_id`。Agent/Text 任务不能携带
这个字段。Console 展示为“技能任务”；CLI 可通过 `swe cron create -f spec.json`
和 `swe cron update -f spec.json` 提交完整定义。

每次执行读取目标租户运行时范围的 `.secret/envs.json`，核对 `sapId` 与任务目标
用户，按配置组装 Header、查询参数和 JSON 请求体。每个 attempt 只发送一次 HTTP
请求。HTTP 错误、业务成功规则不满足、结果字段缺失、渲染失败或任务会话写入失败，
都使本次执行失败。成功后把渲染结果写入任务会话，生成 trace，并经 CronManager
写入 `swe_cron_executions`；Monitor 通知 worker 按 Agent 任务的产品规则发送
完成提醒。用户任务界面只显示技能任务及其结果。

普通/手动执行选择最新发布版本。批调度在批次创建时从共享配置表读取版本和
`provider_id/model_id`，将其固定到每个 intent；后续任务级重试使用同一版本。
这组模型字段只决定 Scheduler 容量池，不启动模型调用。
