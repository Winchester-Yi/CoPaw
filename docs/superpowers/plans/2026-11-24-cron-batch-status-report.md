# 定时任务批调度简报实施计划

详细设计见 [设计文档](../specs/2026-11-24-cron-batch-status-report-design.md)。使用现有 FastAPI、MySQL、HTTPX 和 pytest。按仓库工具映射在主线程逐项实施。验证后由用户决定是否提交。

所有测试在 WSL 工作树目录运行，解释器使用原仓库的 `/mnt/d/Projects/CoPaw/venv/bin/python`，`PYTHONPATH=src:scheduler/src:monitor/src`。

- [x] Monitor。实现并验证业务日窗口、整批跳过、异常聚合和共享占用。只读事务复用新版看板和 Worker 在途口径。
- [x] SWE。实现受保护的单次投递接口，验证员工号解析、拒绝、超时、成功和渠道调用次数。
- [x] Scheduler。完成异步受理、快照冻结、分段、独立后台、结果查询和人工建表 SQL。使用真实 MySQL 验证幂等、并发争抢及投递中断。
- [x] 本地对接。补充部署配置和平台作业请求，运行定向回归、格式检查及隔离 MySQL 集成验证。
- [ ] 环境联调。在测试来源验证平台固定请求体、实际招乎长度和回执，再启用真实来源。

## 招乎汇总与明细分开发送

- [x] 先修改 Scheduler 渲染测试和 Monitor 快照测试，验证第一条只含汇总、逐批明细隐藏 ID、缺名时使用「未命名任务」，并覆盖超长明细、模型池续发和无批次。运行 `venv/bin/python -m pytest -q tests/unit/scheduler/test_batch_status_report.py tests/unit/monitor/test_batch_status_report.py`，观察新断言在原实现上失败。
- [x] 修改 `batch_status_report/render.py` 和 Monitor 的简报名称回退，冻结正文时将模板版本升为 2。沿用现有投递顺序及数据快照。同步特性、详细设计与接入文档。
- [x] 更新隔离 MySQL 用例，核对汇总先领取且后续明细在前一条结束后继续投递。运行简报相关测试及 `git diff --check`，核对测试结果和投递条件。

## 简报按失败成员分类

- [x] 先加渲染和 MySQL 回归断言，覆盖零失败、主执行失败、子任务结算失败、同一成员的多种异步错误和看板错误类型组合。
- [x] 在 Monitor 快照聚合当前失败成员及历史错误类型，Scheduler 按失败数有条件地渲染详情。同一成员按失败阶段计一次，统计类型时沿用看板组合。保留旧快照字段供滚动升级。
- [x] 更新需求和详细设计，运行简报单测、隔离 MySQL 回归和差异检查。
