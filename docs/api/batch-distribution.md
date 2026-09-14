# 技能和 MCP 批量分发接口

## 1. 功能说明

管理员通过一次请求，将市场中的多个技能和 MCP 分发给多个目标租户。接口按
item 粒度创建异步任务：

- 每个技能 item 对应一个异步任务。
- 每个 MCP item 对应一个异步任务。
- 一个异步任务内部包含多个目标租户的执行明细。
- 同一批次的所有任务通过 `batch_id` 关联，并可通过批量查询接口统一查看状态。

接口前缀为 `/api`，请求和响应均使用 JSON。

## 2. 公共请求头

两个接口都需要以下请求头：

| Header | 必填 | 说明 |
|---|---:|---|
| `X-Source-Id` | 是 | 市场来源 ID，用于隔离数据和任务查询范围。 |
| `X-Manager` | 是 | 管理员标识，必须为字符串 `true`。 |

提交接口还支持以下操作人信息请求头：

| Header | 必填 | 说明 |
|---|---:|---|
| `X-User-Id` | 否 | 发起分发操作的用户 ID。 |
| `X-User-Name` | 否 | 发起分发操作的用户名称。 |

## 3. 提交批量分发任务

### 3.1 基本信息

```text
POST /api/market/distributions
Content-Type: application/json
```

### 3.2 请求体

```json
{
  "batch_id": "batch-20260911-001",
  "skill_item_ids": [
    "skill-item-001",
    "skill-item-002"
  ],
  "mcp_item_ids": [
    "mcp-item-001"
  ],
  "target_tenant_ids": [
    "tenant-a",
    "tenant-b"
  ],
  "overwrite": true
}
```

### 3.3 请求字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `batch_id` | `string` | 是 | 批次 ID，长度为 1～128 个字符。调用方负责保证业务上的唯一性。 |
| `skill_item_ids` | `string[]` | 否 | 要分发的技能市场 item ID 列表。 |
| `mcp_item_ids` | `string[]` | 否 | 要分发的 MCP 市场 item ID 列表。 |
| `target_tenant_ids` | `string[]` | 是 | 目标租户 ID 列表。 |
| `overwrite` | `boolean` | 否 | MCP 分发时是否覆盖目标租户已有配置，默认值为 `true`。技能分发沿用现有技能分发逻辑。 |

约束：

- `skill_item_ids` 和 `mcp_item_ids` 不能同时为空。
- `target_tenant_ids` 不能为空。
- item ID 和目标租户 ID 会去除空白、去重并排序后参与处理。
- 技能 item 必须属于当前 `X-Source-Id` 下的市场数据。
- MCP item 必须属于当前 `X-Source-Id` 下的市场数据。

### 3.4 成功响应

HTTP 状态码：`200 OK`

```json
{
  "batch_id": "batch-20260911-001",
  "status": "queued",
  "reused": false,
  "task_ids": [
    "3f7d2c4a-1d6e-5f78-9a10-111111111111",
    "6a8e4b2c-3f10-5d92-8c44-222222222222",
    "8b9c0d1e-4a20-5e63-7f55-333333333333"
  ],
  "tasks": [
    {
      "task_id": "3f7d2c4a-1d6e-5f78-9a10-111111111111",
      "resource_type": "skill",
      "item_id": "skill-item-001",
      "status": "queued"
    },
    {
      "task_id": "6a8e4b2c-3f10-5d92-8c44-222222222222",
      "resource_type": "skill",
      "item_id": "skill-item-002",
      "status": "queued"
    },
    {
      "task_id": "8b9c0d1e-4a20-5e63-7f55-333333333333",
      "resource_type": "mcp",
      "item_id": "mcp-item-001",
      "status": "queued"
    }
  ]
}
```

### 3.5 响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `batch_id` | `string` | 本次批次 ID。 |
| `status` | `string` | 批次当前状态。首次提交通常为 `queued`。 |
| `reused` | `boolean` | 是否复用了已存在的同批次任务。 |
| `task_ids` | `string[]` | 本批次生成或复用的全部异步任务 ID。 |
| `tasks` | `object[]` | 任务列表。 |
| `tasks[].task_id` | `string` | 异步任务 ID。 |
| `tasks[].resource_type` | `string` | 资源类型：`skill` 或 `mcp`。 |
| `tasks[].item_id` | `string` | 市场 item ID。 |
| `tasks[].status` | `string` | 当前任务状态。 |

提交接口只负责创建任务并返回任务 ID，不等待所有目标租户执行完成。

## 4. 批量查询分发状态

### 4.1 基本信息

```text
GET /api/market/distributions?batchids=batch-20260911-001&batchids=batch-20260911-002
```

`batchids` 支持重复传入，用于一次查询多个批次。重复的批次 ID 会被去重，
返回顺序按照请求中首次出现的顺序排列。

### 4.2 成功响应

HTTP 状态码：`200 OK`

```json
{
  "batches": [
    {
      "batch_id": "batch-20260911-001",
      "status": "partial_failed",
      "task_ids": [
        "3f7d2c4a-1d6e-5f78-9a10-111111111111",
        "8b9c0d1e-4a20-5e63-7f55-333333333333"
      ],
      "tasks": [
        {
          "task_id": "3f7d2c4a-1d6e-5f78-9a10-111111111111",
          "resource_type": "skill",
          "item_id": "skill-item-001",
          "status": "succeeded"
        },
        {
          "task_id": "8b9c0d1e-4a20-5e63-7f55-333333333333",
          "resource_type": "mcp",
          "item_id": "mcp-item-001",
          "status": "failed"
        }
      ],
      "total_task_count": 2,
      "done_task_count": 2,
      "failed_task_count": 1
    },
    {
      "batch_id": "batch-20260911-002",
      "status": "queued",
      "task_ids": [],
      "tasks": [],
      "total_task_count": 0,
      "done_task_count": 0,
      "failed_task_count": 0
    }
  ]
}
```

不存在的批次不会返回 `404`，而是在 `batches` 中返回对应的空批次项。

### 4.3 查询响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `batches` | `object[]` | 批次查询结果列表。 |
| `batches[].batch_id` | `string` | 批次 ID。 |
| `batches[].status` | `string` | 批次汇总状态。 |
| `batches[].task_ids` | `string[]` | 批次中的全部任务 ID。 |
| `batches[].tasks` | `object[]` | 批次中的任务明细。 |
| `batches[].total_task_count` | `integer` | 任务总数。 |
| `batches[].done_task_count` | `integer` | 已完成任务数，包括成功、失败和部分失败任务。 |
| `batches[].failed_task_count` | `integer` | 失败或部分失败任务数。 |

查询范围按 `X-Source-Id` 隔离，只能查询当前来源下的批次。

## 5. 状态说明

| 状态 | 含义 |
|---|---|
| `queued` | 任务已创建，等待执行；批次中所有任务均未开始时，批次为此状态。 |
| `running` | 至少有一个任务正在执行，且批次尚未全部结束。 |
| `succeeded` | 批次中的所有任务均执行成功。 |
| `failed` | 批次中的任务全部执行失败，或单个任务的所有目标均失败。 |
| `partial_failed` | 批次中同时存在成功和失败任务，或单个任务只完成了部分目标。 |

单个任务的目标租户执行结果还会记录在统一异步任务明细表中，可结合现有异步任务查询能力查看目标级明细。

## 6. 幂等规则

提交接口支持按批次幂等：

1. 幂等范围为 `X-Source-Id + batch_id`。
2. 请求内容包括技能 item 列表、MCP item 列表、目标租户列表和 `overwrite`。
3. 列表会先去重、排序，再参与幂等判断，因此同一批次仅调整列表顺序不会产生新任务。
4. 相同来源、相同 `batch_id` 且请求内容一致时：
   - 不会重复创建异步任务。
   - 返回原任务 ID。
   - `reused` 返回 `true`。
5. 相同来源、相同 `batch_id` 但请求内容不一致时，返回 `409 Conflict`。
6. 不同 `X-Source-Id` 下可以使用相同的 `batch_id`，互不影响。

建议调用方为每个业务批次生成稳定且唯一的 `batch_id`，并在网络超时或收到未知结果时使用原请求重试。

## 7. 错误码

| HTTP 状态码 | 场景 |
|---:|---|
| `400` | `batch_id` 为空、未提供任何技能/MCP item，或未提供目标租户。 |
| `403` | 未提供 `X-Manager: true`。 |
| `404` | 请求中的技能或 MCP item 不存在，或不属于当前来源。 |
| `409` | 已存在相同来源和 `batch_id`，但本次请求内容不同。 |
| `422` | 请求参数类型或格式不符合接口模型。 |
| `503` | 异步任务数据库连接不可用。 |

错误响应通常为 FastAPI 标准格式：

```json
{
  "detail": "batch_id already exists with a different request"
}
```

## 8. cURL 示例

### 8.1 提交批量分发

```bash
curl -X POST 'http://127.0.0.1:8088/api/market/distributions' \
  -H 'Content-Type: application/json' \
  -H 'X-Source-Id: source-a' \
  -H 'X-Manager: true' \
  -H 'X-User-Id: manager-001' \
  -H 'X-User-Name: 管理员' \
  --data-raw '{
    "batch_id": "batch-20260911-001",
    "skill_item_ids": ["skill-item-001", "skill-item-002"],
    "mcp_item_ids": ["mcp-item-001"],
    "target_tenant_ids": ["tenant-a", "tenant-b"],
    "overwrite": true
  }'
```

### 8.2 批量查询状态

```bash
curl -G 'http://127.0.0.1:8088/api/market/distributions' \
  -H 'X-Source-Id: source-a' \
  -H 'X-Manager: true' \
  --data-urlencode 'batchids=batch-20260911-001' \
  --data-urlencode 'batchids=batch-20260911-002'
```

## 9. 数据存储说明

批次关系复用统一异步任务表，不单独创建批次关系表：

- `swe_async_tasks.batch_id` 保存批次 ID。
- `idx_async_tasks_batch_id` 用于按批次查询。
- 任务与目标租户的执行明细仍保存在 `swe_async_task_items`。

因此，一个批次可以通过 `batch_id` 关联多个异步任务，而无需维护额外的映射表。
