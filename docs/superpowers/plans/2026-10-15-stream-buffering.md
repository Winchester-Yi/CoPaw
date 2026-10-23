# Stream Buffering Implementation Plan

**Goal:** 减少 Shell 小块 worker 提交，并通过共享日志、游标和客户端恢复控制 SSE 内存。

**Architecture:** Shell 在 reader 内聚合，复用原 spool worker。SSE 独立开发传输日志与订阅状态，先后端共享重放再前后端游标恢复，不在全局锁内等待 I/O。

**Tech Stack:** Python asyncio/pytest、现有有界 worker、React/TypeScript/Vitest。

设计：[stream-buffering-design](../specs/2026-10-15-stream-buffering-design.md)。在当前用户指定分支执行，保留现有无关修改，不使用其他 worktree 的旧版本覆盖。

## 1. Shell：独立可交付

- [x] 对 `_execute_unix_subprocess` 做 impact：直接 caller 为平台入口，间接 Shell 工具，LOW。
- [x] 真实 pipe 验证 256 KiB 加尾块输出，捕获 append 批次；旧实现 65 次，新实现 5 次，完整内容相同。
- [x] 在 `shell.py` reader 增加 64 KiB bytearray 和 finally flush。跨批次剩余数据在 await 前保留；原跨批次取消测试失败后修复。保持 4 KiB 读取与实时回调。
- [x] 增加 EOF 前实时可见、取消前未满批数据 flush 回归。慢写测试取消逐 4 KiB 内核偏移假设，验证真实批次写入；保留超时、继承 pipe、重复取消、目录替换测试。
- [x] 定向回归 47 passed。
- [x] 4/16/32/64 KiB 各三轮；原始数据与 p99 限制见 `docs/superpowers/benchmarks/2026-10-15-shell-batching.md`。
- [x] 更新 Playbook，修改文件 pre-commit、diff 检查、detect-changes 通过；提交 4a0d41b3e。

## 2. SSE：共享日志与订阅

- [x] 完成 TaskTracker、Console route 和调用链影响分析；动态图调用以 coordinator/路由源码与回归补足。新响应类图影响为 CRITICAL，路由入口为 LOW。
- [x] 新建专责 replay store 模块及独立有界 transport I/O worker；记录 seq、长度、offset，追加成功才公布。单帧、每轮与进程预算先预留再写，失败回滚预留。
- [x] 先写共享日志的 append/read/关闭/短写/失败/配额/并发测试，再实现内核。read 每次受条数与字节约束，日志索引与 TTL 清理不能无限增长。
- [x] 用订阅对象取代 payload 队列：保存 next_seq、唤醒事件、关闭状态；stream 拉取历史，不在 attach 预填充。无丢失唤醒，无全局锁 I/O，保留 TurnIdentity 校验。
- [x] 测试两订阅者一快一慢、晚 attach、历史与实时交界、无订阅继续生产、终态回放、断开/取消 cleanup；运行 TaskTracker、coordinator 与 Console 路由回归。

## 3. SSE：前后端续传与治理

- [x] 前端先支持非终态 EOF/控制帧恢复，后端默认不主动断开旧客户端。更新 request owner 状态，事件 reducer 与 cursor 同步提交，跨 owner 不复用游标。
- [x] SSE 帧加入 turn-bound id；请求支持 after cursor，兼容未提供 cursor 的全量重放。参数验证覆盖错误身份、未来游标和不可用日志，不能因重连创建新 producer。
- [x] 前端保留同一 owner 的 reducer，跳过重复、拒绝缺口；通过现有 reconnect callback 传递 cursor。覆盖完整 API adapter、Stream parser 与 useChatRequest 测试。
- [x] 新前端支持后开启慢消费者预算，控制事件独立于数据队列；控制发送失败时以非终态 EOF 触发恢复。重试有退避和总预算，终态 snapshot 可替换恢复。
- [x] 添加日志/订阅预算、配额/恢复及 worker 指标；真实 ASGI 验证有序结束、超时与锁竞争断连。四租户内核模拟已记录 RSS 与吞吐，结果不代表生产端到端压力测试；Linux 容器验证留给部署环境。
- [x] 最终交付检查：后端 150 passed、前端 52 passed、typecheck 与全部修改文件 pre-commit 通过；独立复核无剩余 Important/Critical 问题，staged detect-changes 为 29 文件/159 符号、CRITICAL。前后端协议同一提交，主动断开以 cursor-v1 协商为门槛。

## 实现边界与测量

首版取消近期 payload 热缓存，避免额外淘汰状态；订阅只持有有界读取批次。固定容量、线程数与协议策略见设计稿。逐事件磁盘日志减少慢队列内存，但无节流模拟耗时增加；详见 benchmarks/2026-10-15-sse-replay.md。ASGI 锁竞争断连曾复现订阅泄漏，已在保活与事件生成器清理区增加取消屏蔽，4 个断连场景回归通过。
