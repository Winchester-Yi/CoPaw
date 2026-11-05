# 会话软租约 Implementation Plan

> 在现有隔离 worktree 顺序实施；用户已授权切换方案并随后要求提交本包，不另开 worktree。

**Goal:** 接受未被撤销代次的迟到写入，心跳失败不误取消执行，保留明确 fence/Stop/恢复安全语义。

**Architecture:** ExecutionStore 和 PublicationStore 统一移除基于时间的授权判断，保留数据库行锁和完整身份版本检查。Runner 心跳仅重试规范化可用性错误，保留每次 SQL 总预算和动态控制容量；确认的执行权冲突仍取消。

**Tech Stack:** Python >=3.10、AnyIO、asyncio、MySQL、pytest。

- [x] 1. 先改/加单元测试并观察失败：过期 start/renew/finish、所有 Turn publication 的 reserve/commit、代次/owner/Turn 改变拒绝；心跳超时后任务继续并恢复，慢 admission 可进入 start，错误分类与未知提交不混淆。
- [x] 2. ExecutionStore._validate_authority 与 PublicationStore._validate_turn_authority 只按身份和状态校验，最终心跳 UPDATE 移除时间谓词但保留 owner/generation/active Turn；删除无消费者的 lease_valid 计算列。保留 SQL 总预算，规范化可重试心跳错误为 StorageUnavailable。
- [x] 3. IndexedQuery._renew 移除本地租期截止；仅暂态可用性错误按间隔重试、日志记录异常/恢复，明确冲突/完整性/编程错误取消。terminal 查询独立预算，外部取消继续传播；admission 不再以本地时间拒绝。
- [x] 4. 更新专用 MySQL 测试：过期后提交、不同问题仍 busy、旧代次拒绝、提交与 fence 两种顺序；覆盖真实锁等待仍有总预算。运行数据库、storage、Tracing 与真实测试模块（无专用库明确跳过）。
- [x] 5. 修订旧严格租约设计中的当前规则、实施报告和入口索引；对本包显式文件运行 pre-commit，核对无 schema/NAS/API 格式变更，报告结果与真实环境限制。

验证命令使用 `../../venv/bin/python -m pytest`；不读取/更改开发库配置，不升级共享 venv，不触及已有无关变更。

最终扩大回归 1084 passed、51 skipped（专用 MySQL 库未配置）；本包 pre-commit 通过，按用户后续指令提交。详见[实施记录](../reports/2026-11-05-conversation-soft-lease-implementation.md)。
