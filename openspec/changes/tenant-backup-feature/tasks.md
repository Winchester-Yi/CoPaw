## 1. 基础设施

- [x] 1.1 创建 `src/copaw/app/backup/` 目录
- [x] 1.2 添加 `list_all_tenant_ids()` 函数到 `config/utils.py`
- [x] 1.3 在 `config/envs/dev.json` 添加备份环境变量
- [x] 1.4 在 `config/envs/prd.json` 添加备份环境变量

## 2. 备份模块核心文件

- [x] 2.1 创建 `__init__.py` 模块导出
- [x] 2.2 创建 `config.py` 配置模型（从环境变量加载）
- [x] 2.3 创建 `models.py` 数据模型（字段名适配 tenant）
- [x] 2.4 创建 `s3_client.py` S3 客户端
- [x] 2.5 创建 `task_store.py` 任务状态持久化

## 3. 备份执行器

- [x] 3.1 创建 `worker.py` 异步执行器
- [x] 3.2 实现三目录压缩逻辑（工作目录、密钥、Provider 配置）
- [x] 3.3 实现解压时路由到正确目录
- [x] 3.4 实现并行压缩和上传
- [x] 3.5 实现恢复回滚机制

## 4. 服务层和路由

- [x] 4.1 创建 `service.py` 业务逻辑层
- [x] 4.2 创建 `router.py` API 路由
- [x] 4.3 创建 `batch_models.py` 批量操作模型
- [x] 4.4 创建 `batch_service.py` 批量操作服务
- [x] 4.5 创建 `batch_router.py` 批量操作路由

## 5. 路由注册

- [x] 5.1 在 `routers/__init__.py` 注册 backup router
- [x] 5.2 在 `routers/__init__.py` 注册 batch_backup router

## 6. 验证与测试

- [ ] 6.1 编写备份模块单元测试
- [ ] 6.2 编写集成测试（备份和恢复流程）
- [ ] 6.3 手动测试 S3 连接和备份上传
- [ ] 6.4 手动测试恢复和回滚机制

## 7. 文档更新

- [ ] 7.1 更新 CLAUDE.md 添加备份功能说明
- [ ] 7.2 添加 API 使用文档