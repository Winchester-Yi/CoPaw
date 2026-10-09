# Workflow 绑定配置操作手册

本文供配置人员把一个技能 ID 绑定到 workflow 接口。**发布绑定**只保存调用配置；
定时任务执行时才会调用业务接口。

## 1. 准备信息

| 项目 | 本次测试示例 | 说明 |
| --- | --- | --- |
| SWE 地址 | `https://<swe-host>` | 调用绑定接口的服务地址，不是 workflow 网关地址 |
| 来源系统 | `RMASSIST` | 放入请求头 `X-Source-Id`；创建任务时必须相同 |
| 技能 ID | `<skill_id>` | 放入绑定接口路径；任务选择的**第一个**技能 ID 必须与之相同 |
| 管理员身份 | `<manager_user_id>` | 请求头 `X-User-Id`，角色 `X-User-Role: manager` 或 `admin` |
| workflow 地址 | 见下方 JSON | 由 SWE 执行任务时调用 |
| API-Key | `<API_KEY>` | 测试期间允许作为 `API-Key` Header 固定值保存到数据库；不要放入共享文档 |
| 调度模型 | `<provider_id>`、`<model_id>` | 两项必填；批调度时决定容量池，不代表 workflow 使用模型推理 |

数据库迁移账号需先执行 `scripts/sql/cron_workflow_bindings.sql`，创建绑定表、
版本表，并给 `swe_cron_jobs` 增加 `workflow_binding_id` 列。SWE 启动时不建表、
不校验表结构。SWE 环境变量需包含
`SWE_WORKFLOW_ALLOWED_HOSTS=aplus-gateway.paasuat.cmbchina.cn`，修改后重启 SWE。

## 2. 编写请求体

将下面内容保存为本地的 `workflow-binding.json`，替换三个尖括号占位值。
**不要把填有真实 API-Key 的文件提交到仓库或发到聊天中。**

```json
{
  "url": "http://aplus-gateway.paasuat.cmbchina.cn/openapi/runtime/app/07c1cda53/tag/dev/workflow/run/WFJjSaPwuz",
  "method": "POST",
  "timeout_seconds": 60,
  "headers": {
    "API-Key": {"source": "literal", "value": "<API_KEY>"},
    "Content-Type": {"source": "literal", "value": "application/json"}
  },
  "body": {
    "inputParams": {
      "sapId": {"source": "env", "key": "sapId"},
      "bbk": {"source": "env", "key": "bbkOrgId"},
      "brn": {"source": "env", "key": "brnOrgId"},
      "rst": {"source": "env", "key": "rtlPstId"},
      "cookie": {"source": "secret", "key": "cookie"},
      "token": {"source": "secret", "key": "auth_token"}
    }
  },
  "result_fields": {"response": []},
  "renderer_key": "direct",
  "provider_id": "<provider_id>",
  "model_id": "<model_id>"
}
```

这份测试请求按已确认的约定**不传** `openId` 和 `responseMode`。
`result_fields.response: []` 表示先取整个返回 JSON 作为展示结果；取得真实成功响应后，
再把它改成精确字段路径，并按接口协议增加 `success_rule`。
例如，只有在响应确实为 `{"code":"OK","data":{"result":"完成"}}` 时，
才能使用 `"success_rule":{"path":["code"],"equals":"OK"}` 和
`"result_fields":{"result":["data","result"]}`。当前执行引擎要求一次返回
完整 JSON，不能直接解析 SSE 事件流；响应体上限为 1 MiB。

`source` 的含义：`literal` 使用配置中的固定 `value`；`env` 按 `key` 读取目标
运行时 scope 的 `.secret/envs.json`；`secret` 按 `key` 读取密钥。这里的
`cookie`、`auth_token` 优先来自目标用户保存的 Cron 授权状态。当前实现会先读取
`envs.json` 中的同名 key，再用可用的 Cron 授权值覆盖；测试时应确认最终来源。

### 其他可配置字段

| 字段 | 用法 |
| --- | --- |
| `method` | `GET`、`POST`、`PUT` 或 `PATCH`；`GET` 不可配置 JSON `body` |
| `headers` | Header 名到映射对象；`API-Key` 可用 `literal`，`Cookie`、`Authorization` 不可用固定值 |
| `path_params` | URL 中的 `{name}` 与这里的 `name` 一一对应，执行时进行 URL 编码 |
| `query` | 查询参数名到映射对象；URL 本身不填写 `?` 查询串 |
| `body` | JSON 对象或数组；内部任意层可写映射对象 |
| `success_rule` | 可选，`path` 定位业务状态字段，`equals` 给出成功值 |
| `result_fields` | 必填，展示字段名到响应路径的映射；空路径 `[]` 取整个 JSON，数字路径段可索引数组 |
| `renderer_key` | `direct` 直接展示；其他值须由 SWE 服务端预先注册渲染器 |
| `provider_id`、`model_id` | 必填，批调度容量归属；即使只手动测试也要填写非空值 |
| `timeout_seconds` | 单次接口调用超时，范围 1～7200 秒，默认 60 秒 |

除示例中的来源外，还可以使用 `{"source":"runtime","key":"user_id"}`；
运行时可用键为 `job_id`、`tenant_id`、`source_id`、`user_id`、
`scheduled_fire_at`、`batch_id`、`dispatch_attempt`。映射对象可加
`prefix`，例如把令牌映射到 Header 时设置 `"prefix":"Bearer "`。

## 3. 发布绑定

向 **SWE** 发送以下请求。按环境既有认证要求补充网关 Token 或 Cookie；
`X-Tenant-Id`、`X-User-Id` 使用配置人员自己的有效身份。

```bash
curl --request PUT \
  'https://<swe-host>/api/cron/workflow-bindings/<skill_id>' \
  --header 'X-Source-Id: RMASSIST' \
  --header 'X-Tenant-Id: <manager_tenant_id>' \
  --header 'X-User-Id: <manager_user_id>' \
  --header 'X-User-Role: manager' \
  --header 'Content-Type: application/json' \
  --data-binary @workflow-binding.json
```

成功时返回 `binding_id`、`version`、`skill_id` 和配置内容。相同
`X-Source-Id` + `skill_id` 再次 PUT 会发布**新版本**并切换当前版本；
不要把网络超时后的重复提交当作无副作用重试。固定 API-Key 会明文存于
`swe_workflow_binding_versions.config_json`，管理接口的响应也包含它。

## 4. 验证并创建任务

1. 使用相同的 `X-Source-Id` 和管理身份请求
   `GET /api/cron/workflow-bindings/<skill_id>`，核对 `binding_id`、
   `version` 和目标 URL。**不要转发完整响应**，其中含固定 API-Key。
2. 在 Console“定时任务”→“创建任务”中把任务类型选为“技能任务”，
   在“绑定技能ID”中把 `<skill_id>` 放在第一个。服务端会自动写入
   `workflow_binding_id`；Agent 和 text 任务不需要这个字段。
3. 目标用户的 `.secret/envs.json` 必须包含 `sapId`、`bbkOrgId`、
   `brnOrgId`、`rtlPstId`；其中 `sapId` 必须与任务的
   `dispatch.target.user_id` 完全相同。`cookie` 和 `token` 映射需要该用户
   有可用的 Cron 授权状态。
4. 先手动运行一次，检查任务会话结果、trace 和 `swe_cron_executions`。
   批调度任务还需 Scheduler 所连数据库的 `swe_cron_jobs.workflow_binding_id`
   列和任务行中的非空绑定 ID。

## 5. 常见错误

| 错误 | 核对项 |
| --- | --- |
| `Manager role required` | 请求的 `X-User-Role` 须为 `manager` 或 `admin` |
| `workflow configuration storage unavailable` | SWE 是否已连接数据库；启动时不再自动建表 |
| `workflow endpoint host is not approved` | SWE 的 `SWE_WORKFLOW_ALLOWED_HOSTS` 是否包含接口主机名；修改后重启 |
| `workflow binding is missing or disabled` | 来源系统、首个技能 ID 和已发布绑定是否一致 |
| `workflow sapId does not match target user` | 目标 scope 的 `envs.json.sapId` 与任务目标用户 ID 是否相同 |
| `workflow secret value missing: cookie` | 目标用户的 Cron 授权文件是否有效；过期时间不能是无效日期 |
| `Unknown column workflow_binding_id` | Scheduler 实际连接的数据库是否完成 `swe_cron_jobs` 补列迁移 |

更多执行机制见 [Workflow 类型定时任务](cron-workflow.md)。
