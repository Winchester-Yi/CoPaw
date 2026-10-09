# 应用市场 TDSQL 查询迁移设计

> 创建时间：2026-09-28
> 状态：设计已确认，待实施

## 1. 背景与目标

当前应用市场的技能和 MCP 元数据主要保存在 NAS 上的
`~/.swe.marketplace/<source_id>/index.json`，市场服务通过文件索引完成列表、
详情和分行筛选；TDSQL 仅保存分类、操作日志、技能统计配置以及租户持有状态。

本次改造采用：

> **TDSQL 元数据权威 + NAS 内容存储**

目标：

1. 应用市场技能列表、技能详情从 TDSQL 查询元数据。
2. 应用市场 MCP 列表、MCP 详情从 TDSQL 查询元数据。
3. `GET /market/browse` 和分行数量统计与列表查询使用同一 TDSQL 数据源。
4. 技能文件、MCP 配置文件、文件树、下载和版本历史继续存放并读取 NAS。
5. 保持现有 HTTP API 响应结构，Console 不需要感知存储迁移。

## 2. 范围

### 2.1 本期范围

- `GET /market/skills`
- `GET /market/skills/{item_id}`
- `GET /market/mcp`
- `GET /market/mcp/{item_id}`
- `GET /market/browse`
- 应用市场页面使用的分类/分行筛选与数量统计
- 技能发布、编辑、下架、删除时的市场元数据同步
- MCP 发布、上传、编辑、删除时的市场元数据同步
- 历史 `index.json` 数据迁移到 TDSQL

### 2.2 非本期范围

- “我创建的技能”和“我接收的技能”
- “我的 MCP”
- 分发记录、撤回记录和租户当前持有状态
- 技能/MCP 版本历史查询
- 技能文件树、技能文件下载
- MCP `mcp.json` 内容迁移
- 删除 NAS 内容存储
- Console API 路径和响应模型调整

其中，详情接口的元数据来自 TDSQL，但技能内容预览、文件树和 MCP 配置仍从
`content_path` 指向的 NAS 目录读取。敏感配置继续使用现有脱敏逻辑。

## 3. 当前实现与问题

当前关键链路：

```text
GET /market/skills
  -> skills_browse.list_skills
  -> MarketplaceService.list_skills
  -> load_index(index.json)
  -> Python 过滤

GET /market/mcp
  -> mcp_browse.list_market_mcp
  -> MarketplaceService.list_mcp_items
  -> load_index(index.json)
  -> Python 过滤

GET /market/browse
  -> market_browse.browse_market
  -> load_index(index.json)
  -> Python 计算分类/分行 facets
  -> MarketplaceService.list_skills/list_mcp_items
```

主要问题：

- 多实例部署时，文件索引的读写和缓存一致性依赖 NAS。
- 列表、分类统计和分行统计均需完整加载 `index.json`。
- 技能元数据分散在 `index.json` 和 `swe_marketplace_skills`，存在双重事实源。
- MCP 没有独立的市场元数据表，`swe_mcp_clients` 表记录的是租户实际持有的 MCP，
  不能作为市场目录。
- `index.json`、市场内容目录和数据库之间缺少可校验的一致性边界。

## 4. 目标架构

```text
                         ┌────────────────────┐
                         │ Console Market UI  │
                         └─────────┬──────────┘
                                   │ existing APIs
                         ┌─────────▼──────────┐
                         │ Market FastAPI     │
                         │ browse/detail     │
                         └──────┬───────┬────┘
                                │       │
                    metadata    │       │ content
                                │       │
                         ┌──────▼───┐ ┌─▼────────────────┐
                         │ TDSQL    │ │ NAS               │
                         │ metadata │ │ skill/MCP content │
                         └──────────┘ └──────────────────┘
```

职责边界：

| 数据 | 权威存储 | 说明 |
|---|---|---|
| item_id、名称、描述、版本 | TDSQL | 列表和详情元数据 |
| source_id、category_id、bbk_ids | TDSQL | 隔离和权限过滤 |
| status、删除/下架状态 | TDSQL | 查询可见性 |
| creator、时间、统计配置 | TDSQL | 展示和统计 |
| SKILL.md、skill.json、附件 | NAS | 详情内容、文件树、下载 |
| mcp.json | NAS | MCP 配置读取和脱敏 |
| index.json | 兼容缓存/迁移输入 | 不再作为查询权威源 |

## 5. 数据模型

### 5.1 技能市场表

继续使用现有 `swe_marketplace_skills` 表。需要核对线上实际表结构后，补齐以下
元数据字段：

```sql
ALTER TABLE swe_marketplace_skills
    ADD COLUMN description TEXT NULL,
    ADD COLUMN version VARCHAR(64) NOT NULL DEFAULT '1.0.0',
    ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'active',
    ADD COLUMN category_id BIGINT NULL,
    ADD COLUMN bbk_ids JSON NULL,
    ADD COLUMN content_path VARCHAR(512) NULL;
```

已有字段继续保留并作为查询字段：

- `source_id`
- `item_id`
- `skill_id`
- `skill_name`
- `cn_name`
- `include_in_statistics`
- `creator_id`
- `creator_name`
- `updator_id`
- `updator_name`
- `is_unpublished`
- `is_deleted`
- `created_at`
- `updated_at`

实现时不能假设开发库和生产库结构完全一致。迁移脚本需要先通过
`information_schema.columns` 检查字段，再执行对应的增量迁移；不能覆盖或删除
现有数据。

技能查询有效条件：

```sql
source_id = :source_id
AND COALESCE(is_unpublished, 0) = 0
AND COALESCE(is_deleted, 0) = 0
AND status = 'active'
```

### 5.2 MCP 市场表

新增 `swe_marketplace_mcps`，与 `swe_mcp_clients` 分离。
`swe_mcp_clients` 继续记录租户当前持有的 MCP，不参与市场目录查询。

```sql
CREATE TABLE IF NOT EXISTS swe_marketplace_mcps (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    item_id VARCHAR(64) NOT NULL,
    client_key VARCHAR(128) NOT NULL,
    name VARCHAR(256) NOT NULL,
    chinese_name VARCHAR(256) DEFAULT '',
    description TEXT,
    guidance TEXT,
    version VARCHAR(64) NOT NULL DEFAULT '1.0.0',
    creator_id VARCHAR(64) DEFAULT '',
    creator_name VARCHAR(256) DEFAULT '',
    category_id BIGINT NULL,
    bbk_ids JSON NULL,
    content_path VARCHAR(512) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_source_item (source_id, item_id),
    UNIQUE KEY uk_source_client_key (source_id, client_key),
    INDEX idx_source_status (source_id, status),
    INDEX idx_source_category (source_id, category_id),
    INDEX idx_source_name (source_id, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='应用市场 MCP 元数据';
```

说明：

- `content_path` 指向 NAS 上的 `mcp/<item_id>/mcp.json`。
- 不把 MCP 的完整 headers、env、token 等敏感配置写入 TDSQL。
- `bbk_ids` 延续当前 JSON 数组语义：空数组表示全员可见，`100` 表示总行可见。
- 第一阶段保留 JSON 字段，使用 TDSQL/MySQL 的 `JSON_CONTAINS` 过滤；
  暂不增加机构关系表。

### 5.3 条目状态

列表和详情只返回：

```text
status = 'active'
AND 未下架
AND 未删除
```

技能沿用 `is_unpublished`、`is_deleted` 兼容字段；MCP 使用 `status`，
删除时采用软删除状态。NAS 内容暂不随软删除立即清理。

## 6. 查询设计

### 6.1 Registry 层

新增或扩展以下数据访问对象：

```text
MarketSkillRegistry
  - list_market_skills(...)
  - get_market_skill(...)
  - upsert_market_skill(...)
  - mark_unpublished(...)
  - mark_deleted(...)
  - sync_from_index(...)

MarketMCPRegistry
  - list_market_mcps(...)
  - get_market_mcp(...)
  - upsert_market_mcp(...)
  - mark_deleted(...)
  - sync_from_index(...)
```

Registry 负责：

- SQL 和字段映射
- `source_id`、状态、分类、机构过滤
- 数据库行到 `MarketItem` 的转换
- 数据库不可用时返回明确异常，不静默返回空列表

Registry 不负责：

- 读取 SKILL.md 或 `mcp.json`
- 文件树和下载
- 用户分发
- 版本历史

### 6.2 技能列表与详情

`MarketplaceService.list_skills()` 改为：

```text
MarketSkillRegistry.list_market_skills()
  -> 应用总行/分行/分类过滤
  -> 查询技能调用统计
  -> 转换为 MarketSkillResponse
```

`get_skill_detail()` 改为：

```text
MarketSkillRegistry.get_market_skill()
  -> 校验可见性
  -> 查询调用统计和用户统计
  -> 返回 TDSQL 元数据
```

技能详情内容预览、文件树和下载仍按 `source_id + item_id` 从 NAS 读取。

### 6.3 MCP 列表与详情

`MarketplaceService.list_mcp_items()` 改为：

```text
MarketMCPRegistry.list_market_mcps()
  -> 应用总行/分行/分类过滤
  -> 查询 MCP 调用统计
  -> 转换为 MarketMCPItem
```

`get_mcp_detail()` 改为：

```text
MarketMCPRegistry.get_market_mcp()
  -> 校验可见性
  -> 从 content_path 读取 mcp.json
  -> normalize_mcp_config_data()
  -> 对 env/headers 脱敏
  -> 返回 TDSQL 元数据 + NAS 配置摘要
```

如果 TDSQL 条目存在但 NAS 内容缺失，详情接口返回 404 或 409 均可，
实施时统一为 404，避免向调用方暴露内部存储细节，并记录结构化错误日志。

### 6.4 `/market/browse`

`market_browse.py` 当前直接调用 `load_index()`，必须一并迁移。

改造后：

```text
查询基础市场条目
  -> Registry 从 TDSQL 获取 MarketItem
  -> 使用现有 browse.py 过滤函数计算可见集合
  -> 计算 categories / branches facets
  -> MarketplaceService 返回列表项
```

第一阶段可以继续使用 Python facet 计算，以最小化行为变化；
列表本身已经从 TDSQL 读取，不再读取 `index.json`。

后续如果条目数量较大，再将 facet 聚合下推到 SQL。

### 6.5 分行统计

`list_all_bbk_ids()` 也改为从 Registry 获取 active 市场条目。
否则市场页面的分行菜单会出现列表与数量不一致。

## 7. 写路径与一致性

本期虽然只迁移读路径，但必须同步调整产生市场元数据的写路径，
否则 TDSQL 很快会落后于 NAS。

### 7.1 发布/上传

```text
1. 校验请求和内容
2. 写入 NAS 内容目录
3. 在 TDSQL upsert 市场元数据
4. 更新 index.json 兼容缓存
5. 返回成功
```

数据库写入失败时：

- 不返回成功；
- 保留 NAS 内容，避免破坏已写入文件；
- 写入结构化 reconcile 日志；
- 由后台校验/补偿任务重新执行 TDSQL upsert。

### 7.2 编辑、下架和删除

- 编辑：更新 NAS 内容，再更新 TDSQL 元数据和 `index.json`。
- 下架：先更新 TDSQL 状态，再同步 `index.json`。
- 删除：TDSQL 软删除，`index.json` 同步移除或标记，NAS 内容保留。
- 版本历史目录不在本次迁移范围内，继续使用现有版本服务。

为了避免发布并发覆盖，沿用现有数据库连接的命名锁，
锁粒度建议为：

```text
marketplace:{source_id}:{item_type}:{item_id}
```

### 7.3 `index.json` 的新定位

迁移完成后：

- 不再用于市场列表、详情和 facet 查询；
- 继续作为兼容缓存和人工恢复材料；
- 写路径继续同步一段时间；
- 通过校验任务比较 TDSQL 和 `index.json`，确认稳定后再评估停止双写。

## 8. 历史数据迁移

新增一次性迁移命令或内部管理接口，支持：

- `source_id` 指定；
- `dry_run`；
- 技能/MCP 分开统计；
- 幂等 upsert；
- 缺失内容目录、非法字段和数据库错误明细。

迁移流程：

```text
扫描 index.json
  -> 校验 item_type 和必填字段
  -> 校验内容目录存在
  -> 写入对应 TDSQL 表
  -> 记录迁移结果
```

迁移验收要求：

```text
TDSQL 可见条目集合 == index.json 可见条目集合
```

至少比较：

- item_id
- item_type
- name
- version
- category_id
- bbk_ids
- status
- creator_id
- created_at
- updated_at

迁移完成后先进入双写阶段，再通过配置切换列表查询数据源。

## 9. 发布策略

### 阶段一：双写、文件读

- 发布/编辑/下架/删除同时写 TDSQL 和 NAS；
- 列表与详情仍可保持文件读；
- 运行一致性比对；
- 补齐历史数据。

### 阶段二：灰度 DB 读

- 选择指定 `source_id` 读取 TDSQL；
- 对比文件结果；
- 观察查询耗时、错误率和内容缺失；
- 不做静默 fallback，避免掩盖数据库问题。

### 阶段三：全量 DB 读

- 所有市场列表、详情、browse、分行统计读 TDSQL；
- NAS 仅承担内容读取；
- 保留 `index.json` 双写和校验。

### 阶段四：收敛

- 删除查询路径中的 `load_index()`；
- 保留迁移和修复工具；
- 根据运行结果决定是否停止 `index.json` 双写。

## 10. 错误处理与可观测性

### 数据库不可用

列表、详情、browse 返回 `503 Database unavailable`，不返回空列表。
空列表会误导用户认为市场没有数据，也会导致分发入口异常。

### TDSQL 有记录但 NAS 内容缺失

- 列表仍可返回元数据；
- 技能文件树/下载返回 404；
- MCP 详情返回 404；
- 记录 `marketplace_content_missing` 结构化日志，包含
  `source_id`、`item_id`、`item_type`、`content_path`。

### TDSQL 与 `index.json` 不一致

查询以 TDSQL 为准；一致性任务记录差异，不在请求链路自动覆盖数据库。

建议日志字段：

```text
event=marketplace_metadata_sync
source_id
item_type
item_id
operation
db_result
nas_result
reconcile_required
```

## 11. 测试设计

### Registry 单元测试

- source_id 隔离；
- active/inactive、下架、删除过滤；
- category_id 过滤；
- 总行和分行 `bbk_ids` 过滤；
- 空数组、`100` 和多分行数组；
- 数据库行到 `MarketItem` 的字段映射；
- DB 异常不静默返回空结果。

### Service 测试

- 技能列表和详情不再调用 `load_index()`；
- MCP 列表和详情不再调用 `load_index()`；
- MCP 详情仍从 NAS 读取配置并脱敏；
- NAS 内容不存在时的错误行为；
- 调用统计与 TDSQL 元数据组合；
- `/market/browse` 的列表、分类 facet、分行 facet 与服务列表一致；
- `list_all_bbk_ids()` 与列表结果口径一致。

### 迁移测试

- 技能和 MCP 历史数据可重复迁移；
- 缺失内容目录有明确错误；
- 非法 `bbk_ids`、时间和状态字段有错误明细；
- 迁移不覆盖已有正确记录；
- 迁移后 DB 查询结果与文件索引一致。

### 集成验收

至少覆盖：

1. 总行用户查询技能和 MCP。
2. 分行用户只能看到全员、总行和本分行可见条目。
3. 分类不可见时，普通用户不能通过 item_id 详情绕过。
4. Console 的技能和 MCP 市场页面接口响应结构不变。
5. 数据库异常返回 503。
6. DB 元数据存在、NAS 内容缺失时不会泄露内部路径。

## 12. 实施拆分

### 阶段 A：数据库和 Registry

- 新增 SQL 迁移；
- 扩展 `MarketSkillRegistry`；
- 新增 `MarketMCPRegistry`；
- 增加行到 `MarketItem` 的统一映射；
- 补充单元测试。

### 阶段 B：查询读路径

- 修改 `MarketplaceService.list_skills()`；
- 修改 `get_skill_detail()`；
- 修改 `list_mcp_items()`；
- 修改 `get_mcp_detail()`；
- 修改 `market_browse.py`；
- 修改 `list_all_bbk_ids()`；
- 补充 API 和集成测试。

### 阶段 C：写路径双写和迁移

- 发布、编辑、下架、删除同步 TDSQL；
- 增加历史迁移命令；
- 增加一致性校验；
- 运行双写和灰度验证。

## 13. 预计改造文件

核心文件：

```text
market/src/market/marketplace/service.py
market/src/market/marketplace/market_skill_registry.py
market/src/market/marketplace/mcp_registry.py
market/src/market/app/routers/market_browse.py
market/src/market/app/routers/skills_browse.py
market/src/market/app/routers/mcp_browse.py
market/src/market/app/routers/skills_market.py
market/src/market/app/routers/mcp_market.py
market/src/market/marketplace/models.py
scripts/sql/marketplace_tdsql_query_migration.sql
```

测试文件：

```text
market/tests/unit/marketplace/test_market_skill_registry.py
market/tests/unit/marketplace/test_market_mcp_registry.py
market/tests/unit/marketplace/test_skills_browse.py
market/tests/unit/marketplace/test_market_browse.py
market/tests/unit/market/test_mcp_models.py
```

预计前端只需要执行现有 API 回归测试，不修改接口协议。

## 14. 风险与控制

| 风险 | 控制措施 |
|---|---|
| 线上技能表结构与设计文档不一致 | 迁移前读取 `information_schema`，采用增量迁移 |
| 双写部分成功 | 保留 NAS 内容，记录 reconcile 事件，提供幂等补偿 |
| DB 和 NAS 元数据不一致 | TDSQL 作为查询权威，后台校验，不在请求内互相覆盖 |
| MCP 敏感配置泄露 | TDSQL 不存完整配置，详情继续从 NAS 读取并脱敏 |
| JSON `bbk_ids` 查询性能不足 | 第一阶段保留现状；数据量增长后再拆分关系表 |
| 列表与 facet 口径不一致 | `browse` 和 `list_all_bbk_ids` 统一复用 Registry 数据 |
| 多实例并发发布 | 使用现有 TDSQL 命名锁和幂等 upsert |

## 15. 验收标准

1. 应用市场技能/MCP列表不再依赖 `index.json`。
2. 应用市场技能/MCP详情元数据不再依赖 `index.json`。
3. `/market/browse` 和分行统计与市场列表使用同一 TDSQL 数据源。
4. 技能文件、MCP 配置和下载能力继续正常工作。
5. Console 不需要修改请求协议。
6. 历史索引数据完成迁移并通过一致性校验。
7. 数据库不可用、内容缺失和双写失败均有明确错误与日志。
