# Tenant Backup Specification

## Overview

租户备份功能提供租户级别数据的安全备份和恢复能力，支持将租户的完整数据（工作目录、密钥配置、Provider 配置）打包上传到 S3 存储，并支持按需恢复。

## Capabilities

### tenant-backup

租户数据备份与恢复能力。

**提供能力:**
- 租户数据完整备份（工作目录 + 密钥 + Provider 配置）
- S3 云存储集成
- 并行压缩和上传
- 进度跟踪
- 恢复回滚支持
- 多实例批量管理

## Configuration

### Environment Variables

备份功能通过以下环境变量配置（定义在 `config/envs/{env}.json`）：

| 变量名 | 必需 | 默认值 | 说明 |
|--------|------|--------|------|
| `COPAW_BACKUP_AWS_ACCESS_KEY_ID` | 是 | - | AWS Access Key |
| `COPAW_BACKUP_AWS_SECRET_ACCESS_KEY` | 是 | - | AWS Secret Key |
| `COPAW_BACKUP_S3_BUCKET` | 是 | - | S3 存储桶名称 |
| `COPAW_BACKUP_S3_PREFIX` | 否 | `copaw` | S3 路径前缀 |
| `COPAW_BACKUP_S3_REGION` | 否 | `cn-north-1` | S3 区域 |
| `COPAW_BACKUP_ENDPOINT_URL` | 否 | - | 自定义 S3 端点 |

### Multi-Environment Support

支持环境特定的配置，使用前缀方式：

```
{ENV}_COPAW_BACKUP_S3_BUCKET  # 例如 DEV_COPAW_BACKUP_S3_BUCKET
```

优先级：环境特定变量 > 通用变量

## Data Model

### BackupTask

```python
class BackupTask(BaseModel):
    task_id: str
    task_type: BackupTaskType  # "backup" | "restore"
    tenant_id: Optional[str]
    status: BackupTaskStatus  # pending | running | completed | failed | rolling_back | rolled_back
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]

    # Input parameters
    target_tenant_ids: Optional[list[str]]
    backup_date: Optional[str]  # YYYY-MM-DD
    backup_hour: Optional[int]  # 0-23
    instance_id: Optional[str]

    # Progress info
    current_step: str
    progress_percent: int  # 0-100
    processed_tenants: int
    total_tenants: int

    # Results
    s3_keys: list[str]
    local_zip_paths: list[str]
    error_message: Optional[str]
    rollback_data_paths: list[str]
    restored_tenants: list[str]
```

## Storage Structure

### S3 Path Format

```
{s3_prefix}/{instance_id}/{YYYY-MM-DD}/{HH}/{tenant_id}.zip
```

示例：`copaw/instance-01/2026-04-07/14/tenant-abc.zip`

### Zip File Structure

每个租户的备份 zip 文件包含：

```
{tenant_id}.zip
├── config.json              # 租户配置
├── workspaces/              # Agent 工作空间
├── memory/                  # 记忆数据
├── media/                   # 媒体文件
├── .secret/                 # 租户密钥（解压到 WORKING_DIR/tenant_id/.secret/）
│   └── envs.json
└── .providers/              # Provider 配置（解压到 SECRET_DIR/tenant_id/providers/）
    ├── builtin/
    │   ├── openai.json
    │   └── ...
    ├── custom/
    └── active_model.json
```

## API Endpoints

### Single Tenant Operations

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/backup/upload` | 创建备份任务 |
| POST | `/api/backup/download` | 创建恢复任务 |
| GET | `/api/backup/tasks` | 查询任务列表 |
| GET | `/api/backup/tasks/{task_id}` | 查询任务详情 |
| DELETE | `/api/backup/tasks/{task_id}` | 删除任务 |
| GET | `/api/backup/list` | 列出可用备份 |

### Batch Operations (Multi-Instance)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/backup/batch/instances` | 获取实例列表 |
| PUT | `/api/backup/batch/instances` | 更新实例配置 |
| POST | `/api/backup/batch/upload` | 批量备份 |
| POST | `/api/backup/batch/download` | 批量恢复 |
| GET | `/api/backup/batch/tasks` | 批量任务列表 |
| GET | `/api/backup/batch/tasks/{batch_id}` | 批量任务详情 |

## Behavior

### Backup Process

1. **任务创建**: 接收备份请求，创建 BackupTask 记录
2. **枚举租户**: 如果未指定租户列表，枚举所有租户
3. **并行压缩**: 每个租户独立压缩（最多 3 个并发）
   - 压缩工作目录
   - 压缩密钥目录（`.secret/`）
   - 压缩 Provider 配置（`.providers/`）
4. **并行上传**: 上传到 S3（最多 5 个并发）
5. **清理临时文件**: 删除本地临时 zip 文件

### Restore Process

1. **任务创建**: 接收恢复请求，创建 BackupTask 记录
2. **备份当前数据**: 为每个租户创建回滚备份
3. **下载备份**: 从 S3 下载对应租户的 zip 文件
4. **解压恢复**: 按前缀路由到正确目录
   - `.secret/` → `WORKING_DIR/tenant_id/.secret/`
   - `.providers/` → `SECRET_DIR/tenant_id/providers/`
   - 其他 → `WORKING_DIR/tenant_id/`
5. **成功清理**: 删除回滚备份

### Rollback on Failure

如果恢复过程中发生错误：
1. 标记任务状态为 `rolling_back`
2. 使用回滚备份恢复原始数据
3. 标记任务状态为 `rolled_back`

## Error Handling

| 错误场景 | 响应 |
|----------|------|
| S3 配置缺失 | 400 Bad Request: "Backup not configured" |
| 任务正在运行 | 409 Conflict: "Another backup task is already running" |
| S3 连接失败 | 任务状态 `failed`，包含错误信息 |
| 备份不存在 | 任务状态 `failed`，提示无可用备份 |

## Dependencies

- `boto3`: AWS SDK for Python (S3 操作)
- `pydantic`: 数据模型验证
- `httpx`: 批量操作的 HTTP 客户端