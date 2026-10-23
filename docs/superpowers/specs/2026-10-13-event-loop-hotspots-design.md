# 事件循环五项热点优化

## 已确认约束

保持 API、工具截断协议、持久化保证和租户隔离兼容，分阶段优化。工作基于 `freshness-ttl-optimization`；主工作区的未提交修改不纳入本次开发。

## 阶段 A：执行边界

1. 工具输出压缩：在独立有界输出 worker 中完成内容复制、编码、截取、artifact 写入和清理；worker 不修改在线 Msg，协程返回后应用内容。取消后不应用结果，worker 完成才释放并发许可。
2. checkpoint：请求级内存异步准备后注入 Agent，绑定归档与 recover_evidence 共用同一 epoch。文件锁、读取和解析在 runtime-state worker 中执行。保留同步接口用于既有同步调用方。
3. Provider：异步模型解析入口使用 Provider 既有初始化 executor 读取运行配置和取得模型配置副本，Agent 构造消费已准备结果。完整快照路径不再次 prepare_scope；同步 CLI 路径保持兼容。
4. Shell：异步子进程读流不变，完整输出 join/decode、错误分类与响应格式化在输出 worker 中执行。原有输出格式、退出码、取消和进程组清理不变。
5. S3：SDK 构造、列表分页和结果处理统一经独立有界备份 worker；路由、普通恢复、shell 恢复和批量查询使用异步入口。当前 API 返回完整列表；不跨请求缓存最新结果。

## 后续阶段

阶段 B：artifact 有限头尾读取、签名和预算缓存、低频保留期清理；checkpoint 仅读取构造所需 epoch。恢复计划单次枚举复用已由用户明确排除本轮。

阶段 C：Shell 有界字节缓冲和流式 spool，完整格式化 artifact 共用 canonical 截断协议；SSE 重放分批执行并保持顺序。截断关闭或落盘失败时继续兼容原输出，不承诺最终内存有界。

阶段 D：测量 worker 排队后再隔离 runtime-state executor 和调整容量。不得把重型输出与 S3 工作塞进 runtime-state 默认预算。

## 验证

每项先增加会在原实现失败的线程边界或响应性回归，再修改生产代码。覆盖源配置覆盖、artifact 失败、取消、Chat epoch 重建、Provider scope 隔离、Shell 超时与非零退出、S3 分页。基线定向测试 56 项通过。

记录事件循环延迟 p95/p99、各 worker 排队和执行时间、RSS 采样峰值、checkpoint 读取字节/调用与 S3 分页尝试次数。阶段 A 不宣称解决 Shell 无界积累或全历史重复读取。

## 阶段 A 实现状态

五项执行边界均已实现。输出和备份使用独立的进程级线程预算；当前输出为 8（用户后续调整）、备份为 4。取消运行任务后仍占用许可直到真实完成。输出异步入口只在内容发生变化时替换在线消息，取消后不应用 worker 结果。备份初始化等待位于任务互斥锁内且在持久化之前，保留 409 判断顺序。

同步接口继续保留。Query 的 SESSION_START 阻止结果在模型及内存准备之前返回；Agent 和模型客户端仍在事件循环上构造。checkpoint 绑定和 recover_evidence 复用请求内存捕获的 epoch。

Provider 配置副本保证捕获后不会随管理器刷新改变，既有 builtin Provider 逐字段刷新仍不提供跨字段事务快照。消息内容在压缩期间由请求独占，身份检查能防止覆盖被替换的内容，不检测并发原地修改。

运行时性能和真实 S3 延迟尚未压测；当前验证证明线程边界、协议兼容及取消/并发行为。取消、ContextVar、等待预算等排查方法见 [Playbook](../../../analysis/playbook/event-loop-worker-boundaries.md)。

## 阶段 B：历史工具输出

历史 artifact 改为无缓冲的有限头尾读取，沿用原 UTF-8、行边界与 marker 内核，展示与旧实现一致。缓存键包含已验证的绝对路径、dev/ino/size/mtime_ns/ctime_ns、展示预算及恢复引用；最多 128 项，累计缓存字符串的 `sys.getsizeof` 不超过 4 MiB，键和容器开销另计。每次命中仍验证路径和 metadata；文件修改、删除、预算变更不能错误复用。

异步历史压缩按目录和保留期节流，300 秒内最多一次扫描，记录最多 128 项；记录被 LRU 淘汰后允许提前重扫。失败扫描也采用此间隔。显式同步 cleanup 接口继续立即扫描。读取/检查失败或读取期间文件变化时，保留原展示且不缓存。工作区外目录 symlink 不读取；工作区内 alias 保留原恢复引用格式。

此子阶段不改变新输出的全量持久化、在线消息复制及 worker 容量。

## 2026-10-14 后续实现

checkpoint 绑定改为 Chat 锁内只读 epoch 元数据，不读取完整 record/events；损坏 epoch 立即拒绝，完整状态消费者继续校验 record/events。持久化写入、原子替换和 fsync 保持原保证。

Unix Shell 在启用工具压缩的上下文内采用流式 spool，stdout/stderr 分别最多保留一个展示预算的原始缓冲。超出后经输出 worker 写临时文件，最终生成完整格式化 UTF-8 artifact 并复用 canonical 展示；短写回退与重复取消保持完整性。Windows、禁用压缩和磁盘失败继续原全输出行为。SSE 历史重放入队及消费每 64 条让出 loop，并保持历史与实时顺序。

默认 executor 饱和探针观测到约 253 ms runtime-state 等待后，将其隔离为独立 8 线程、8 个已提交任务的进程级预算。输出仍为用户调整的 8，备份为 4。ContextVar、异常和真实线程完成后释放许可的规则不变。

进程级指标接入 output/runtime_state/provider/backup，worker 保留最近 2048 次完成任务的排队/执行时长与累计完成数；checkpoint active-state 原始字节和 S3 分页尝试为累计计数。诊断日志增加 loop p99、样本数和 RSS 采样峰值，保留原字段。采样口径、性能代价及可复现结果见 [性能记录](../benchmarks/2026-10-14-event-loop-followups.md)。S3 恢复计划复用未实现，分页计数不变。
