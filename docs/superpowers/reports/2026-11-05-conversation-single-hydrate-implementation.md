# Chat 与 Tracing 单次 metadata hydrate 实现记录

日期：2026-11-05。基线：`f66a603ba`。工作区：`.worktrees/conversation-mysql-nas`。

## 改动与成本边界

完成 [SQL/NAS 成本设计](../specs/2026-11-05-conversation-sql-nas-cost-design.md)中的工作包 A，按[实施计划](../plans/2026-11-05-conversation-single-hydrate-plan.md)推进。

此前 Chat 与 Tracing 当前详情、归档页都会先通过 manager 读取完整 Chat，再由 HistoryStore 读取同一份 metadata。现在四个 indexed 入口直接调用 reader，将既有授权回调传入。Tracing 的目标解析保留 workspace 和 user/source/agent 规则，indexed 路径不再前置读取完整 Chat。

减少的是每请求路由前置的一次完整 metadata 查询与其 pin、NAS 正文读取；reader 内必要的 metadata、消息正文读取、版本复核和清理仍保留。没有新增缓存，没有调整 schema、索引、存储格式或 HTTP 模型，也没有把详情改成 summary。文件后端仍走原 manager 查询和授权路径。

运行时仅修改 `src/swe/app/runner/api.py` 与 `src/swe/app/routers/tracing.py`。没有修改 HistoryStore 的读取、授权、复核和 pins 生命周期。

## 验证证据

- 四入口成本回归：禁止 manager 完整读取，确认使用指定的 current/archive reader；实施前 4 项失败，实施后通过。
- Tracing 的 id、user、source、agent 授权覆盖 reader 前后两次检查，均拒绝不匹配对象；移除对已删除 lookup 阶段的绑定。
- Chat/Tracing 当前与归档的 missing、存储异常、无效 cursor 分别验证 404、503、422；存储错误不会回退到文件后端。
- 真实 HistoryStore 当前详情组装测试验证每请求仅一次 `_hydrate`，两入口累计两次；保留 memory/task/error/anchor、隐藏上下文脱敏、归档边界及审批组装。覆盖授权拒绝、NAS 失败、revision 冲突和取消时的清理。
- 文件后端的详情/归档响应 parity、身份隔离和运行中读取回归继续通过。

执行：

```bash
../../venv/bin/python -m pytest \
  tests/unit/routers/test_tracing_chat_history_parity.py \
  tests/unit/routers/test_tracing_chats_api.py \
  tests/unit/app/conversation_storage/test_history.py \
  tests/unit/app/test_chat_compaction_history_api.py \
  tests/unit/app/test_chat_history_message_identity.py \
  tests/unit/app/test_model_call_error_history.py \
  tests/unit/app/test_context_usage.py -q
```

结果：**98 passed**。本工作包文件的 pre-commit、`git diff --check` 及五份相关文档的本地链接/占位项检查均通过。pre-commit 首轮格式化了测试，并发现补充断言后函数超过语句数限制；提取重复计数断言后，重新检查及上述回归通过。

上述测试使用真实 reader 组装逻辑，但数据库、hydrate 和 NAS payload 使用替身；不证明真实 MySQL/NAS 的延迟或吞吐收益。尚未执行专用数据库与现场 NAS 验收，不提供加速倍数。

## 剩余工作

下一包为 [B1a 引用批量化](../specs/2026-11-05-conversation-reference-batching-design.md)：在原事务内批查 locator 和引用、保留窄列并按 packet 预算限制命令大小。B1b 的全量一致性与联合锁、B2 成员批量写、同 bundle 冷读、NAS 背压、有界缓存、写入快照准备均未实现。

本工作包按用户要求提交，保留已有 worktree 和无关改动。
