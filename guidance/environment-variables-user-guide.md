# 环境变量使用指南

环境变量用于保存运行 Shell 命令、MCP 服务或其他工具时需要使用的配置，例如 API Token、业务系统凭据、MCP Server 启动参数等。本文从用户功能出发说明如何通过界面、CLI 和 API 管理环境变量，以及这些变量在 MCP 和 Shell 中如何生效。

本文以当前代码实现为准。

## 1. 管理环境变量

环境变量按当前用户和当前来源分别保存。使用 Console 时，请求头由前端自动携带；在平台 Shell 中使用 `swe env` CLI 时，当前用户和来源由系统注入的运行时环境变量提供；直接调用 API 时，需要按平台认证方式携带当前用户和来源对应的请求头。

环境变量名必须满足：

```text
^[A-Za-z_][A-Za-z0-9_]*$
```

变量值必须是字符串。变量名区分大小写，例如 `TOKEN` 和 `token` 是两个不同变量。

以下变量名不能作为用户环境变量写入：

| 变量 | 含义 | 是否需要用户配置 | 使用位置 | 示例 |
| -- | -- | -- | -- | -- |
| `SWE_WORKING_DIR` | 系统工作目录 | 否 | 系统运行环境 | 不建议配置 |
| `SWE_SECRET_DIR` | 系统密钥目录 | 否 | 系统运行环境 | 不建议配置 |
| `PATH` | 可执行文件搜索路径 | 否 | Shell / 子进程 | 不建议配置 |
| `HOME` | 用户主目录 | 否 | Shell / 子进程 | 不建议配置 |
| `SHELL` | Shell 路径 | 否 | Shell / 子进程 | 不建议配置 |
| `BASH_ENV` | Bash 启动配置 | 否 | Shell / 子进程 | 不建议配置 |
| `ENV` | Shell 启动配置 | 否 | Shell / 子进程 | 不建议配置 |
| `ZDOTDIR` | Zsh 配置目录 | 否 | Shell / 子进程 | 不建议配置 |
| `IFS` | Shell 分隔符 | 否 | Shell / 子进程 | 不建议配置 |
| `CDPATH` | Shell 路径搜索配置 | 否 | Shell / 子进程 | 不建议配置 |
| `PYTHONPATH` | Python 模块搜索路径 | 否 | Python 子进程 | 不建议配置 |
| `PYTHONHOME` | Python 安装目录 | 否 | Python 子进程 | 不建议配置 |
| `LD_LIBRARY_PATH` | Linux 动态库路径 | 否 | 子进程 | 不建议配置 |
| `DYLD_LIBRARY_PATH` | macOS 动态库路径 | 否 | 子进程 | 不建议配置 |
| `SWE_STDIO_LAUNCHER_DROP_ENV_KEYS` | MCP stdio 启动器内部变量 | 否 | MCP stdio | 不建议配置 |
| `SWE_TENANT_ID` | 系统注入的用户/租户标识 | 否 | Shell / MCP stdio | 系统自动注入 |
| `SWE_SOURCE_ID` | 系统注入的来源标识 | 否 | Shell / MCP stdio | 系统自动注入 |
| `SWE_RUNTIME_SCOPE_ID` | 系统注入的运行范围标识 | 否 | Shell / MCP stdio | 系统自动注入 |
| `SWE_SESSION_ID` | 系统注入的会话标识 | 否 | Shell / MCP stdio | 系统自动注入 |
| `SWE_CHAT_ID` | 系统注入的对话标识 | 否 | Shell / MCP stdio | 系统自动注入 |
| `SWE_TRACE_ID` | 系统注入的链路追踪标识 | 否 | Shell / MCP stdio | 系统自动注入 |

### 查询环境变量

#### 通过界面查询

进入 Console 侧边栏的环境变量页面。页面会加载当前用户和当前来源下已经保存的变量。

变量值默认以密码输入框展示，可以点击眼睛图标显示或隐藏。当前代码中，普通查询接口会返回原始值，因此该页面应只对有权限的用户开放。

#### 通过 CLI 查询

```bash
swe env list
```

CLI 查询默认掩码非空值：

```text
Key                             Value
MCP_TOKEN                       ********
EMPTY
```

需要显示明文时，显式加 `--show-values`：

```bash
swe env list --show-values
```

CLI 从当前进程环境中的 `SWE_TENANT_ID` 和 `SWE_SOURCE_ID` 生成请求头。平台 Shell 会自动注入这两个变量；如果在没有这两个变量的普通终端中执行，命令会失败并提示缺少运行时标识。

CLI 默认连接 `http://127.0.0.1:8088`，并自动使用 `/api` 前缀。如服务运行在其他端口，可以使用全局参数：

```bash
swe --host 127.0.0.1 --port 8099 env list
```

#### 通过 API 查询

### 接口

`GET /api/envs`

### 请求参数

| 参数 | 类型 | 必填 | 说明 |
| -- | -- | -- | -- |
| 无请求体 | - | - | 查询当前请求用户和来源下的环境变量 |

### 请求示例

```json
{}
```

### 返回示例

```json
[
  {
    "key": "MCP_TOKEN",
    "value": "tenant-secret"
  },
  {
    "key": "SERVICE_REGION",
    "value": "cn"
  }
]
```

### 说明

返回值是按 `key` 排序的数组。普通 `GET /api/envs` 返回明文值，不会自动脱敏。CLI 的 `swe env list` 调用该接口，但默认在终端输出时掩码非空值。

### 新增和增量更新环境变量

#### 通过界面操作

在环境变量页面中可以：

- 点击“添加环境变量”新增变量。
- 修改已有变量的值。
- 勾选变量后批量删除。
- 点击保存提交本次新增和修改。
- 点击重置撤销尚未保存的本地改动。

已有变量的 Key 在当前界面中不可编辑。如果需要改名，需要删除旧变量后新增一个新变量。页面保存时调用 `PATCH /api/envs`，只提交新增或变更的变量；没有修改的变量不会被删除。

#### 通过 CLI 新增或更新

```bash
swe env set MCP_TOKEN tenant-secret
```

`swe env set` 新增或更新单个变量。成功时只输出变量名，不回显变量值：

```text
Saved: MCP_TOKEN
```

该命令从 `SWE_TENANT_ID` 和 `SWE_SOURCE_ID` 读取当前用户和来源，调用 `PATCH /api/envs`，请求体等价于：

```json
{
  "values": {
    "MCP_TOKEN": "tenant-secret"
  }
}
```

#### 通过 API 增量更新

### 接口

`PATCH /api/envs`

### 请求参数

| 参数 | 类型 | 必填 | 说明 |
| -- | -- | -- | -- |
| `values` | `object<string,string>` | 否 | 要新增或覆盖的变量。Key 不存在时新增，Key 已存在时更新值 |
| `delete` | `string[]` | 否 | 要删除的变量名。变量不存在时忽略 |
| `preserve` | `string[]` | 否 | 兼容字段。当前代码不会根据该字段改变结果 |

### 请求示例

```json
{
  "values": {
    "MCP_TOKEN": "new-secret",
    "SERVICE_REGION": "cn"
  },
  "delete": ["OLD_TOKEN"]
}
```

### 返回示例

```json
[
  {
    "key": "MCP_TOKEN",
    "value": "new-secret"
  },
  {
    "key": "SERVICE_REGION",
    "value": "cn"
  }
]
```

### 说明

`PATCH /api/envs` 是增量 merge 语义：

- `values` 中不存在的旧变量会保留。
- `values` 中已有的 Key 会被覆盖。
- `values` 中不存在的 Key 会被新增。
- `delete` 中的 Key 会从已有变量中删除；不存在则忽略。
- 如果同一个 Key 同时出现在 `delete` 和 `values`，代码会先删除再写入 `values`，最终该 Key 会保留为 `values` 中的新值。
- 普通 `PATCH /api/envs` 返回明文值。

#### 通过 API 全量替换

### 接口

`PUT /api/envs`

### 请求参数

| 参数 | 类型 | 必填 | 说明 |
| -- | -- | -- | -- |
| 任意合法变量名 | `string` | 否 | 请求体本身是环境变量键值对象 |

### 请求示例

```json
{
  "MCP_TOKEN": "tenant-secret",
  "SERVICE_REGION": "cn"
}
```

### 返回示例

```json
[
  {
    "key": "MCP_TOKEN",
    "value": "tenant-secret"
  },
  {
    "key": "SERVICE_REGION",
    "value": "cn"
  }
]
```

### 说明

`PUT /api/envs` 是全量替换语义。请求体中没有出现的旧变量会被删除。普通 `PUT /api/envs` 返回明文值。

普通 `PUT /api/envs` 请求体中不能包含 `tenant_id`、`source_id`、`target_tenant_id`、`target_source_id` 这些保留字段；如果需要为指定目标写入变量，请使用 `PUT /api/envs/target`。

当前 CLI 不提供全量替换命令。

### 删除环境变量

#### 通过界面删除

在环境变量页面中可以删除单个变量，也可以勾选多个变量后批量删除。删除已保存变量时，页面会在确认后立即调用删除接口并重新加载列表；删除尚未保存的新行只会移除本地输入，不会调用接口。

#### 通过 CLI 删除

```bash
swe env delete MCP_TOKEN
```

`swe env delete` 从 `SWE_TENANT_ID` 和 `SWE_SOURCE_ID` 读取当前用户和来源，删除单个变量，调用 `DELETE /api/envs/{key}`。成功时只输出被删除的变量名。服务端返回错误时，CLI 会显示错误并以非零退出码结束。

#### 通过 API 删除

### 接口

`DELETE /api/envs/{key}`

### 请求参数

| 参数 | 类型 | 必填 | 说明 |
| -- | -- | -- | -- |
| `key` | `string` | 是 | 要删除的环境变量名，放在 URL 路径中 |

### 请求示例

```json
{}
```

### 返回示例

```json
[
  {
    "key": "SERVICE_REGION",
    "value": "cn"
  }
]
```

### 说明

删除成功后返回剩余环境变量列表，普通删除接口返回明文值。要删除的 Key 不存在时，接口返回 `404`。

### 为指定用户和来源配置环境变量

该能力适合系统集成、批量预置或管理端代配置场景。调用方可以显式指定目标用户/租户标识和来源标识，把一组环境变量写入目标。

#### 通过 API 写入目标

### 接口

`PUT /api/envs/target`

### 请求参数

| 参数 | 类型 | 必填 | 说明 |
| -- | -- | -- | -- |
| `target_tenant_id` | `string` | 是 | 目标用户/租户标识。字段名以接口 Schema 为准 |
| `target_source_id` | `string` | 是 | 目标来源标识 |
| `values` | `object<string,string>` | 否 | 要写入目标的一组环境变量 |

### 请求示例

```json
{
  "target_tenant_id": "user-a",
  "target_source_id": "source-a",
  "values": {
    "MCP_TOKEN": "target-secret"
  }
}
```

### 返回示例

```json
{
  "envs": [
    {
      "key": "MCP_TOKEN",
      "value": "********"
    }
  ],
  "audit": {
    "actor": "manager-1",
    "target_tenant_id": "user-a",
    "target_source_id": "source-a",
    "keys": ["MCP_TOKEN"]
  }
}
```

### 说明

该接口要求请求头 `X-User-Role` 为 `manager` 或 `admin`，否则返回 `403`。

`PUT /api/envs/target` 会把 `values` 作为目标环境变量的完整集合写入。响应中的 `envs` 会脱敏，非空值显示为 `********`；审计信息只包含操作者、目标和写入的 Key，不包含原始值。

当前 CLI 不提供目标用户和来源写入命令。

## 2. 在 MCP 中使用环境变量

### MCP stdio 中使用环境变量

stdio 类型 MCP Server 启动时，系统会构造子进程环境变量：

- 继承允许传递的系统进程环境。
- 注入当前用户和来源下保存的环境变量。
- 再合并 MCP client 配置里的 `env` 字段。
- 最后注入系统运行时标识变量。

如果用户环境变量和 MCP client 的 `env` 中存在同名 Key，MCP client 的 `env` 值优先生效。受保护变量会被过滤或拒绝，不能通过用户配置覆盖系统运行边界。

#### MCP stdio 示例

先保存环境变量：

```bash
swe env set MCP_TOKEN tenant-secret
```

再配置 stdio MCP：

```json
{
  "mcpServers": {
    "demo-stdio": {
      "transport": "stdio",
      "command": "node",
      "args": ["server.js"],
      "env": {
        "SERVICE_REGION": "cn"
      }
    }
  }
}
```

启动 `node server.js` 时，子进程可以从环境变量中读取：

```text
MCP_TOKEN=tenant-secret
SERVICE_REGION=cn
```

如果 MCP 配置里也写了：

```json
{
  "env": {
    "MCP_TOKEN": "client-secret"
  }
}
```

则子进程最终读取到的 `MCP_TOKEN` 是 `client-secret`。

stdio 环境变量是在进程启动时注入的。MCP Server 已经启动后再修改环境变量，不会自动改变该进程已持有的环境变量；需要重启或重新加载对应 MCP 客户端。

### MCP HTTP Header 中使用环境变量

HTTP / SSE 类型 MCP 不会自动把所有用户环境变量放进 Header。只有在 MCP client 的 `headers` 中显式写出环境变量引用时，系统才会解析。

当前代码支持的用户环境变量引用语法是：

```text
${ENV:KEY}
```

例如 `${ENV:MCP_TOKEN}` 会读取当前用户和来源下保存的 `MCP_TOKEN`。不要把 `${MCP_TOKEN}` 或 `{{MCP_TOKEN}}` 当作用户环境变量引用语法。

#### MCP HTTP Header 示例

先保存环境变量：

```bash
swe env set MCP_TOKEN tenant-secret
```

再配置 HTTP MCP：

```json
{
  "mcpServers": {
    "demo-http": {
      "transport": "streamable_http",
      "url": "https://mcp.example.com/stream",
      "headers": {
        "Authorization": "Bearer ${ENV:MCP_TOKEN}",
        "X-Region": "cn"
      }
    }
  }
}
```

运行时，系统会把 Header 构造成：

```json
{
  "Authorization": "Bearer tenant-secret",
  "X-Region": "cn"
}
```

Header 普通字符串可以和环境变量引用拼接，例如 `Bearer ${ENV:MCP_TOKEN}`。如果变量不存在，`${ENV:MCP_TOKEN}` 会保持原样，不会替换为空字符串。

Header 解析还会先执行一次系统进程环境变量展开，例如 `${HOME}` 会按后端进程环境展开；之后再解析 `${ENV:KEY}`。通过 `${ENV:KEY}` 插入的用户环境变量值不会再进行第二次系统进程环境变量展开。

### MCP 环境变量常见问题

#### MCP stdio 和 MCP HTTP Header 的处理一致吗？

不一致。stdio 会在子进程启动时把用户环境变量注入到进程环境中；HTTP / SSE 只会解析 Header 中显式写出的 `${ENV:KEY}`。

#### 修改环境变量后什么时候生效？

新的 Shell 命令和新启动的 MCP stdio 进程会读取最新变量。已经启动的 MCP stdio 进程不会自动更新，需要重启或重新加载。MCP HTTP Header 会在构建或重建客户端 Header 时重新解析变量；代码中已有重建客户端时重新解析 Header 的行为。

#### 变量不存在会怎样？

MCP HTTP Header 中的 `${ENV:KEY}` 如果找不到对应变量，会保留原字符串。stdio 场景下，不存在的变量不会出现在子进程环境中，除非 MCP client 自己的 `env` 字段或系统进程环境提供了它。

#### 是否支持多个变量？

支持。环境变量接口接收多个 Key，MCP HTTP Header 的同一个字符串中也可以包含多个 `${ENV:KEY}` 引用。

#### 是否支持普通字符串和变量拼接？

MCP HTTP Header 支持普通字符串和 `${ENV:KEY}` 拼接，例如 `Bearer ${ENV:MCP_TOKEN}`。stdio 不是字符串替换机制，而是把变量作为子进程环境传递。

## 3. 默认环境变量

默认环境变量分为两类：用户自行配置的变量，以及系统运行时自动提供的变量。用户自行配置的变量来自环境变量页面、`swe env` CLI 或 `/api/envs` 接口；系统默认变量由运行时自动注入，用户不需要也不能通过环境变量接口配置同名 Key。

### Shell 默认环境变量

Shell 命令执行时，系统会为子进程准备环境变量。除用户自行配置的变量外，当前代码还会设置以下默认变量或默认值：

| 变量 | 含义 | 是否需要用户配置 | 使用位置 | 示例 |
| -- | -- | -- | -- | -- |
| `SWE_TENANT_ID` | 当前用户/租户标识 | 否 | Shell 子进程 | `user-a` |
| `SWE_SOURCE_ID` | 当前来源标识 | 否 | Shell 子进程 | `source-a` |
| `SWE_RUNTIME_SCOPE_ID` | 当前运行范围标识 | 否 | Shell 子进程 | 系统生成 |
| `SWE_SESSION_ID` | 当前会话 ID | 否 | Shell 子进程 | `session-1` |
| `SWE_CHAT_ID` | 当前对话 ID | 否 | Shell 子进程 | `chat-uuid-1` |
| `SWE_TRACE_ID` | 当前链路追踪 ID | 否 | Shell 子进程 | `trace-1` |
| `OPENBLAS_NUM_THREADS` | OpenBLAS 线程数默认值 | 否 | Shell 子进程 | `1` |
| `OMP_NUM_THREADS` | OpenMP 线程数默认值 | 否 | Shell 子进程 | `1` |
| `MKL_NUM_THREADS` | MKL 线程数默认值 | 否 | Shell 子进程 | `1` |
| `NUMEXPR_NUM_THREADS` | NumExpr 线程数默认值 | 否 | Shell 子进程 | `1` |
| `VECLIB_MAXIMUM_THREADS` | vecLib 最大线程数默认值 | 否 | Shell 子进程 | `1` |

线程数变量只在当前环境中没有同名值时设置为 `1`；如果系统进程环境已经显式提供了同名变量，Shell 子进程会保留已有值。

Shell 子进程的 `PATH` 会包含当前 Python 可执行文件所在目录，但 `PATH` 不能通过用户环境变量接口配置。

### MCP Tool Header 默认变量

MCP HTTP / SSE 调用时，系统会自动注入一组运行时 Header。用户不需要在 MCP 配置中手动填写这些 Header；如果用户配置或前端透传了同名 Header，系统会移除后再写入当前运行时的值。

| 变量 | 含义 | 是否需要用户配置 | 使用位置 | 示例 |
| -- | -- | -- | -- | -- |
| `x-swe-tenant-id` | 当前用户/租户标识 | 否 | MCP HTTP Header | `user-a` |
| `tenantid` | 当前用户/租户标识兼容 Header | 否 | MCP HTTP Header | `user-a` |
| `x-swe-source-id` | 当前来源标识 | 否 | MCP HTTP Header | `source-a` |
| `sourceid` | 当前来源标识兼容 Header | 否 | MCP HTTP Header | `source-a` |
| `x-swe-runtime-scope-id` | 当前运行范围标识 | 否 | MCP HTTP Header | 系统生成 |
| `x-swe-session-id` | 当前会话 ID | 否 | MCP HTTP Header | `session-1` |
| `sessionid` | 当前会话 ID 兼容 Header | 否 | MCP HTTP Header | `session-1` |
| `x-swe-chat-id` | 当前对话 ID | 否 | MCP HTTP Header | `chat-uuid-1` |
| `chatid` | 当前对话 ID 兼容 Header | 否 | MCP HTTP Header | `chat-uuid-1` |
| `x-swe-trace-id` | 当前链路追踪 ID | 否 | MCP HTTP Header | `trace-1` |
| `traceid` | 当前链路追踪 ID 兼容 Header | 否 | MCP HTTP Header | `trace-1` |

这些 Header 不是用户环境变量，不能通过 `/api/envs` 写入同名环境变量来覆盖。

## 4. 使用示例

### 示例 1：配置 Token 并用于 MCP HTTP Header

第一步，保存 Token：

```bash
swe env set MCP_TOKEN tenant-secret
```

也可以调用增量更新接口：

```http
PATCH /api/envs
```

```json
{
  "values": {
    "MCP_TOKEN": "tenant-secret"
  },
  "delete": []
}
```

第二步，配置 HTTP MCP Header：

```json
{
  "mcpServers": {
    "secure-http": {
      "transport": "streamable_http",
      "url": "https://mcp.example.com/stream",
      "headers": {
        "Authorization": "Bearer ${ENV:MCP_TOKEN}"
      }
    }
  }
}
```

第三步，实际调用时系统会把 Header 中的 `${ENV:MCP_TOKEN}` 替换为 `tenant-secret`，并发送：

```http
Authorization: Bearer tenant-secret
```

### 示例 2：在 MCP stdio 中使用环境变量

第一步，保存 MCP Server 需要读取的变量：

```bash
swe env set MCP_TOKEN tenant-secret
swe env set SERVICE_REGION cn
```

第二步，配置 stdio MCP：

```json
{
  "mcpServers": {
    "local-server": {
      "transport": "stdio",
      "command": "python",
      "args": ["server.py"]
    }
  }
}
```

第三步，系统启动 `python server.py` 时，会把 `MCP_TOKEN` 和 `SERVICE_REGION` 注入到该子进程环境中。Server 代码可以按普通环境变量方式读取，例如：

```python
import os

token = os.environ.get("MCP_TOKEN")
region = os.environ.get("SERVICE_REGION")
```

### 示例 3：通过接口为指定用户和来源预置环境变量

管理端可以为目标用户和来源预置变量：

```http
PUT /api/envs/target
```

```json
{
  "target_tenant_id": "user-a",
  "target_source_id": "source-a",
  "values": {
    "MCP_TOKEN": "preloaded-secret",
    "SERVICE_REGION": "cn"
  }
}
```

请求头需要包含：

```http
X-User-Role: manager
```

或：

```http
X-User-Role: admin
```

接口成功后，目标用户在对应来源下启动 Shell、MCP stdio，或在 MCP HTTP Header 中引用 `${ENV:MCP_TOKEN}` 时，会使用这次预置的变量。

## 5. 常见问题

### CLI 是直接改本地文件吗？

不是。当前 `swe env` 通过本机 Swe HTTP 服务调用 `/api/envs`，不会直接读写本地环境变量文件。

### CLI 如何确定操作哪个用户和来源？

当前 `swe env` 不提供 `--tenant-id` 或 `--source-id` 参数。它会读取当前进程环境中的 `SWE_TENANT_ID` 和 `SWE_SOURCE_ID`，并把它们作为请求头发送给 `/api/envs`。平台 Shell 会自动注入这两个值；如果缺少任一值，CLI 会报错并退出。

### CLI 查询返回的是明文还是脱敏值？

`swe env list` 默认掩码非空值。加 `--show-values` 才显示明文。普通 HTTP `GET /api/envs` 返回明文值。

### CLI 支持哪些操作？

当前支持 `list`、`set`、`delete`。`set` 是单 Key 增量新增或更新；`delete` 是单 Key 删除。当前 CLI 不提供全量替换、目标用户和来源写入，也不允许通过命令行参数覆盖当前运行时的用户和来源。

### 查询接口返回的是明文还是脱敏值？

普通 `GET /api/envs`、`PUT /api/envs`、`PATCH /api/envs`、`DELETE /api/envs/{key}` 返回明文值。`PUT /api/envs/target` 返回脱敏值。

### 更新环境变量会不会删除没有传入的 Key？

`PATCH /api/envs` 不会删除未传入的 Key。只有 `delete` 中列出的 Key 会被删除。`PUT /api/envs` 是全量替换，会删除请求体中没有出现的旧 Key。

### 删除是按 Key 删除还是整体删除？

`DELETE /api/envs/{key}` 和 `swe env delete` 都按单个 Key 删除。界面批量删除多个变量时，是对每个已保存变量分别调用删除接口。

### 修改环境变量后什么时候生效？

新的 Shell 命令和新启动的 MCP stdio 进程会读取最新值。已经启动的 MCP stdio 进程不会自动更新。MCP HTTP Header 会在构建或重建客户端 Header 时解析当前值。

### 环境变量不存在会怎样？

`PATCH /api/envs` 删除不存在的 Key 会忽略。`DELETE /api/envs/{key}` 或 `swe env delete` 删除不存在的 Key 会返回错误。MCP HTTP Header 中引用不存在的 `${ENV:KEY}` 会保持原样。

### 用户变量和默认变量冲突时谁优先？

用户不能写入受保护变量。对于允许写入的普通变量，运行子进程时用户保存的变量会覆盖同名系统进程环境；MCP client 自己的 `env` 字段会覆盖同名用户变量。系统运行时注入的 `SWE_*` 标识变量不能由用户变量覆盖。

### MCP stdio 能使用 `${ENV:KEY}` 吗？

当前代码只在 MCP HTTP Header 解析 `${ENV:KEY}`。MCP stdio 使用的是子进程环境变量注入，不是字符串替换。

### MCP HTTP Header 能直接使用 `${MCP_TOKEN}` 吗？

`${MCP_TOKEN}` 会走系统进程环境变量展开，不是用户环境变量引用。用户保存的环境变量应使用 `${ENV:MCP_TOKEN}`。

### 是否支持空值？

值必须是字符串，空字符串也是字符串。目标写入接口返回脱敏值时，非空值显示为 `********`，空字符串显示为空字符串。CLI 查询默认只掩码非空值。
