## Why

CoPaw 在 Kubernetes 容器多实例部署环境中，需要支持租户数据的备份和恢复功能。当容器重启、迁移或发生故障时，需要能够将租户数据（包括工作目录、密钥配置和 Provider 配置）备份到 S3 存储，并在需要时恢复。参考 CoPaw-zhaohu 项目中已实现的备份功能，需要将其适配到当前项目的租户隔离架构中。

## What Changes

- **新增** `src/copaw/app/backup/` 模块，包含完整的备份/恢复功能
- **新增** 租户级别的备份支持，每个租户独立打包为 zip 文件
- **新增** S3 存储支持，备份文件按 `{prefix}/{instance_id}/{YYYY-MM-DD}/{HH}/{tenant_id}.zip` 结构存储
- **新增** 环境变量配置加载，从 `dev.json`/`prd.json` 读取 AWS S3 凭证
- **新增** 批量备份 API，支持多实例部署的集中管理
- **新增** `list_all_tenant_ids()` 工具函数用于枚举所有租户
- **修改** 路由注册，添加 `/api/backup/*` 和 `/api/backup/batch/*` 端点

## Capabilities

### New Capabilities

- `tenant-backup`: 租户数据备份与恢复能力，支持将租户的工作目录、密钥配置和 Provider 配置打包上传到 S3，支持按需恢复

### Modified Capabilities

- （无现有 spec 需要修改）

## Impact

- **代码文件**:
  - `src/copaw/app/backup/`: 新增备份模块（11 个文件）
  - `src/copaw/config/utils.py`: 新增 `list_all_tenant_ids()` 函数
  - `src/copaw/config/envs/dev.json`: 新增备份相关环境变量
  - `src/copaw/config/envs/prd.json`: 新增备份相关环境变量
  - `src/copaw/app/routers/__init__.py`: 注册备份路由

- **数据存储**: 备份文件存储在 S3，路径结构为 `{prefix}/{instance_id}/{YYYY-MM-DD}/{HH}/{tenant_id}.zip`

- **API 变更**: 新增备份 API 端点
  - `POST /api/backup/upload` - 创建备份任务
  - `POST /api/backup/download` - 创建恢复任务
  - `GET /api/backup/tasks` - 查询任务列表
  - `GET /api/backup/tasks/{task_id}` - 查询任务详情
  - `DELETE /api/backup/tasks/{task_id}` - 删除任务
  - `GET /api/backup/list` - 列出可用备份
  - `POST /api/backup/batch/upload` - 批量备份
  - `POST /api/backup/batch/download` - 批量恢复

- **环境变量**: 新增以下环境变量配置
  - `COPAW_BACKUP_AWS_ACCESS_KEY_ID`
  - `COPAW_BACKUP_AWS_SECRET_ACCESS_KEY`
  - `COPAW_BACKUP_S3_BUCKET`
  - `COPAW_BACKUP_S3_PREFIX`
  - `COPAW_BACKUP_S3_REGION`
  - `COPAW_BACKUP_ENDPOINT_URL`

- **向后兼容**: 完全向后兼容，备份功能为可选功能