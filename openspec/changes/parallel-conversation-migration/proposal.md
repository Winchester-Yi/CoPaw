# 万级 workspace 会话迁移并行化

## 问题

当前 v3 发现、源解析、NAS bundle 发布及全量验证按 workspace 串行执行。万级 workspace 场景下，NAS 延迟无法被其他 workspace 工作覆盖；全量 inventory、每批 journal hydrate/清单重写还放大内存和控制面开销。

## 拟议变化

保留一个进程、一个迁移作业、一条持锁 SQL 连接，增加有界 workspace 调度和 NAS IO 并行。数据库事务由唯一协调任务执行。解析结果按 workspace 写入本地临时文件，避免一次加载全部历史正文；导入和验证消费有界窗口。修复每批全量 journal hydration 与重复源树遍历，增加资源限制、进度及故障验收。

## 边界

仅修改 v3 直接迁移路径；保留旧 offline CLI 的兼容行为。无线上增量/CDC、多迁移进程协同、SQL多writer、改库DDL或正文格式变更。不改变一次连续停机、全服务整体ready和原分享字段保留契约。

状态：实施中。CLI 已接入并发与资源参数；有界 workspace/分享发布、spool 与增量 journal 已实现。完整 runtime 校验 IO 的并行预取正在收尾，万级四档端到端及故障验收尚未完成，不能据此放行迁移。
