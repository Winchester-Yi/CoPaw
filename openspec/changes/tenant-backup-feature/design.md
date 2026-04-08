## Context

CoPaw 项目需要在 Kubernetes 容器多实例部署环境中支持租户数据的备份和恢复。参考项目 CoPaw-zhaohu 已实现了基于用户隔离的备份功能，但当前项目使用的是租户隔离架构，需要进行适配。

### 参考项目备份功能特点

1. **用户级备份**: 每个用户独立打包为 zip 文件
2. **S3 存储**: 路径结构 `{prefix}/{instance_id}/{YYYY-MM-DD}/{HH}/{user_id}.zip`
3. **配置文件加载**: 从 `~/.copaw/backup.json` 加载 S3 配置
4. **多实例批量操作**: 支持对多个容器实例执行批量备份/恢复
5. **回滚支持**: 恢复前备份当前数据，失败时可回滚

### 当前项目架构差异

| 维度 | 参考项目 | 当前项目 |
|------|----------|----------|
| 隔离单位 | `user_id` | `tenant_id` |
| 工作目录 | `WORKING_DIR / user_id` | `WORKING_DIR / tenant_id` |
| 密钥目录 | `SECRET_DIR / user_id` | `WORKING_DIR / tenant_id / ".secret"` |
| Provider 配置 | `SECRET_DIR / user_id / providers` | `SECRET_DIR / tenant_id / providers` |

## Goals / Non-Goals

**Goals:**
- 实现租户级别的数据备份和恢复
- 备份包含三个目录：工作目录、密钥目录、Provider 配置目录
- 从环境变量加载 S3 配置（支持 dev/prd 环境区分）
- 支持多实例部署的批量备份管理
- 支持恢复时的回滚机制

**Non-Goals:**
- 不修改现有的租户隔离架构
- 不引入新的依赖（除了 boto3 用于 S3 操作）
- 不支持增量备份（全量备份）

## Decisions

### 决策 1: 复用参考项目代码，最小化修改

**选择**: 直接复制参考项目的备份模块，仅修改必要的适配点

**理由**:
- 参考项目功能已验证，减少开发风险
- 最小化修改降低引入 bug 的可能性
- 保持代码风格一致性

**修改点**:
1. 字段名 `user_id` → `tenant_id`
2. 压缩/解压逻辑适配三目录结构
3. 配置加载从环境变量读取

### 决策 2: 配置从环境变量加载

**选择**: 使用 `dev.json`/`prd.json` 存储 S3 凭证，通过环境变量读取

**理由**:
- 与现有配置管理方式一致
- 敏感信息不存储在代码仓库
- 支持多环境（dev/prd）配置隔离

**环境变量**:
```
COPAW_BACKUP_AWS_ACCESS_KEY_ID
COPAW_BACKUP_AWS_SECRET_ACCESS_KEY
COPAW_BACKUP_S3_BUCKET
COPAW_BACKUP_S3_PREFIX
COPAW_BACKUP_S3_REGION
COPAW_BACKUP_ENDPOINT_URL
```

### 决策 3: 备份三个目录

**选择**: 每个租户备份三个目录到同一个 zip 文件

**目录结构**:
```
{tenant_id}.zip
├── config.json           # 租户配置
├── workspaces/           # Agent 工作空间
├── memory/               # 记忆数据
├── .secret/              # 租户密钥（解压到 WORKING_DIR/tenant_id/.secret/）
│   └── envs.json
└── .providers/           # Provider 配置（解压到 SECRET_DIR/tenant_id/providers/）
    ├── builtin/
    ├── custom/
    └── active_model.json
```

**理由**:
- 完整备份租户所有数据
- 解压时根据前缀路由到正确目录
- 避免数据遗漏

### 决策 4: 新增 `list_all_tenant_ids()` 函数

**选择**: 在 `config/utils.py` 中添加 `list_all_tenant_ids()` 函数

**理由**:
- 扫描 `WORKING_DIR` 目录，返回包含 `config.json` 的子目录名
- 与参考项目的 `list_all_user_ids()` 功能对应
- 备份时枚举所有需要备份的租户

## Risks / Trade-offs

| 风险 | 缓解措施 |
|------|---------|
| [风险] boto3 依赖未安装 | 在 requirements.txt 中添加 boto3 |
| [风险] S3 配置错误导致备份失败 | 添加配置验证，返回明确的错误信息 |
| [风险] 大租户备份时间过长 | 使用并行压缩和上传，添加进度跟踪 |
| [风险] 恢复时覆盖现有数据 | 先备份当前数据，支持回滚 |
| [风险] 多实例并发备份冲突 | 任务锁机制，同一时间只允许一个备份任务 |

## API Design

### 单租户备份 API

```
POST /api/backup/upload
Request: {
  "tenant_ids": ["tenant-1", "tenant-2"],  // 可选，为空则备份所有
  "instance_id": "instance-01",            // 可选，默认 "default"
  "backup_date": "2026-04-07",             // 可选，默认今天
  "backup_hour": 14                        // 可选，默认当前小时
}
Response: {
  "task_id": "uuid",
  "status": "pending",
  "message": "Backup task created successfully"
}
```

### 恢复 API

```
POST /api/backup/download
Request: {
  "date": "2026-04-07",
  "hour": 14,
  "instance_id": "instance-01",
  "tenant_ids": ["tenant-1"]
}
Response: {
  "task_id": "uuid",
  "status": "pending",
  "message": "Restore task created"
}
```

### 批量备份 API

```
POST /api/backup/batch/upload
Request: {
  "instance_ids": ["instance-01", "instance-02"],
  "backup_date": "2026-04-07",
  "backup_hour": 14
}
```

## File Structure

```
src/copaw/app/backup/
├── __init__.py          # 模块导出
├── config.py            # 配置模型和环境变量加载
├── models.py            # BackupTask 等数据模型
├── s3_client.py         # S3 客户端封装
├── task_store.py        # 任务状态持久化
├── worker.py            # 异步备份/恢复执行器
├── service.py           # 业务逻辑层
├── router.py            # API 路由
├── batch_models.py      # 批量操作模型
├── batch_router.py      # 批量操作路由
└── batch_service.py     # 批量操作服务
```

## Migration Plan

### 部署前准备

1. 在 `dev.json` 或 `prd.json` 中配置 S3 凭证
2. 确保 boto3 已安装

### 验证步骤

1. 调用 `GET /api/backup/list` 验证 S3 连接
2. 调用 `POST /api/backup/upload` 测试单租户备份
3. 调用 `GET /api/backup/tasks/{task_id}` 查看进度
4. 调用 `POST /api/backup/download` 测试恢复