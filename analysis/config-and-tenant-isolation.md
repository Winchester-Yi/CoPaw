# 配置体系与租户隔离

本文档聚焦请求级目录、配置模型、多租户上下文与 Provider 配置隔离。

## 核心目录与文件

| 区域 | 关键文件 | 说明 |
|------|----------|------|
| 常量与请求上下文 | `src/swe/constant.py` | 提供 `contextvars` 级别的用户目录切换与路径助手 |
| 环境默认值引导 | `src/swe/env_defaults.py` | 启动早期按 `SWE_ENV` 将 `dev/prd` 默认值注入 `os.environ` |
| 配置模型 | `src/swe/config/config.py` | 配置主模型 |
| 配置工具 | `src/swe/config/utils.py` | 配置读写、路径工具、租户辅助逻辑 |
| 配置上下文 | `src/swe/config/context.py` | 配置上下文支持 |
| 环境配置 | `src/swe/config/envs/dev.json`, `src/swe/config/envs/prd.json` | 环境差异化配置 |
| 时区 | `src/swe/config/timezone.py` | 时区探测 |
| 租户模型 | `src/swe/tenant_models/*.py` | 租户配置模型、管理器、异常、上下文 |
| 租户接入 | `src/swe/app/tenant_context.py`, `src/swe/app/middleware/tenant_identity.py`, `src/swe/app/middleware/tenant_workspace.py` | 请求到租户上下文的绑定链路 |
| 租户初始化 | `src/swe/app/workspace/tenant_initializer.py`, `src/swe/app/workspace/tenant_pool.py` | 新租户目录准备和工作区池化 |
| 环境变量持久化 | `src/swe/envs/store.py` | 环境变量文件存取 |

## 请求级目录助手

`src/swe/constant.py` 是多租户目录隔离的关键入口，包含以下核心函数：

- `set_request_user_id(user_id)`
- `get_request_working_dir()`
- `get_request_secret_dir()`
- `get_active_skills_dir()`
- `get_memory_dir()`
- `get_models_dir()`

## 环境配置加载顺序

- `src/swe/__init__.py` 启动时先调用 `src/swe/envs/store.py` 中的 `load_envs_into_environ()`，加载持久化的 `envs.json`
- 随后调用 `src/swe/env_defaults.py` 中的 `load_env_defaults()`，根据 `SWE_ENV` 从 `src/swe/config/envs/dev.json` 或 `src/swe/config/envs/prd.json` 补充默认值
- `src/swe/constant.py` 本身不直接读取 `dev.json` / `prd.json`，它消费的是已经合并后的 `os.environ`
- 如果某个常量值与 `dev/prd` 不一致，优先检查是否有更早的进程环境变量、`envs.json`，或模块导入时机过早导致常量已固化

## 租户目录结构

```text
~/.swe/
├── <tenant>/
│   ├── config.json
│   ├── active_skills/
│   ├── customized_skills/
│   ├── memory/
│   ├── models/
│   └── sessions/
```

```text
~/.swe.secret/
├── <tenant>/
│   └── providers/
│       ├── builtin/
│       ├── custom/
│       └── active_model.json
```

## 关键集成点

| 文件 | 作用 |
|------|------|
| `src/swe/app/runner/runner.py` | 在执行期消费请求上下文 |
| `src/swe/app/workspace/workspace.py` | 挂接租户相关服务 |
| `src/swe/providers/provider_manager.py` | 以租户为粒度返回 ProviderManager |
| `src/swe/cli/app_cmd.py` | 支持 CLI/单用户模式切换 |

## Source 系统配置

Source 系统配置用于按 `source_id` 保存系统级配置，不写入 tenant
`config.json`，也不参与 `bbk_id`、用户或机构覆盖。它和现有
tenant/source 运行时目录隔离是两层能力：目录隔离保证数据不串，source
系统配置让请求进入后可以读取当前接入系统的配置。

### 存储表

建表 SQL 位于 `scripts/sql/source_system_config.sql`：

```sql
CREATE TABLE IF NOT EXISTS swe_source_system_config (
    source_id VARCHAR(64) NOT NULL,
    config_json JSON NOT NULL,
    version BIGINT NOT NULL DEFAULT 1,
    updated_by VARCHAR(128) DEFAULT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id)
);
```

### 后端入口

| 文件 | 作用 |
|------|------|
| `src/swe/app/source_system_config/models.py` | Pydantic 模型、默认空配置和 JSON object 校验 |
| `src/swe/app/source_system_config/store.py` | MySQL 读写与 JSON/schema 校验 |
| `src/swe/app/source_system_config/service.py` | 默认值合成、TTL 缓存、last-known-good fallback |
| `src/swe/app/source_system_config/middleware.py` | 请求级加载并绑定到 `request.state` 与 ContextVar |
| `src/swe/app/source_system_config/runtime.py` | `get_current_source_system_config()` 和上下文绑定 helper |
| `src/swe/app/source_system_config/router.py` | effective config API 与 manager CRUD 管理 API |

### 默认行为与失败行为

- 缺少 source 配置记录时返回内置默认空配置；当前默认不改变既有业务行为。
- source 配置根对象必须是 JSON object；具体 key 和业务含义由后续开发人员在消费点决定。
- 管理接口支持按 `source_id` 创建、读取、更新、删除和列表查询配置。
- DB 读取失败且已有 last-known-good 缓存时继续使用缓存，并在返回配置上标记 `stale=true` 与 `last_error`。
- DB 读取失败且没有缓存时，effective config API 返回错误，不静默返回默认配置。

### 已接入业务 key

`follow_up_suggestions` 是第一个 source 系统配置消费方，用于控制回答后的“猜你想问”功能：

```json
{
  "follow_up_suggestions": {
    "enabled": true,
    "prompt_template": "根据以下对话，生成{max_count}个用户可能想问的后续问题..."
  }
}
```

- `enabled=false` 时，当前 source 下后端不再生成尾随建议。
- `prompt_template` 由后端 `generate_suggestions()` 使用，支持 `{max_count}`、`{user_message}`、`{assistant_response}`。
- 缺少该 key 时保持默认启用和内置 prompt，避免影响未配置 source 的既有行为。

## 关联功能域

- 模型与 Provider 运行栈: [model-provider-and-local-runtime.md](model-provider-and-local-runtime.md)
- 安全边界与路径防护: [security-and-governance.md](security-and-governance.md)
