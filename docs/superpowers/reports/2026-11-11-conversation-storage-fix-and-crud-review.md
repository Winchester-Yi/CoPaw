# 会话存储修复复核与 CRUD 完整性审计

日期：2026-11-11。固定源码：`09e2e2c01cfbeb6aac771d2c914b6ba65679a1aa`。
工作树：`.worktrees/conversation-mysql-nas`。本轮由主代理与三个子代理实际阅读迁移、Chat/Session CRUD、删除/分享/GC 调用链。历史正文生成新 bundle 是确认后的设计目标。初次复核仅新增报告；后续修复轮已完成下文 F3/F4 的代码与回归测试，未执行迁移或操作业务数据。

## 结论

此前两项问题的修复机制成立，可关闭原 F1、F2。扩展代码审计确认新的并发读取问题 F3，以及有合法所有权转移前提的删除授权竞态 F4，两项均在本次修复轮处理；环境验收和物理回收边界仍在，因此不能认定整个迁移及增删改查逻辑完全正确、完备，更不能提供“100% 正确”的保证。

本结论以具体调用链和可达交错为依据，测试仅用于辅助验证。未发现问题的已读路径也不等于穷尽所有运行状态。

## 原问题修复复核

### F1：在线 context usage SQL 投影——修复成立

`projection_publication.py:338` 从恢复正文或 projection envelope 的 state 提取 usage，按 ContextUsageSnapshot 模型验证，缺失或无效时保旧值并标 invalid，reset 强制 invalid。`:375` 将派生结果放入本次有效 CommitPlan；该字段不序列化，不由外部提交计划宣称为权威数据。

`publications.py:902` 在 inspect 已耐久 bundle 后调用 effective_recovery_plan；`:771` 在实际安装事务内更新 usage 两列，紧接 `:780` 安装 recovery locator/state_revision。共同受 publication authority、state revision 和事务回滚保护；普通状态保存、终态和重对账都走这一提交路径。因此不是只在 Session adapter 外层另做一次易失的 SQL 更新。

有效快照覆盖旧值并可清除 invalid；失败/无效快照使用 COALESCE 保留旧数值并标 stale，符合此前缺口的修复要求。

### F2：Chat patch 丢失并发更新——修复成立

`runner/api.py:2017` 将最初读取的 existing 作为 expected_chat 传递，并将 ConflictError 返回 409；`runner/manager.py:418` 在本地锁内重读并比较；`chat_repository.py:803` 先读行及 metadata_revision，再读取 current 与 expected 比较，最终安装仍受 metadata_revision CAS 约束。

并发 B 若在 repository 读行前提交，current/expected 比较拒绝 A；若在读行后提交，最终 revision CAS 拒绝 A。选择冲突后重读重试，而不是静默合并，能够阻止原报告所述旧完整 spec 覆盖并发修改。

## F3 — P2：共享 Session reset 后，在途历史请求可能返回已隐藏消息

设计要求：在线历史由共享 Session 控制，当前删除/可见性变更应生效；reader pin 仅保护物理正文，不能代替可见性重验。依据主设计第 3、6、7 节以及 ADR0038 的当前删除语义。

代码证据，路径相对于 `src/swe/app/conversation_storage/`：

- `history.py:278` 的 `_snapshot` 读取 Chat、Session.history_high，查询当前可见在线消息并建立 pin。snapshot 未记录 Session context_epoch 或其他可见性版本。
- `history.py:310` 的 `_validate_chat_row` 只重查 Chat 自身 history_revision、epoch、metadata_revision、归属等字段。
- `history.py:413` 的 `_assemble` 在正文读取前后调用上述检查，没有重查 Session 版本或返回消息的 current_visible。
- `archive.py:290` 的 `_reset` 隐藏整个 Session 所有非 archive 消息，并增加 Session.context_epoch，但只增加发起 reset 的 Chat.epoch/history_revision。

可达交错：A、B 共享一个 Session；B 的读取已取旧消息并 pin；A 的 reset 完整提交；B 继续读取并执行最后校验。B 自身 Chat 行未变，因此检查通过，返回已被 reset 隐藏的在线消息。同一 Chat 的 reset 可被 Chat 版本捕获，缺口发生在共享 Session 的另一 Chat。

影响：一次在途详情或 tracing 请求可能回显旧在线内容，不是物理正文丢失；随后新请求通常能看到正确 reset 状态。该问题足以否定“所有读路径完整正确”的结论。

建议：snapshot 捕获并在返回前校验 Session 的可见性版本，或批量重验所返回 occurrence 当前可见性。不要用 pin 存在判断消息仍可见，也不必给同 Session 所有 Chat 广播版本更新。补受控交错回归：暂停 B 的 NAS 读取，提交 A reset，再继续 B，要求拒绝旧 snapshot 或返回重新读取的可见结果。

## F4 — P2：合法转移所有权后，在途 DELETE 仍沿用旧授权

`runner/api.py:2052` 附近先读取 Chat 并调用 `_authorize_chat`，之后仅将 chat_id 传给 manager.delete_chats。`chat_repository.py:595` 的候选查询若发生在合法 rebind 提交之后，会读取新 Session，随后的关联重验仅证明候选查询以来未再变更。最终 tombstone SQL 按 scope/id 更新，没有授权时的 user 或 revision 约束。

可达交错：Alice 的 DELETE 已读取并授权原 Chat；有权限的管理调用或内部 `mgr.update_chat` 将 Chat 转移到 Bob 的 Session 并提交；Alice 的删除才进入 repository 候选查询，取得新关联并通过锁内检查，删除现属 Bob 的 Chat。

前提必须明确：普通 Alice 的更新 API 不能自行转给 Bob，`_authorize_requested_user` 会拒绝；该竞态需要同 scope 下受支持的管理/内部所有权转移。不是“任意用户可直接删除任意 Chat”，但旧授权跨所有权变更继续有效，仍是不完整的删除授权链。

建议将 expected_chat、授权身份或版本贯穿到删除事务，持有 Chat 行锁后核验；不匹配返回冲突或不可见。候选查询后的 Session 关联检查无法覆盖授权到候选查询之前的变更。

## CRUD 与生命周期代码覆盖

下列判断限于已读代码路径，文件均在 `src/swe/app/conversation_storage/`，除非另有前缀。

| 范围 | 实际检查与判断 |
| --- | --- |
| Session 创建 | `chat_repository.py:639` 使用完整 SessionIdentity 唯一键插入，竞争返回既有 ID；不会按文件名猜身份 |
| Chat 创建/草稿 | `publications.py:511` scope guard 后锁 Session；`chat_mutations.py:233` 在锁下分配顺序及 draft 指针；`chat_repository.py:843/881` 响应丢失后复用 winner，墓碑不复活 |
| Chat 读取/列表 | `chat_repository.py:51/120` scope 过滤；`:185/221` pin、hydrate 后重查版本和归属；分页保留 OFFSET、双 cursor 排序和 total；summary 纯 SQL，不冒充稳定历史快照 |
| Chat 更新/重绑 | 轻量 metadata 短事务直写 SQL；source/target Session 按顺序锁，busy 拒绝重绑；同时更新 external_session_key；专家绑定独立 revision；客户端 status 不覆盖 Turn 权威 |
| Session 保存/恢复 | `session_adapter.py:251/327` 完整身份、pin/hydrate、恢复定位重验、state_revision CAS；活动 Turn 要求 execution authority；数据库故障不回退 JSON |
| Chat 删除 | `chat_repository.py:595` scope candidates、Session/Chat 排序锁、重验关联后 tombstone；保留 Session/Turn/share，不调用旧 NAS 目录清理；已删行不被 upsert 复活。授权快照未贯穿，见 F4 |
| 在线历史 | 固定 high 查询和 pin 存在；**共享 Session reset 重验不完整，见 F3** |
| archive/checkpoint | archive cursor 包含 scope/Chat/epoch/high，按序分页并重验 Chat；checkpoint full 循环固定 high、校验连续范围，超预算显式失败而非截断成功 |
| 分享创建/读取 | `shares.py:173/229` 安装前校验 selection proof 和当前 occurrence；`:634/740` 独立 token 读取，不依赖原 Chat 存在，pin 后重验 locator 再计数 |
| GC/pin 竞争 | `gc.py:160/349` READ COMMITTED 下锁 guard/publication 后复查保护并标 deleting；`retention.py:378` 新 pin 要求 committed/live，不能复活 deleting 对象 |
| 依赖闭包 | `retention.py:490/537` 仅引用更早且同 scope 的 committed/live publication，source 物理 reclaimed 后才释放依赖 |

### 删除到物理回收尚非完全闭环

`retained_history` 在归档及导入时置 1，未找到清零或专用释放路径；`gc.py:197` 将它作为保护。因此删除 Chat 后，其专属旧 checkpoint/event bundle 仍可能长期不能回收。设计允许保留有效历史，并未承诺立即清理，故此项属于保守保留及释放能力边界，不归类为数据丢失 bug；但不能宣称物理清理生命周期已经完备。

## 迁移主链代码覆盖

| 设计要求 | 实际逻辑与判断 |
| --- | --- |
| 首次整体停写、生成新 bundle | `conversation_storage_v3_cmd.py:213` 固定应用 schema、named lock、server UUID 与各服务 NAS roots；`importer.py:517` 遍历导入，符合当前目标；停旧 writer 由部署保证 |
| 只接管本作业数据 | `journal.py:70/116` 拒绝其他作业，首次六表为空，业务行反向 JOIN 作业 ticket 拒绝未知行 |
| 稳定 ID/operation 与续跑 | `journal.py:217` 按 migration_id/kind/source_key 取得 SQL ticket；committed 批次走 verify，不重复安装；本地文件失败后可从 SQL hydrate |
| 批次安装原子性 | `direct_import.py:103` 业务安装与 journal committed 同事务；NAS 在 SQL 安装前发布 |
| 保全旧语义 | `verify.py:237/275/344` 由正式 adapter 比较完整恢复 state、消息 occurrence、Chat 元数据/删除/归档；`direct_import.py:112` 分享行锁内比对原八字段，仅回填 locator |
| ready 后才能上线 | CLI verify/finalize 再查源/分享/schema；`authority.py:93` 核对 ready、receipt、server UUID、schema、NAS、workspace；不满足直接拒绝启动 |
| 元数据权威切换 | `runtime.py:224/260` 初始化 SQL authority，不就绪抛错；读取由 SQL locator 指向新 bundle，无数据库故障时 JSON fallback |

已读正常冻结主链未发现新的确定性迁移缺陷。源冻结仍是必要前提：verify 检查当前 inventory，不独立证明所有 SQL ticket 与当前源集合双向等值。普通删除通常被目录 mtime/严格路径检查阻断，保留 mtime 的源替换或回滚不在此保证内。分享 finalize SQL 的 all-null locator 删除必须保持零行门禁，不能掩盖漏导。HA server UUID 变化后的恢复/重绑定仍需要现场可执行方案。

## 验证与信心边界

本轮定向回归：`test_online_context_usage.py`、`test_chat_repository.py`、`test_publications.py`、`tests/unit/app/test_chat_api_update.py`，结果 **105 passed，0 failed，0.78 秒**。用于辅助确认修复；F3 来自代码交错推导，不是由这些测试覆盖证明。

未进行业务 NAS、MySQL/TDSQL、最小权限、多 Pod 故障与恢复演练；不能仅由源码证明实际存储系统兑现 fsync/hard-link 和数据库连接/事务行为。尚未穷举所有外部授权中间件、内部工具和业务调用方。

高信心结论是：原 F1/F2 的实现缺口已补上；F3 是当前可达的读后重验缺口，F4 在合法并发所有权转移前提下成立；现版本仍有清理能力与环境验收边界。报告不把“已读路径未发现其他问题”扩大为全系统绝对正确。


## F3/F4 修复轮（2026-11-11）

复核确认两个交错成立，原 F1/F2 判断不变。以下结论替代上文关于“现版本仍有 F3/F4”的状态描述；上文保留问题发现时的调用链，行号对应固定审计源码。

### F3 已修复

在线 `_snapshot` 和 `_turn_snapshot` 同时捕获 Session.context_epoch；`_assemble` 在正文读取前及响应组装完成后，除 Chat 自身版本外重查同 service unit 下的 Session epoch。共享 Session 的另一 Chat reset 提交后，旧快照抛 ConflictError，不返回已隐藏正文，finally 仍释放 reader pins。归档快照不强加在线 reset 语义。快照事务退出后才进行 NAS 读取和重验，数据库连接基线为 autocommit，重验不沿用快照事务的旧读视图。响应的可见性以末尾成功重验为线性化点；不承诺撤回已通过重验的响应。

回归覆盖详情与 tracing 两种组装路径，在正文读取中模拟另一 Chat reset 增加 Session epoch，确认冲突且 pins 收尾。修复前两例均未抛异常，修复后通过；快照捕获测试断言 epoch 被保存。

### F4 已修复

DELETE API 将授权时 ChatSpec 通过 `expected_chats` 传入 ChatManager 和 MySQL repository。原有 Session 排序锁、Chat 行锁及关联重验后，在 tombstone 前核对 external_chat_key 对应的 user/channel/external Session 身份；归属不符整个事务失败，API 返回 409。行锁持有至 tombstone 提交，检查后无法再插入并发转移。内部无授权快照的批量删除保持原语义；旧存储分支在 manager 本地锁中比较快照。

回归模拟授权后、候选查询前已合法转移给 Bob，确认不执行 DELETE 更新；同时覆盖归属未变时可成功删除，以及 API 快照传递与 409 映射。该修复刻意只核对归属，不因无关标题更新拒绝授权仍有效的删除。

### 验证范围

执行 `../../venv/bin/python -m pytest tests/unit/app/conversation_storage tests/unit/app/test_chat_api_update.py -q`：**1045 passed，54.41 秒**。格式调整后定向复跑历史、Chat repository、批处理和 API 测试：**76 passed**。相关文件的 pre-commit（含 mypy、black、flake8、pylint）全部通过。未连接现场 MySQL/TDSQL、NAS 或多 Pod 环境；迁移、分享和 GC 的既有边界没有被本次局部修复消除。以后新增共享 Session 的可见性变更，必须同步推进 context_epoch；授权后的写操作必须在持锁事务内重验授权所依赖的身份。
