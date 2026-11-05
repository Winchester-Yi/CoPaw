# Checkpoint 读取契约与持久化 plan 容量：实现和验证

日期：2026-11-05，Asia/Shanghai。工作区：`conversation-mysql-nas`，基于 `b0cd9ee24`。本工作包按用户后续指令单独提交，不包含工作区原有的其他改动。

按[实施计划](../plans/2026-11-05-checkpoint-read-contracts-plan.md)完成已确认的逐页保护设计。未变更 schema、索引、存储格式或单批 pin 上限；本包不等于所有长会话及 compact 容量问题已经解决。

## 读取契约

| 方式 | 实现入口 | 成功语义 |
| --- | --- | --- |
| record | `ArchiveReader.checkpoint_record` / facade `read_checkpoint_record` | 只读 active checkpoint，验证身份、epoch、revision、watermark；不查事件 locator，不读取事件正文 |
| prefix | `checkpoint_prefix` / `read_checkpoint_prefix` | 1..4096 条的有界前缀，显式返回 `has_more` 与 `next_cursor`；保留 record/events/current_epoch 消费字段 |
| page | `checkpoint_page` / `read_checkpoint_page` | 内部 typed cursor 固定 Chat/Session/epoch/revision/active publication/high，继续读取同一捕获尾部 |
| full | `checkpoint_full`、无 limit 的旧 `checkpoint`、facade `read_checkpoint_state` | 完整覆盖捕获 high，或明确失败；不再以默认 4096 条窗口充当完整历史 |
| 流式完整遍历 | `iter_checkpoint_pages` | 必须正常遍历至结束才证明完整；每页交付前已排空并释放 IO/pins，最后一次 yield 恢复后再次复核当前版本 |

分页结果只是一页，不能凭收到末页就宣告完整校验成功；取消、中途停止及尾部冲突都不能变成完整成功。捕获后的正常 append 不进入本次结果，允许 event_high 增长；epoch/revision/active 替换、high 倒退、删除、缺页、重复/重叠 publication 和坏尾部明确失败。

## 引用保护、NAS 和成本

- `checkpoint_reader.py` 集中管理读取，`ArchiveReader` 保留已有 evidence 等入口。每页短事务查询当前身份和事件 locator，并用 `ReaderPins` 保护所需 publication；NAS 在事务外执行。
- active 正文单独 pin，读入已验证内存对象且 IO 排空后释放；后续页不再读 active 正文，因此不持有跨整个历史的 active pin。active 与事件共用 publication 时仍按每个实际 NAS 操作保护。单批上限继续为 4096，不通过扩大上限解决历史长度。
- 查询按现有 `(chat_id,epoch,event_last,id)` 索引顺序推进；读取 `limit+1` 个 locator 作为重叠探针，超过合法页容量直接失败，额外项不 pin、不读正文。实际 MySQL 执行计划尚未验收。
- 每个读到的事件 extent 完整解码、校验序号、epoch 和事实，即使其中部分事件位于本次返回窗口之外。流式遍历与 full 请求内只保留最后一个已验证 extent 的缓存，跨页共用 extent 不重复 NAS 读取；缓存命中仍执行当前 SQL/pin 检查，不替代 GC 保护。
- 沿用 PinBatch 的 shield/drain：重复取消后仍等实际 worker 完成，随后释放 pin；失败路径也释放。不会因取消异步等待而提前解除仍在运行的 NAS IO 保护。

本包减少了不必要的 active/event 读取和全量数组驻留，但没有批量重写 RetentionStore 的逐成员 SQL。完整正文验证仍有 O(完整尾部正文) 的读取/校验成本，不能称为 O(页大小) 的总工作量或零 NAS。

## 容量与兼容边界

- full 数组默认安全阈值为 **65536 个事件、256 MiB checkpoint/event extent 读取字节**，支持通过 `checkpoint_full(max_events=..., max_bytes=...)` 显式指定。事件数量在查事件前预检；字节按 SQL locator 在读取正文前逐页预检。两项均包含完整成功语义，超限抛出明确存储不可用错误。
- 256 MiB 不是精确 Python 堆内存上限，不包括 SQL/Bundle 索引等其他分配或 IO，也不是生产 SLO；默认数值尚未经生产容量测量。单页同样默认受此字节预算限制。
- 流式遍历按页消费，不累计完整事件数组，不受 full 数组 65536 的累计事件数限制；它仍受每页的字节边界和现有 extent 读取约束。超过单页预算时可用更小 page_size 遍历；单个不可读 extent 不能靠减小 page_size 绕过完整性校验。
- prefix/page 每页仍上限 4096；需要更多事件必须续页或完整遍历。旧 `checkpoint(chat_id, limit=...)` 保留显式前缀调用方式并增加分页事实；无 limit 改为完整读取。
- 以上为数据库 indexed archive 的明确契约；文件后端旧接口/格式和原有 HTTP 响应没有被缩减或替换。

## 调用方调整

1. facade 的 record 路径不再绕道 full；head 增加 active publication 用于核对捕获身份。
2. compact 只读取源消息数量的 prefix，用该页的 epoch/revision/active/high 对照准备时 metadata，再继续既有发布 CAS；degraded checkpoint 只取 record。
3. 摘要投影完整遍历事件以验证尾部，但用 `deque(maxlen=5)` 仅保留最后五条，不再为摘要累计完整事件数组。
4. migration 与源尾部逐页、逐项精确比较，验证剩余数量与 record，必须正常穷尽；restore 也穷尽同一完整读取器，不能只验证首个窗口。零事件仍验证 record。
5. offline `ReadOnlyRetention` 仍只用于明确停止 writer/GC 的边界，不新增在线无 pin 旁路；单元测试实际接入 ReadOnlyDatabase/adapter，完整跨越 4096 而不写 SQL。

## 持久化 plan 最终字节校验

`CommitPlan.serialize` 对最终 `json.dumps` 结果做实际 UTF-8 字节检查，超出既有 `MAX_PLAN_BYTES=1 MiB` 抛 `ValueError`。reserve 本来就在 transaction、SQL 和 NAS 之前调用 serialize，因此无需改变发布顺序。parse 仍将非法或过大内容映射为 `IntegrityError`，正常写入不额外完整 parse 一遍。

注意后续 `effective_recovery_plan` 在读取 NAS projection 后扩展的内存安装 plan 并非本次持久化的 plan 文本。本包未实现它、compact 模型调用前后、envelope/index/bundle 的全链路容量准入及错误终态；这些仍属于下一工作包。

## 验证证据

- 先复现 4097 个实际事件只返回 4096；独立 active 的边界和共享 publication 同样纳入测试，未降低 `MAX_PIN_BATCH`。
- plan 真实 1 MiB−1/等于/+1 字节覆盖 ASCII、中文、emoji、引号与反斜杠转义；合法 4096 条消息仍超容量、实际 reserve 进入禁止 SQL 的问题均先红后绿。
- 追加完整性回归：页容量隐藏重复 publication、末页 yield 后 reset、事件/字节预算必须在事件 NAS 前拒绝、续读删除冲突、坏 extent 尾部、重复取消排空、同 extent 跨页只读一次。
- 扩大回归：**865 passed、16 skipped，52.63 秒**。范围为全部 `tests/unit/app/conversation_storage/`、六组 checkpoint/compaction agent 测试和本包显式 opt-in MySQL 测试。
- 格式调整后的专项复验：**67 passed，13.07 秒**（checkpoint reader、plan capacity、checkpoint consumers）。改动文件的完整 pre-commit 全部通过，最终 `git diff --check` 通过。
- 独立 SPEC review 与代码质量 review 均通过。

运行命令（项目虚拟环境）：

```bash
../../venv/bin/python -m pytest \
  tests/unit/app/conversation_storage \
  tests/unit/agents/test_chat_checkpoint.py \
  tests/unit/agents/test_chat_checkpoint_store.py \
  tests/unit/agents/test_checkpoint_commands.py \
  tests/unit/agents/test_checkpoint_journal_v2.py \
  tests/unit/agents/test_checkpoint_memory_attachment.py \
  tests/unit/agents/test_memory_compaction_checkpoint.py \
  tests/integrated/conversation_storage/test_checkpoint_reader_mysql.py -q
```

### 真实环境验收仍未完成

16 个 MySQL 用例覆盖实际五种事件规模、独立/共享 active、共享事件 extent、捕获后 append、epoch/revision 变化、取消排空期间真实 SQL pin 与 GC 拒绝。使用真实 BundleStore 和批量 SQL 种子，不以降低边界或数据库替身冒充验收。

本次缺少显式专用测试库配置，全部 16 项跳过，未连接业务库，也未读取开发配置来寻找连接。真实 MySQL SQL/锁/GC、NAS 挂载属性与跨实例行为、EXPLAIN 和部署性能仍未验证；单元测试替身、本地文件和代码复核不能替代这些验收。
