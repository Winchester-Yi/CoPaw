# Memory Runtime Metrics Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to execute this authorized plan inline; request an independent review after implementation.

**Goal:** 补齐热终态/保护状态、存储提交耗时、恢复/迁移失败统计，保持有界且不改变业务结果。

**Architecture:** 扩展现有PerformanceMetrics的固定数值窗口；同步/异步装饰器统一计时及失败统计。session根发布后观测热dict，checkpoint追加前恢复检查单独统计，既有runtime输出无需新增路由。

**Tech Stack:** Python标准库、threading.Lock、deque、contextlib、pytest。

## 1. 数值聚合与操作计时

Files: `src/swe/performance_metrics.py`, `tests/unit/test_performance_metrics.py`。

- [x] 先写固定标签、2048窗口、并发计数、异常/取消与异步完成测试，运行`env PYTHONPATH=src:tests/fakes ../../venv/bin/python -m pytest tests/unit/test_performance_metrics.py -q`确认新增API缺失导致RED。
- [x] 实现record_session_memory四数值入口及record_storage_operation固定操作/结果入口，snapshot增加session_memory/storage_operations；操作失败更新固定counter。
- [x] 实现同步/异步measure_storage_operation装饰器，finally按真实完成时间记录，指标自身错误不覆盖业务结果；重跑到GREEN。

## 2. session与checkpoint埋点

Files: `app/runner/session.py`, `app/runner/turn_ledger.py`, `agents/memory/checkpoint_storage_v2.py`, `agents/memory/conversation_archive.py`。

- [x] 在`tests/unit/app/test_memory_storage_metrics.py`先覆盖成功提交/UTF8/保护、根失败、恢复损坏、preview/迁移失败与不读冷历史，确认RED。
- [x] _commit_ledger_sync、TurnLedger.recover及两项迁移入口增加装饰器；只在已发布根后记录热状态，复用_size和_digest，记录仅数值。
- [x] checkpoint initialize/append/publish/reset增加提交计时，将原active验证/尾部截断原样提取为受计时的恢复helper；保持锁/flush/fsync/root顺序。
- [x] 运行新指标测试及既有ledger/journal/session事务回归到GREEN。

## 3. 诊断输出、审查与交付

- [x] 验证RuntimeDiagnosticManager输出包含新字段，更新本设计及playbook口径。
- [x] 独立审查指标语义、边界与额外读取；处理有证据的问题。
- [x] 运行受影响专项pytest及pre-commit、diff check，记录范围和实际结果。
- [x] 同步主工作区，保留无关改动；本轮不自动部署或迁移生产数据。
