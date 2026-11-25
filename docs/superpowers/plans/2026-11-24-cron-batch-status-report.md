# 定时任务批调度简报实施计划

详细设计见 [设计文档](../specs/2026-11-24-cron-batch-status-report-design.md)。使用现有 FastAPI、MySQL、HTTPX 和 pytest。按仓库工具映射在主线程逐项实施。验证后由用户决定是否提交。

所有测试在 WSL 工作树目录运行，解释器使用原仓库的 `/mnt/d/Projects/CoPaw/venv/bin/python`，`PYTHONPATH=src:scheduler/src:monitor/src`。

- [x] Monitor。实现并验证业务日窗口、整批跳过、异常聚合和共享占用。只读事务复用新版看板和 Worker 在途口径。
- [x] SWE。实现受保护的单次投递接口，验证员工号解析、拒绝、超时、成功和渠道调用次数。
- [x] Scheduler。完成异步受理、快照冻结、分段、独立后台、结果查询和人工建表 SQL。使用真实 MySQL 验证幂等、并发争抢及投递中断。
- [x] 本地对接。补充部署配置和平台作业请求，运行定向回归、格式检查及隔离 MySQL 集成验证。
- [ ] 环境联调。在测试来源验证平台固定请求体、实际招乎长度和回执，再启用真实来源。
