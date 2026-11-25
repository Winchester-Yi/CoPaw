# 批调度自动重试在线配置实施计划

**目标**：来源系统管理员可在线调整新版批调度自动尝试上限。保存后由 Scheduler 在后续判定中读取，已入队的执行继续按照队列状态推进。

**方案**：系统特性配置保留来源覆盖值。SWE 内部接口仅投影重试上限，Scheduler 自动重试巡检和当日失败补跑共用读取结果。缺少来源覆盖时继承当前 Scheduler 部署配置，缺省为 3。

- [ ] 在 `tests/unit/app/test_source_system_config.py` 和 `tests/unit/app/test_system_cron_config_api.py` 验证覆盖值校验、清除及内部只读投影。更新 `source_system_config` 注册表、存储及 `crons/system_api.py`。
- [ ] 在 `tests/unit/scheduler/test_managed_batch_auto_retry.py` 和 `tests/unit/scheduler/test_daily_failure_retry.py` 先验证在线值变化和原生重试耗尽判定，再实现 `managed` 公共读取逻辑，接入两个巡检及运行时组装。
- [ ] 在 `console/src/pages/SystemConfigPage/index.test.tsx` 验证写入和清除。沿用定时任务区和抽屉现有的输入与统一保存交互。
- [ ] 使用仓库 WSL 虚拟环境验证上述 Python 测试及 `test_scheduler_config.py`，运行 Console 定向测试、类型检查和相关格式检查，核对页面适用的浏览器条件及最终差异。
