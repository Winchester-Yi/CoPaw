# SWE/Monitor Token 观测底座 A：实施设计

日期：2026-10-12。状态：用户已确认继续实施，专门 Usage 核对工作移出本轮。

依据：[MVP v1.0](../../../Agent_Token_效率诊断平台_MVP_方案设计_v1.0.md)、[提交差距分析与决策记录](../reviews/2026-10-12-token-diagnostics-mvp-gap-analysis.md)、[ADR-0037](../../adr/0037-token-diagnostics-own-records-and-share-tracing-enablement.md)。

## 已确认边界

- 首批 SWE/Monitor 增量开发，记录事件版本与扩展边界；不建设外部 Agent/Gateway 通用接入。
- 本轮交付 A 的合规存储、任务归因、事件及完整性状态。专门 Usage 核对脚本、真实 Provider 对照与核对报告暂缓；B 的前缀索引、三页面/八规则、C 的反馈/实验/策略后续展开。
- 一个 Token Diagnostic Task 对应一次根请求目标执行，包含 Main Agent、SubAgent 和辅助模型调用。附着/恢复同一执行复用 Task，后续请求建立新 Task。
- Token 继续独立记账，Trace/Span 可选关联；无 Trace 或 Trace 写入失败不阻止 Token 记录。
- 保留 Provider Usage 采集、来源与未知/缺失状态。用户暂缓专门核对工作后，本轮不以真实 Usage 对照、核对覆盖率或 ≤1% 偏差作为交付门槛；不宣称真实对账通过。
- 业务优先、非阻塞、有界缓冲；记录缺口与完整性并验证故障行为，不承诺崩溃后恢复交付。
- 用户已确认五张 Token 表尚未建立：直接修订初始 schema 和读写代码，无旧数据迁移、JSON 兼容读写、回填或双写。
- 用户接受 A 的 messages_only 近似采集边界。Q9 原确定的受控核对集合 100% 覆盖要求随专门核对工作一并暂缓；基础自动测试仍验证预期调用全部写入、记录幂等和未知/缺口状态。

## 工程约束

- 继续同一 MySQL database 的独立 Token 表、连接池和事务；不借用 Trace 事务。
- 全表有主键，表及字段均有注释；禁止 ENUM/SET/JSON/LOB 等类型、外键、视图与数据库作业；查询列显式列举。
- 将现有 payload/analysis_summary 的标量改为明确列，多条关系/规则证据改为有主键的明细；具体 DDL 随事件字段模型收敛，不用大文本载荷绕过规范。
- 保持已有五规则和 Console 查询兼容；字段重构不能静默改变其结论或证据语义。
- 缺 Usage 使用 null，明确零保留为零；真实 Usage 与估算分开。流式累计快照不相加，每个可观察重试分别记账；缓存/推理子项按 Provider 语义规范化一次。
- 只记录允许的数字、元数据和域内 HMAC；原始 Query/Prompt/工具参数/结果不默认持久化。
- 使用公共 schema_version 固定采集契约，不把 SDK 默认生成的 response ID 或本地 call UUID 冒充 Provider 物理请求 ID。缺实际 Provider 关联时标记来源/可验证范围。

## A 的采集与验收边界

### Q8：Context/事件深度

A 保留并结构化 Task/执行节点、LLM、Tool、准备输入 Context，以及实际发生的上下文操作。Context 与 Call 一对一；记录顺序、类别、来源、长度/估算、采集阶段/版本与完整性。用户已接受该近似采集边界。

messages_only 明确为消息级近似。服务端模板、Tokenizer、Token 偏移和不可得的变换前后量保持未知；不据此宣称精确前缀或精确 Segment 归因。跨轮 Segment 生命周期、Token 前缀分块与匹配分析在 B 交付。A 的历史记录不保证能回补这些尚未采集的数据。

工具的只读/副作用/版本信息仅使用可靠声明，没有声明就记录 unknown，不能按名字推断。压缩事实仍保留，缺 before/after Context 时标明未采集。

### Q9：原核对门槛暂缓，基础完整性验证保留

用户曾确认正常受控核对集合 100% 覆盖、无重复和汇总偏差 ≤1%；随后明确暂不考虑专门 Usage 核对工作，因此这些真实核对门槛不属于本轮验收。

本轮基础自动测试使用明确数字的受控响应，验证正常记录不漏、不重、数字正确、流式累计快照不重复相加；缺 Usage、中断或故障时状态与已知数字保留正确。这些属于采集/存储回归，不能替代真实 Provider 对账。

运行期覆盖必须给分母来源、进程/窗口边界及可验证范围；完全丢失的进程内计数无法从 Token 表恢复时标记 unverified，不声称全局完整覆盖。

### Q10：专门真实核对工作暂缓

用户明确暂不考虑这块工作。本轮不实现独立参考采集/对照脚本、核对报告、专用核对接口或相关产品页面，不需要选择首批真实 Provider/模型。

后续恢复时，核对须针对同一次请求的 Provider 原始数字与账本，建立独立参考清单并单列未知、缺失、重复、数值差异；不能使用另一次相同 Prompt 的响应或账本自身汇总作独立基准。该方向保留为后续设计背景，不是本轮实现任务。

## 实施分解与验收

| 步骤 | 本轮实现结果 | 验证 |
| --- | --- | --- |
| 合规存储 | 五张主表的 JSON 载荷拆为明确字段及必要关系明细，补主键/注释；两侧 Store、分析写入和清理同步调整 | 初始 DDL 规范检查；隔离 MySQL 写入/读取/清理；显式列查询；旧五规则证据可往返 |
| 请求归因 | 一次根执行一个 Task，记录聊天轮次/定时执行等入口关联；子 Agent/辅助调用使用共同 Task 及独立分支 | 同执行附着/恢复不重建，后续执行不混账；并行分支与租户隔离 |
| 事件/Context | 固定事件版本和可审计来源；Task/执行节点、Call、Tool、messages_only Context 与实际操作关联 | 父子路径、顺序、调用关联及降级字段；缺不可得信息不虚构 |
| 完整性与兼容 | 保留已知尝试/子分支、未知 Usage、gap/unverified 状态；现有查询与 Console 可继续使用 | 受控数据正常不漏不重；数据库失败/超限/关闭/退出故障；五规则和前端回归 |

身份优先使用已有执行关联（Answer Turn 等），关联不可得时以独立 Token 执行身份记账并标记来源；不以 session_id/B3 ID 代替执行身份。步骤树按实际观测动作建立，不推断未知业务阶段。

代码层事件可以承载结构化内存对象，但数据库层不得保留通用 JSON payload。状态值、字段清单和字段映射以固定版本契约定义；保留现有查询语义，避免把存储调整扩展为接口重设计。

## 设计验证入口

- 合规 DDL 在隔离测试 MySQL 建表，SWE 写入、独立 Monitor 查询/分析，验证重放幂等、隔离与清理。
- 请求级归因覆盖聊天/Channel、定时任务、子 Agent、标题/压缩等现有入口；附着/恢复不重新生成 Task，实际新执行分别记录。
- 自动测试使用受控 Provider 响应验证流式 Usage、明确零/未知、错误/中断、可观察重试与落库；不发起真实 Provider 核对。
- 故障覆盖数据库不可用、队列限额、关闭超时、进程退出；采集无法验证时显式降级，不能用已知部分伪装完整总量。
- 项目 venv 的 pytest、现有前端兼容测试及变更范围质量检查；代码编辑前执行 GitNexus impact，交付前分析图变更。

Q1–Q8 的核心边界继续有效；Q5/Q9/Q10 中专门真实核对工作按用户最新要求暂缓。实施分解已获授权，按下列固定采集契约交付。


## 固定采集契约

- `schema_version=1.0`，`telemetry_source=swe_token_diagnostics`；运行记录、调用和事件共同携带执行身份与 Agent 版本。
- 根身份使用 source/tenant 范围内的 AnswerTurn 三元组或 `cron_persistence_key`；无法取得时独立生成并标 `identity_source=token_generated`。Task 与根分支共享执行标识，子分支拥有独立 ID。
- 每个可观测模型尝试独立生成 `step_id`/`context_id`，父步骤指向执行分支；工具开始和结束共享步骤及开始时间。外部服务调用有步骤但 Context 不可见，`context_id=null`。
- 采集阶段 `messages_only`，格式置信度 `message_approx`，组装逻辑 `prepared-messages-v1`；媒体标 `multimodal`，采集失败标 `unknown`。Tokenizer、模板与精确 Token 边界本轮不采集。
- `usage_origin=provider|unknown`、`usage_accuracy=reported|unavailable` 表示数字来源；缺部分字段/中断继续由 `usage_status=partial` 表示，二者不能互相替代。
- 工具安全属性目前没有可靠声明接入：`tool_kind`/`idempotency_class=unknown`、`is_cacheable=false`、`data_version=null`；压缩事件记录实际 epoch，变换前后 Context 尚未采集。
- 数据库共 11 张类型化关系表：五张主表及预期子分支、规则评估、缺失字段、三类证据引用表。初始 DDL 没有历史兼容或迁移路径。
