# 会话服务单元隔离实施验收

日期：2026-11-02。工作树：`.worktrees/conversation-mysql-nas`，实现基线 `d4a691892`。

## 实现范围

- 共享七表的服务列、索引和 readiness 与已提交 SQL 一致；环境身份必须显式提供。
- 运行时、会话、历史、分享和执行/发布/引用均绑定同一服务；显式外服务引用在 NAS IO 前拒绝。
- v2 迁移清单提供原 workspace 和每服务原 NAS 映射；全局 ID 协调，分享回填后收紧。
- 备份/恢复、GC、结束 owner 结算带服务边界；同服务多 scope 和跨服务隔离根恢复均保留。
- 原有 4096/4097 Publication 分批 pin、依赖及取消清理回归保留。

SQL 位于 `scripts/sql/conversation_metadata_tables.sql`、
`conversation_share_publication_extension.sql` 和 `conversation_share_service_unit_finalize.sql`；
其设计与实库 schema 验证已随 `b10a83abd`、`d4a691892` 提交。本轮接入运行时契约。

## 单元和静态校验

使用 `../../venv/bin/python -m pytest`，范围为：

```text
tests/unit/app/conversation_storage
tests/unit/cli/test_conversation_storage_cmd.py
tests/unit/scripts/test_rehearse_conversation_storage.py
tests/unit/scripts/test_benchmark_conversation_semantic.py
tests/unit/agents/tools/test_emit_wplus_sop_event.py
```

最终完整结果：637 passed，0 failure/error/skip，38.90s。
XML：`/private/tmp/conversation-service-all-unit-final.xml`。
任务变更文件的全部 pre-commit hooks（含 mypy、Black、flake8、pylint）通过；
`git diff --check` 通过。

## 真实 MySQL 与本地 NAS 模拟

仅使用随机生成的隔离 schema，每项测试自行创建和清理 child schema；包装脚本清理 control
schema，并脱敏数据库输出。正文位于 pytest 临时目录，未读写业务 NAS 或生产表。

初轮双服务运行时、并发执行、分享隔离、readiness 和长期 Session scale 的 6 项通过；
整组恢复与迁移失败分别定位为测试 NAS 根遗漏创建、旧版 identity 切片，随后修复。
常规 Session/Chat/history/share/execution/retention 的 53 项中 51 项通过，另外两项测试
子类构造器未传服务；已修复夹具并列入最终重跑。离线首次完整迁移测试进一步发现迁移
专用锁上下文的交互，以及 archive helper 未传 scope，均有定向回归与修复。

API/W+ 事件工具、重复归档问题分享证明、结束 owner 恢复 CLI 与 fsync/正文损坏故障路径最终 7 项通过（147.37s）；证据 `conversation-service-mysql-api.xml`。完整迁移与安装数据库重跑结果待追加。失败的中间 XML 保留，不用通过的子集掩盖失败：
`conversation-service-mysql-first.xml`、`conversation-service-mysql-runtime.xml`、
`conversation-service-mysql-offline.xml`、`conversation-service-mysql-regression.xml` 均在
`/private/tmp/`。最终证据使用独立 XML。

## 独立检视与边界

子代理 `review_service_runtime_spec`、`review_service_migration_spec`、
`review_service_maintenance_spec` 分别只读检视运行时、离线迁移和维护链路。
确认问题均修复并复核；检视不连接数据库、不访问 NAS、不修改文件。

token 排序规则冲突检查每次最多 512 个参数，保留跨批等价冲突检测；批次数 B 时约
`B(B+1)/2` 次查询，是当前离线预检的性能取舍。

本记录不替代真实 Kubernetes/NAS、全 scope 生产快照和固定旧生产镜像的上线/回退验收。
未创建新业务 workspace，未部署、推送或合并。本轮提交排除原有用户修改
`analysis/playbook/common-errors.md` 及两份 2026-11-01 未跟踪文档。
