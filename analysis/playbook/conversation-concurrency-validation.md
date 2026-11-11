# 会话存储并发校验

2026-11-11，针对 062cf455e 的第二轮复核，三项遗漏经调用链和确定性交错测试确认。

## 根因与修复

- 共享历史：Session.context_epoch 只在 reset 时变化，compact/recovery prune 隐藏 occurrence 不改变另一共享 Chat 的 history_revision。NAS pin 只保护正文寿命，不能证明在线可见性。HistoryStore._validate_snapshot 现在通过有界 collection 查询重验所有非 archive occurrence 的 current_visible/deleted_at；读取前及返回前均检查，隐藏时抛 ConflictError，并在 finally 释放 pin。不增加 context_epoch，避免改变 checkpoint/消息安装的 epoch 语义。archive 保持 Chat 版本校验。
- 批量删除：HTTP batch-delete 已逐项授权，却只传 ID。现在收集 expected_chats 并传至 manager/repository；仓库原有事务行锁内 ownership 比较拒绝过期授权，HTTP 返回 409。缺失 Chat 仍跳过，事务内任何冲突会回滚整批。
- 自动标题：进程内 manager 锁不能保护其他 Pod；旧 spec 合并标题后无条件保存会覆盖新 metadata。现在修改前深拷贝 expected_chat，并使用 MySQL repository.update_chat_if_unchanged；仓库已有 metadata revision 条件提交阻止检查后的竞争。文件仓库仍使用原有进程内锁保存路径。自动标题 runner 捕获冲突，停止发布本次标题，不覆盖新 metadata，不盲目重试。

## 回归入口与证据

修复前新增定向用例 6 failed / 29 passed：批量授权快照缺失、标题未走条件更新、compact/recovery prune × 详情/tracing 未拒绝旧消息。修复后会话存储全目录加 API/manager 测试 1061 passed（53.85 秒），另补实际 AgentRunner._persist_session_title 调用覆盖。

测试通过在 NAS 读取阶段调用真实 _prune（compact 传 context_epoch，普通 recovery 不传）并模拟 SQL 可见性提交，保持共享 Chat 和 Session epoch 不变，验证返回前拒绝旧读取和 pin 释放。原 reset 测试继续保留。标题测试模拟另一个实例在条件提交前写入 metadata，验证保留新值和原标题。

这是本地故障注入验证，未连接真实 MySQL/TDSQL、NAS 或 Kubernetes 多 Pod。返回前重验建立读取有效性检查点，不承诺 HTTP 发送后数据不再变化。批量可见性重验沿用包大小和 ID 数量分批限制，无 schema 变更。
