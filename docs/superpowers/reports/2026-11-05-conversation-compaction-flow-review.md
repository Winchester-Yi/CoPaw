# 会话压缩当前流程梳理

日期：2026-11-05。范围：`feature/conversation-mysql-nas-migration`，重点为 MySQL/NAS 在线路径。

本文记录当前实现，供确认摘要生成和组装的调整方向；不代表已批准的新设计。本轮未修改运行时代码。依赖检查基于当前虚拟环境的 `reme-ai 0.3.1.8`、`agentscope 1.0.18`。

## 2026-11-06 排期决定

按用户决定，本报告涉及的 Compact 容量、错误终态、摘要生成与生成后组装调整，统一记录为 **NAS 迁移功能完成后再处理** 的待办。本轮仅提交现状分析和待办记录，不实施优化；该排期决定不代表上述问题已修复或验收通过，也不冻结其他 NAS 迁移工作。

恢复本项工作时，先复核届时的代码与依赖版本，再确定设计，重点处理：摘要输入覆盖与归档范围一致、checkpoint 与 ReMe 摘要语义、旧摘要更新及最终上下文组装、模型调用前后容量校验、容量失败与 UnknownCommit 的错误结算。

## 1. 先区分四种不同操作

| 操作 | 实际作用 | 是否生成模型语义摘要 |
| --- | --- | --- |
| tool-result compact | 将大段工具结果转成可恢复的 artifact，缩短消息正文 | 否 |
| checkpoint 预压缩候选 | 从消息事件提取 message_id、role、证据引用，追加到结构化 checkpoint | 当前否 |
| context compact | 旧摘要和待压缩消息交给 ReMe，生成新摘要，归档原消息 | 是 |
| async summary | 独立的长期记忆提取任务，调用 summary_memory 和其 toolkit | 是，但返回值不直接作为本次 `_compressed_summary` |

这四者可能在同一次自动压缩流程中出现；后台候选与长期记忆任务也不是同一个任务。

## 2. 自动入口与触发判断

入口是 `src/swe/agents/hooks/memory_compaction.py:MemoryCompactionHook.__call__`，注册到 Agent 的 `pre_reasoning`。

顺序如下：

1. 热加载 Agent 配置与 token counter。
2. 计算剩余压缩阈值：`max_input_length × memory_compact_ratio - tokens(system_prompt + old_summary)`。
3. 如果该值不大于零，只记 warning 并退出；本次连后面的工具结果压缩也不会执行。
4. `get_memory(prepend_summary=False)` 获取消息，然后按配置执行工具结果压缩。
5. 若 context compact 关闭，退出。
6. ReMe `check_context` 选择待压缩消息及保留消息。
7. 另算 projected tokens，按 checkpoint 阶段决策。
8. checkpoint 分支不足以降低占用时，重新获取当前消息、重新选择一次待压缩集合，进入 ReMe 语义摘要回退。

默认阶段如下，可由配置调整：

| 估算占用 / max_input_length | 当前行为 |
| --- | --- |
| 小于 65% | 不做 context compact |
| 65% 至小于 80% | 启动后台候选；每增加 5 个百分点允许准备更新的候选，同一 chat/epoch 不重叠启动 |
| 80% 至小于 90% | 优先安装就绪候选；未安装或安装后仍处于 active/emergency，进入 ReMe 回退 |
| 大于等于 90% | 优先候选；没有安装候选时写 degraded checkpoint，重新计数，必要时进入 ReMe 回退 |

这里的 projected tokens 是“消息字典计数 + system prompt/旧摘要文本计数”，不是最终请求的完整计数：没有在此统一计入 toolkit schemas、formatter 包装和所有运行时提示。

## 3. 哪些消息被选择，哪些被保留

ReMe 的 `AsMsgHandler.context_check` 当前逻辑：

- 消息总量不到剩余压缩阈值时，不选择待压缩消息。
- 达到阈值时，从最新消息向前选取，保留预算默认为 `max_input_length × 10%`。
- 对工具结果补上对应工具调用，避免保留一个孤立的 tool_result。
- 不在保留集合中的消息进入待压缩集合。返回值同时检查保留集合的 tool ID 对齐。

这不是固定“保留最近三条”。只有异常/候选回退等路径使用 `MEMORY_COMPACT_KEEP_RECENT`，环境变量缺省为 3。无效切分回退会递减保留条数，直到尾部的 tool_use/tool_result ID 集合匹配；必要时可能选中全部消息。

checkpoint 候选优先使用上述待压缩集合；没有时，使用去掉最近三条的消息。再从候选尾部逐条缩短，直至工具 ID 集合匹配。实际存储还要求它是精确的在线前缀；不能把这一检查理解为完整的工具事务语义证明。

另外，依赖的 `get_memory(prepend_summary=False)` 只关闭压缩摘要注入，**并不关闭长期记忆注入**。长期记忆非空时仍会返回合成消息，是否存在于实际请求取决于当时的 memory 状态。这种合成消息不属于在线持久化 occurrence，可能与前缀校验冲突。

## 4. 后台候选实际生成什么

入口：`reme_light_memory_manager.py:schedule_precompaction`。

1. 校验所选消息 ID 和在线消息 ID 各自唯一。
2. 按所选消息数量读取 checkpoint prefix。
3. 要求事件连续，且逐项为对应消息的 `message_added`，facts 正好为 message_id/role，引用为 `message:<id>`。
4. 为每个事件构造 EvidenceItem，文本类似 `message_added: {"message_id": "...", "role": "user"}`。
5. 将这些项目追加到旧 record 的 critical_context，推进 revision 和 applied_event_sequence。
6. 写入 pending candidate；indexed memory 保存 candidate ID。

**本方法丢弃传入的 model/formatter 参数，不读取消息内容进行语义提炼，不调用摘要模型。**

安装时，`ArchiveBinding.compact(candidate_id=...)` 读取 candidate，调用 `render_checkpoint_projection` 得到 Markdown，替换 `_compressed_summary`，同时归档源消息。

投影会输出目标、约束、进展、决策、下一步、关键上下文；record 主体没有总 token 上限。只有额外的 Recent Event Delta 默认限制为 5 条、1200 字符，不能据此推断整份摘要受限。

因此，事件 ID/角色本身不会表达原消息中的任务要求、分析和结论；当 record 没有其他路径提供的语义内容时，归档后的模型上下文主要得到事件事实而非消息摘要。

## 5. 紧急 degraded 分支实际做什么

入口：`archive_binding.py:degraded`。

读取 active checkpoint record，将 confidence 改为 degraded，追加“保留证据引用”的 EvidenceItem，写 active checkpoint，再生成投影。

**此分支没有调用 archive compact，也没有从在线 content 移除所选消息。** 因而不能保证缩小上下文，可能同时保留原消息并新增摘要文本。hook 会重新计数，仍偏大时继续 ReMe 回退。

## 6. 真正的摘要如何生成

入口：`reme_light_memory_manager.py:compact_memory` → ReMe `Compactor.execute`。

自动路径绑定当前 Agent 的 model/formatter。手动路径未绑定这两个参数，由 memory manager 创建执行用的 model/formatter。

生成过程：

1. 将所选消息转换成文本，可按配置包含 thinking block，默认包含。
2. 创建独立的 `reme_compactor` ReActAgent，使用专门的压缩 system prompt；此处未传业务 toolkit。
3. 无旧摘要时，输入为“conversation + 创建摘要指令”。
4. 有旧摘要时，输入为“conversation + previous-summary + 更新摘要指令”。
5. 提示词要求结构化 Markdown：目标、约束/偏好、进展、关键决策、下一步、关键上下文，并保留路径、函数名、错误信息。
6. 更新指令要求保留旧信息，合入新进展，更新下一步，也允许移除不再相关的内容。
7. 取模型最终文本作为 history_compact。

所以当前已经使用结构化摘要提示词，但输出仍是字符串；没有将输出解析成并校验为 CheckpointRecord。

当前基本合法性检查只有“非空且包含 `##`”。没有摘要总 token/字节预算、完整字段验证或生成后上下文目标校验。提示词中“保留旧信息”的要求也可能让摘要逐轮增长，但这不表示每次一定增长。

### 输入覆盖存在一个独立风险

当前 ReMe `AsMsgHandler.format_msgs_to_str` 从所选集合的最新消息向前格式化，较旧消息加上后超过阈值就停止；单条最新消息超过阈值时仍强制包含。

之后归档使用的仍是**原先选择的整个集合**，没有用实际送给摘要模型的消息集合修正归档范围。

示例：选择 M1…M100，格式化预算只容纳 M70…M100，则模型只能总结后者；但成功后 M1…M100 都可能被移出在线上下文。原文仍在归档中，并非物理删除，但 M1…M69 不一定被新摘要覆盖。

旧摘要、压缩指令和压缩 system prompt 也是在消息格式化预算之外加入的，因此该阈值不等于完整摘要请求的输入上限。

## 7. 成功后如何持久化与组装

### MySQL/NAS 提交

入口：`archive_binding.py:ArchiveBinding.compact`、`archive_runtime.py:compact`。

1. 从 memory.content 精确匹配所选 occurrence，构造保留 content。
2. 以“保留 content + 新 summary”构造完整 recovery root。
3. 查询在线前缀索引，建立 reader pin，从 NAS 读回原消息并逐项核对。
4. 读取 checkpoint prefix，校验 epoch/revision/active publication/high；candidate 路径还会验证候选。
5. 创建 archive boundary、归档 Message occurrence、checkpoint、messages_archived 事件与 prune 集合。
6. reserve → NAS bundle 写入 → 元数据提交。归档沿用源消息 locator，不为每条源消息再复制一份正文。
7. 确认提交后替换 live content 和 `_compressed_summary`，更新 execution 的已提交状态。
8. 自动/手动入口再调用 update_compressed_summary；输出 compaction boundary metadata 供界面展示。

ReMe 字符串路径与 candidate 路径有一个区别：ReMe 新摘要写入 recovery root，但 `archive_runtime.compact` 本身没有把这份文本转成 checkpoint 的语义字段；candidate 则以 record 渲染结果作为新摘要。两条路径不能简单视为相同内容的两种编码。

### 下一次主模型看到什么

`ReMeInMemoryMemory.get_memory()` 默认将长期记忆和压缩摘要包装在**同一条合成 user 消息**中，再接剩余原始消息。随后 AgentScope 的 formatter 与 system prompt 一起转换成模型格式，并另传工具 schemas。

逻辑上为：

```text
system prompt
→ 合成 user 消息（长期记忆 + 当前压缩摘要，有哪个放哪个）
→ 保留的原始消息，维持原角色和顺序
→ 适用的本轮临时提示/内部上下文
→ formatter 处理后的请求；工具定义作为 tools 参数
```

摘要包装包含“这是之前对话的摘要，请作为上下文保持连续性”的说明；**不是将摘要展开成原来的多条对话，也不是维护多份摘要轮流拼接**。

例如：第一次 `S1 = summarize(M1…M80)`，保留 M81…M100；后续 `S2 = summarize(S1, M81…M150)`，组装时用 S2 替换 S1，再接剩余消息。上述式子表达意图，实际消息覆盖受第 6 节的格式化截断影响。

依赖包装仍写着归档位于 `dialog/YYYY-MM-DD.jsonl`，与 indexed NAS bundle 的实际存储位置不一致。

## 8. 手动 /compact 与自动的区别

| 项目 | 自动压缩 | 手动 `/compact` |
| --- | --- | --- |
| 时机 | 每次 reasoning 前按阈值判断 | 用户命令 |
| 源消息 | check_context 选择，通常保留近期消息 | get_memory(prepend_summary=False) 返回的全部消息 |
| checkpoint 阶段策略 | 有候选与 degraded 分支 | 直接 ReMe 摘要 |
| 模型 | 当前 Agent 绑定 model/formatter | manager 创建执行模型/formatter |
| 旧摘要 | 参与更新，新摘要替换旧摘要 | 相同 |
| 长期记忆任务 | 按开关启动 | 此入口直接启动 |
| 摘要为空 | 状态提示后退出 | 返回 Compact Failed 消息 |
| indexed 成功响应 | boundary metadata | boundary metadata |

长期记忆任务启动在本次摘要/归档成功之前；它的完成不证明本次 context compact 成功。

## 9. 当前容量检查与错误路径

| 层级 | 已有约束/行为 | 当前缺口 |
| --- | --- | --- |
| compact source | 最多 4096 条，否则 ValueError | `_source_rows` 中才检查，ReMe 路径此时通常已经花费摘要调用 |
| CommitPlan | messages/prune/result 等集合上限，最终 UTF-8 JSON ≤1 MiB；reserve 前检查 | 不是整个 compact 模型前的预检 |
| recovery projection | additions/prune 各自有限额 | effective plan 合并后未在该函数做统一最终校验；不能用两边分别合法推导合并合法 |
| bundle | blocks ≤4096，metadata/table 各 ≤4 MiB，bundle ≤256 MiB | 与 plan 条数、摘要 token 限制不是同一个维度 |
| 普通 extent 读取 | 单次 read_extent ≤8 MiB | bundle 能写入不等于所有消费读取路径均能读回任意大 extent |
| 摘要输出/最终请求 | 基本非空、标题标记检查 | 无整体输出预算，ReMe 成功后没有本 hook 的最终硬预算拦截 |

错误处理需分开理解：

- 自动 hook 最外层捕获 Exception，只记录日志后返回；空摘要也返回。因此不能靠该 hook 保证确定性容量错误会阻止后面的主模型调用。
- indexed binding 遇 UnknownCommit 会保存 pending operation/request 并重新抛出；后续执行层还有 pending/存储检查，所以“hook 吞掉异常”等于“unknown 后一定继续执行”并不成立。
- Runner 对 UnknownCommit 和 StorageError 分别传播；普通异常走 failed，取消有 stopped/interrupted 路径。**存储错误并不自动等于 durable failed 终态。**
- 本节是现有分支说明，不表示已经完成容量错误、摘要失败和未知提交的统一结算设计。

## 10. 本轮核查证据与边界

- 读取上述仓库实现及当前安装的 ReMe/AgentScope 源码，未调用外部模型。
- 聚焦回归：memory_compaction_archive、memory_compaction_checkpoint、context_compact_config、archive_runtime、commit_plan_capacity，合计 **70 passed**。这些测试通过不代表上述流程风险已经修复。
- 对实际 AsMsgHandler 做最小实验：使用字符长度替代 token 计数，选择三条消息，预算只够一条时输出只包含最后一条；预算为 1 时仍输出 129 字符的最新消息。此实验验证格式化分支，不是实际模型 token 数或生产性能基准。
- 未执行真实 MySQL/NAS 集成、模型摘要质量或完整 Runner 错误结算演练。

后续讨论可直接围绕三件事展开：哪些原消息确实交给模型总结、旧摘要怎样被更新、最终哪段摘要与哪些原消息一起交给主模型。本文先保留现状，待用户确认后再制定调整方案。
