# 工作区文件批量分发接口

## 1. 接口用途

将当前请求所属 Agent 工作区中的一个文件复制到多个目标用户的同名 Agent 工作区。

源路径和目标路径均为相对于 Agent 工作区的路径。接口支持部分成功：某个目标复制失败时，仍会继续处理后续目标，并在响应中逐项返回结果。

## 2. 基本信息

| 项目 | 内容 |
|------|------|
| HTTP Method | `POST` |
| URL | `/api/files/distribute` |
| Content-Type | `application/json` |
| OpenAPI Tag | `files` |

请求体和响应体字段统一使用 camelCase。HTTP Header 保持平台既有的 `X-*-Id` 命名约定。

## 3. 工作区定位规则

### 3.1 源工作区

当前用户、来源和 Agent 由请求 Header 及现有中间件解析：

```text
<WORKING_DIR>/<当前 scopeId>/workspaces/<当前 agentId>
```

接口不会接受请求体中的源 `tenantId`、`sourceId`、`scopeId` 或 `agentId` 来覆盖当前请求身份。

### 3.2 目标工作区

每个目标由请求体中的 `tenantId` 和 `sourceId` 定位。服务端通过两者生成规范的 `scopeId`，目标 Agent 与源 Agent 相同：

```text
scopeId = encode_scope_id(tenantId, sourceId)
<WORKING_DIR>/<目标 scopeId>/workspaces/<当前 agentId>/<targetPath>
```

调用方不得传入 `scopeId`。目标 Agent 工作区必须已经存在；接口不会自动初始化目标用户或创建 Agent 工作区。

## 4. 请求 Header

| Header | 必填 | 说明 |
|--------|------|------|
| `X-Tenant-Id` | 是 | 当前逻辑租户或用户标识。 |
| `X-Source-Id` | 是 | 当前来源标识，与 `X-Tenant-Id` 一起生成当前运行时 `scopeId`。 |
| `X-Agent-Id` | 否 | 指定源 Agent；未传时使用当前租户配置中的激活 Agent，最后回退到 `default`。 |
| `X-User-Id` | 否 | 当前操作用户标识，按部署环境的调用链传入。 |
| `Authorization` | 视部署配置 | 当部署启用认证时，按现有认证方式传入。 |

## 5. 请求体

```json
{
  "sourcePath": "exports/report.txt",
  "targets": [
    {
      "tenantId": "target-user-a",
      "sourceId": "source-a"
    },
    {
      "tenantId": "target-user-b",
      "sourceId": "source-a"
    }
  ],
  "targetPath": "inbox/daily-report.txt"
}
```

### 5.1 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sourcePath` | `string` | 是 | 源文件相对于当前 Agent 工作区的路径。必须指向已存在的普通文件。 |
| `targets` | `array` | 是 | 目标用户列表，至少包含一项；`tenantId + sourceId` 生成的 `scopeId` 不得重复。 |
| `targets[].tenantId` | `string` | 是 | 目标逻辑租户或用户标识。 |
| `targets[].sourceId` | `string` | 是 | 目标来源标识，与 `tenantId` 一起用于生成目标 `scopeId`。 |
| `targetPath` | `string` | 是 | 目标文件相对于目标 Agent 工作区的路径。所有目标使用同一个相对路径。 |

### 5.2 路径约束

`sourcePath` 和 `targetPath` 必须满足以下规则：

- 使用 `/` 分隔的非空相对路径。
- 不得是绝对路径。
- 不得包含 `..`、反斜杠、冒号或控制字符。
- 不得仅为 `.`。
- 源路径必须是普通文件，不能是目录或符号链接。
- 解析后的源路径和目标路径必须位于各自的 Agent 工作区内。
- 目标路径不能是符号链接；目标已存在时必须是普通文件。

## 6. 成功响应

### 6.1 全部成功

HTTP Status：`200 OK`

```json
{
  "sourcePath": "exports/report.txt",
  "targetPath": "inbox/daily-report.txt",
  "agentId": "default",
  "results": [
    {
      "tenantId": "target-user-a",
      "sourceId": "source-a",
      "scopeId": "dGFyZ2V0LXVzZXItYQ.c291cmNlLWE",
      "success": true,
      "error": ""
    },
    {
      "tenantId": "target-user-b",
      "sourceId": "source-a",
      "scopeId": "dGFyZ2V0LXVzZXItYg.c291cmNlLWE",
      "success": true,
      "error": ""
    }
  ]
}
```

### 6.2 响应字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `sourcePath` | `string` | 规范化后的源相对路径。 |
| `targetPath` | `string` | 规范化后的目标相对路径。 |
| `agentId` | `string` | 本次复制使用的 Agent ID；所有目标使用同一 Agent ID。 |
| `results` | `array` | 与请求目标一一对应的处理结果，顺序与请求一致。 |
| `results[].tenantId` | `string` | 目标 tenant ID。 |
| `results[].sourceId` | `string` | 目标来源 ID。 |
| `results[].scopeId` | `string` | 服务端生成的规范目标 scope ID。 |
| `results[].success` | `boolean` | 当前目标是否复制成功。 |
| `results[].error` | `string` | 成功时为空字符串；失败时为不包含文件内容的错误摘要。 |

## 7. 部分成功

目标工作区不存在、目标路径越界或目标文件写入失败属于目标级错误。接口仍返回 `200 OK`，并继续处理后续目标。

```json
{
  "sourcePath": "exports/report.txt",
  "targetPath": "inbox/daily-report.txt",
  "agentId": "default",
  "results": [
    {
      "tenantId": "missing-user",
      "sourceId": "source-a",
      "scopeId": "bWlzc2luZy11c2Vy.c291cmNlLWE",
      "success": false,
      "error": "Target agent workspace does not exist"
    },
    {
      "tenantId": "target-user-a",
      "sourceId": "source-a",
      "scopeId": "dGFyZ2V0LXVzZXItYQ.c291cmNlLWE",
      "success": true,
      "error": ""
    }
  ]
}
```

调用方必须检查每一项的 `success`，不能只根据 HTTP `200` 判断所有目标均已复制成功。

## 8. 请求级错误

请求级错误会在开始复制前直接返回，不产生本次请求的目标文件写入。

| HTTP Status | 场景 | 响应示例 |
|-------------|------|----------|
| `400` | 缺少必需的租户或来源 Header。 | `{"detail":"X-Tenant-Id header is required"}` |
| `403` | 当前 Agent 被禁用，或当前 Agent 工作区不在允许范围内。 | `{"detail":"Agent 'default' is disabled"}` |
| `404` | 源文件不存在，或当前 Agent 不存在。 | `{"detail":"Source file not found"}` |
| `422` | 请求模型、camelCase 参数、相对路径、目标身份或源文件类型不合法。 | `{"detail":"Invalid target sourceId"}` |
| `503` | 当前租户工作区或租户池不可用。 | `{"detail":"Tenant workspace is unavailable"}` |

常见 `422` 原因包括：

- `targets` 为空。
- `sourcePath` 或 `targetPath` 不是合法相对路径。
- 目标 `tenantId` 或 `sourceId` 格式非法或缺失。
- 请求使用旧 snake_case 参数。
- `targets[]` 显式传入了不允许的 `scopeId` 或其他额外参数。
- 请求中包含生成结果相同的重复目标 `scopeId`。
- 源路径指向目录、符号链接或工作区外部。

FastAPI/Pydantic 参数校验失败时，`detail` 可能是结构化错误数组，而不是字符串。

## 9. 目标级错误摘要

以下错误通过 `results[].error` 返回，HTTP Status 仍为 `200`：

| 错误摘要 | 说明 |
|----------|------|
| `Target scope directory is unavailable` | 目标 scope 目录解析后超出允许的工作根目录。 |
| `Target agent workspace is unavailable` | 目标 Agent 工作区解析后超出目标 scope 的 `workspaces` 目录。 |
| `Target agent workspace does not exist` | 目标 Agent 工作区尚未创建。 |
| `Target path escapes the agent workspace` | 目标路径通过路径解析或符号链接逃逸工作区。 |
| `Target path must not be a symbolic link` | 目标文件本身是符号链接。 |
| `Target path must reference a regular file` | 已存在的目标路径不是普通文件。 |
| `Failed to copy file` | 发生权限、磁盘或其他文件系统写入错误。详细堆栈仅记录在服务日志中。 |

## 10. 覆盖与一致性语义

- 目标父目录不存在时会自动创建。
- 目标普通文件已存在时会被替换。
- 单个目标采用“同目录临时文件写入后替换”的方式，避免直接截断已有硬链接文件。
- 多目标之间不提供事务和回滚：较早目标成功后，较晚目标失败不会撤销此前结果。
- 目标按请求顺序处理，不保证多个目标同时完成。

## 11. cURL 示例

```bash
curl -X POST 'http://127.0.0.1:8088/api/files/distribute' \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: source-user' \
  -H 'X-Source-Id: source-a' \
  -H 'X-Agent-Id: default' \
  -H 'Authorization: Bearer <token>' \
  --data-raw '{
    "sourcePath": "exports/report.txt",
    "targets": [
      {
        "tenantId": "target-user-a",
        "sourceId": "source-a"
      },
      {
        "tenantId": "target-user-b",
        "sourceId": "source-a"
      }
    ],
    "targetPath": "inbox/daily-report.txt"
  }'
```

如果部署未启用 Bearer Token 认证，可按部署约定省略或替换 `Authorization` Header。
