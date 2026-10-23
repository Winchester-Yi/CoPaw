# Stop 附加消息扩展设计

状态：已批准并实现。Q1–Q15、字段和交付边界均已确认；专项验证与最终回归记录见对应实施计划。

## 设计诉求

在模型回复结束后，由 Stop Hook 返回内容，追加一条用户可见消息。
对话中的主要动因是保留原回复的流式输出，避免为了追加内容而启用全文替换所需的暂存。

## 扩展前事实与边界（设计基线）

- Stop 是候选回复的完成门禁；普通请求在 block 后可能自动续跑，再次触发 Stop。
- Goal 仅在正式交付的 Finalization 阶段使用 Stop；Finalization 重试不会重新执行 Goal。
- `outputTransform: true` 通过 `hookSpecificOutput.replacementText` 完整替换候选正文。
  存在潜在匹配的转换器时，正文在条件求值前即被暂存。
- 普通 Stop 输出只接受 allow/block 决策，不允许非空 `hookSpecificOutput`，
  也明确拒绝 `systemMessage` 等效果字段，因此扩展前没有可直接使用的追加消息返回协议。
- 现有完成流程可以发送独立消息，但正常 Stop 放行分支尚未消费附加消息。
- 本节保留扩展前调查基线；新增的独立追加阶段、返回协议和 Console 展示规则见下文。

事实来源：

- [Stop 统一完成门禁 ADR](../../adr/0020-stop-is-the-unified-completion-hook.md)
- [Stop 严格两阶段收尾 ADR](../../adr/0029-stop-output-transformations-use-strict-two-phase-finalization.md)
- `src/swe/agents/hook_runtime/output.py`
- `src/swe/agents/hook_runtime/runtime.py`
- `src/swe/app/runner/turn_lifecycle.py`

## 决策树

第一轮已确认：

1. 仅最终完成尝试放行后展示；被否决尝试的附加内容丢弃。
2. 首期只支持文本/Markdown，以独立消息展示。
3. command/http/prompt 使用统一协议；脚本为主要示例。
   prompt 处理器仍会额外调用模型。

第二轮已确认：

4. 附加消息进入会话历史，也进入后续模型上下文。
5. 全部 Stop 校验放行后，再执行独立追加阶段，在本轮请求结束前生成并发送附加消息。
6. 每处理器最多一条，分别形成独立气泡；按固定来源及配置顺序展示。
7. 沿用租户、Agent Profile、已激活 Skill 三种来源，覆盖普通问答与 Goal 正式交付。
   首期保证 Console 独立气泡与历史恢复；其他 Channel 的投递另行支持。

第三轮已确认：

8. 失败处理器跳过，其他处理器继续；不否决正文成功状态、不触发模型续跑。
9. 附加内容不再次触发 Stop，仅校验协议、非空文本和长度；业务内容由处理器负责。
10. 并发执行，全部结束后按固定来源及配置顺序发送。
11. 用户停止时中止剩余工作，保留已交付消息、丢弃未交付结果；
    Q13 将交付精确定义为成功持久化，断线不停止后台执行。

第四轮已确认：单处理器默认 10 秒、追加阶段总预算 30 秒；单条 16 KiB、
每轮累计 64 KiB；先持久化再发送、崩溃不补跑；不支持 once；失败不插错误气泡。

## 展示与存储调查

- 新的 AgentScope Msg ID 可以生成独立 runtime message；`last=True`
  表示该消息完成，整个响应在生成器结束后才完成，因此可在请求内继续发送附加消息。
- Console 现有实时回复聚合与历史转换会把连续非 user 消息合入同一回复区域。
  独立消息需要明确的展示标记，并在实时与历史两条路径中保持分组一致；
  不能仅靠后端生成新的 Msg ID，也不能复用现有 `cron_task` 标记。
- 会话已有 Agent memory 外部展示记录的先例，但本方案明确不采用该旁路：
  附加消息需要进入模型上下文，因此必须作为真实 assistant 消息保存到 Agent memory。
- 普通完成与 Goal Finalization 共用 Stop 入口，但各自具有最终交付位置。
  两条路径都需遵守最终放行后的追加规则。

证据入口：`runner.py::_persist_model_call_failed_detail`、
`api.py::_build_chat_history`、`turn_lifecycle.py::_stream_goal_finalization`、
`turn_lifecycle.py::_stream_standard_completion_lifecycle`、
`console/src/pages/Chat/sessionApi/index.ts::convertMessages`。

## 已确认决策与术语

### 第一轮

- **Q1：** 仅最终放行后展示。被否决尝试的附加内容丢弃，避免一轮回答出现多条过期结论。
- **Q2：** 首期只支持文本/Markdown，以独立消息展示。
- **Q3：** 三种处理器共用协议，与现有输出转换能力一致；脚本作为主要示例。
  prompt 类型本身会调用模型，使用它会增加一次模型请求。

领域术语采用 **Stop Appended Message（Stop 附加消息）**，已同步到根目录
`CONTEXT.md`；它属于同一 Answer Turn，不等同于正文替换或新的用户问答轮次。
其会进入后续模型上下文。

独立消息的前端目标按独立聊天气泡推进，实时显示、历史刷新应保持一致。

### 第二轮

- **Q4：** 进入会话历史，也进入后续模型上下文。实现应保留附加消息本身及其来源标记，
  不能仅在前端临时渲染或作为模型上下文之外的展示记录存储。
- **Q5：** 正文流式输出 → Stop 校验通过 → 生成并发送附加消息 → 结束本轮。
  追加处理器与普通校验器属于不同阶段；未放行的候选尝试不执行追加阶段。
- **Q6：** 每个处理器最多返回一条消息，多个处理器分别形成独立气泡；
  按固定来源及配置顺序展示，不按执行完成的先后展示。
- **Q7：** 租户、Agent Profile、已激活 Skill 三种来源；普通问答与 Goal 正式交付；
  Console 的独立气泡与历史恢复为首期交付范围。其他 Channel 的附加消息投递另行支持。

领域术语新增 **Stop Append Phase（Stop 追加阶段）**，已同步根目录 `CONTEXT.md`。
仅启用追加能力不触发正文暂存；若同时存在潜在匹配的输出转换器，仍沿用其现有暂存规则。
### 第三轮

- **Q8：** 跳过失败处理器，继续其他处理器；保留正文成功状态，不触发模型续跑。
  追加阶段采用固定尽力交付语义，不复用校验器的 `failPolicy: block` 来否决正文。
  失败记录诊断；按 Q15，聊天中不插入错误气泡。
- **Q9：** 附加消息不再次触发 Stop。只校验返回协议、文本非空和长度限制；
  业务内容由追加处理器负责。
- **Q10：** 多个追加处理器并发执行，全部结束后按配置顺序发送。
  追加阶段不是串行文本变换流水线，各处理器使用同一份已放行正文和上下文快照，
  不读取其他追加处理器的结果。
- **Q11：** 用户停止时中止剩余追加工作，已交付消息保留，未交付结果丢弃
  （按 Q13，以成功持久化为交付边界）；
  按现有停止规则结束本轮，不再补发。已发生的外部副作用不承诺回滚。
  单纯断线时后台继续执行，重连恢复。

领域术语新增 **Best-effort Stop Append（尽力交付的 Stop 追加）**。
超时和取消应覆盖进程/HTTP/prompt 执行的资源清理，不能把取消 Python await
误写成脚本子进程必然停止的保证。追加阶段需要主动检测已接受的用户停止，
不能仅依赖模型的 interrupt 信号。

### 第四轮

- **Q12：** 单处理器默认超时 10 秒，阶段总预算 30 秒；到期取消未完成任务，
  保留已成功结果。单条正文最多 16 KiB UTF-8，每轮累计最多 64 KiB。
  超限跳过、不截断；累计上限按配置顺序计算。
- **Q13：** 先保存历史和模型记忆，再发送；保存成功即视为已交付。
  已保存但客户端尚未收到的消息在刷新后仍可见；停止时丢弃尚未保存结果。
  重连只回放已有消息，进程崩溃后不自动重跑脚本，未保存结果允许丢失。
- **Q14：** 每个最终放行的问答轮次最多执行一次追加阶段，不支持 `once: true`。
  同一轮重连不重新执行；后续问答轮次仍可执行。
- **Q15：** 聊天不插错误气泡；失败进入诊断日志和 Hook 人工测试结果。

## 完整运行流程

```text
模型正文流式输出
  → 可选：既有 Stop 输出转换（存在转换器时仍遵守原暂存规则）
  → 普通 Stop 校验
      block → 既有有限续跑/未完成路径，不执行追加阶段
      allow → 正文获准完成，先释放尚未发送的正文最终消息
  → 冻结追加计划与上下文
  → 并发执行追加处理器（各自超时，受阶段总预算约束）
  → 收集成功结果，按固定来源/配置顺序应用长度预算
  → 逐条检查停止状态 → 写入 memory 并持久化 → 发送独立消息
  → 沿用既有本轮收尾和终态持久化
```

这是 Stop 内部新增阶段，不新增生命周期事件，不把完成工作移到请求关闭后的后台任务。
追加消息不会再次进入 Agent 推理循环、Goal 评审、Stop 校验或输出转换。
仅启用追加时不要求正文暂存；同时配置其他输出转换器时，不改变那些转换器的暂存语义。

普通问答和 Goal 正式交付在各自最终放行出口，先发送尚未释放的正文最终消息，
再调用同一追加 helper；已流式交付的正文不重复发送。
尤其是普通 buffer 模式和 Goal Finalization，不能在正文最终消息之前 yield 附加消息。
共用 Stop gate 入口只负责完成判定，不在 `_emit_stop_hook_if_needed` 内发送附加消息。
Goal 中只在正式 Finalization 放行后追加，不因追加失败重开 Goal 或重试 Finalization。
沿用现有 Stop 候选资格：空正文、计划交互边界、工具终止、Goal 固定 fallback、
hooks 关闭等跳过 Stop 的路径不启动追加阶段。
首期只有 Console 通道启动追加阶段；其他通道直接跳过，不执行会产生外部副作用的追加处理器。

## 配置与返回协议

新增 Handler 字段 `outputAppend`，默认 `false`，三种 Handler 类型共用。
仅允许配置在 Stop，且不能和同一 Handler 的 `outputTransform: true` 或 `once: true` 同用。
不同 Handler 可以分别执行转换、校验、追加；新增阶段必须从普通校验计划中排除追加 Handler，
避免提前执行或把失败合并为完成否决。

```json
{
  "enabled": true,
  "events": {
    "Stop": [{
      "id": "completion-supplement",
      "hooks": [{
        "id": "report-link",
        "type": "command",
        "argv": ["python", "scripts/append_report_link.py"],
        "outputAppend": true,
        "timeout": 10
      }]
    }]
  }
}
```

三种类型统一返回以下 JSON 对象；command 输出到 stdout 并退出 0，HTTP 返回 2xx JSON，
prompt 返回同一结构化对象。`reason` 为非空诊断说明，最多 2,000 字符，不直接显示给用户。

```json
{
  "decision": "allow",
  "reason": "report is available",
  "hookSpecificOutput": {
    "appendText": "报告已生成：[下载报告](https://example.com/report)"
  }
}
```

- 只接受 `decision: allow`；`block/deny/stop` 均是无效输出，不具有完成否决权。
- `appendText` 可省略，表示成功但无需追加；空对象 `hookSpecificOutput` 也表示不追加。
- 提供 `appendText` 时必须是含非空白内容的字符串，原文保留，不做 trim 或截断。
- 顶层仅接受 `decision`、`reason`、`hookSpecificOutput`；嵌套仅接受 `appendText`。
  拒绝 replacementText、权限/上下文字段、角色、任意 metadata 等其他效果。
- 未声明 `outputAppend` 的 Handler 不获得追加能力；原有输出校验不放宽。
- command 非 0、HTTP 非 2xx、解析失败和任何非法字段均记为追加失败并跳过。
- 追加阶段的有效失败策略固定为 allow。为兼容已有通用配置结构，`failPolicy` 字段可保留，
  但在追加模式下不影响行为；UI 和人工测试明确显示有效策略，不能显示成可阻断正文。

输入沿用 HookContext：`hook_event_name` 仍是 Stop，`assistant_response` 是已放行的最终正文。
追加计划使用启用来源、matcher、overlay 和 `if` 的现有规则；`if` 针对该最终正文求值。
阶段开始时刷新已激活 Skill 配置一次，冻结来源配置、overlay、匹配结果与上下文，执行中不再重选。
各处理器读取同一份上下文；请求了 conversation snapshot 的处理器使用同一追加前快照，
仍遵守各自的快照上限与现有脱敏规则，不看到本轮其他追加结果。

## 顺序与资源边界

固定顺序为：租户 → Agent Profile → 已激活 Skill；Skill 按 `skill_name` 排序，
每个来源内部按 matcher group 和 Handler 声明顺序。保留来源限定的身份，
不能因不同来源恰好使用相同 handler ID 就错误合并。

并发执行后按此顺序处理结果，跳过失败/no-op/超限项；不按完成时间抢占累计预算。
若某结果超过剩余预算，则跳过该项，后续较小结果仍可使用剩余预算。
只有有效正文计入 64 KiB 配额；无合格结果时不创建空气泡。

继续使用现有 Handler `timeout`（默认 10 秒）；新增 Agent Profile 的
`running.hook_runtime.max_stop_append_seconds`，默认 30 秒，正有限数。
租户和 Skill 不能提高阶段总预算。总预算约束生成/收集阶段，不替代现有会话持久化超时。
总预算耗尽时取消并回收未完成任务，收集截止前完成的成功结果，之后正常有序交付。
使用带超时的并发收集而非让一个任务异常取消全部结果。

正文上限为固定常量：16,384 字节/条、65,536 字节/轮，按 UTF-8 编码后的实际字节数计算。
追加模式还需在读取时限制原始 command stdout、HTTP 解码响应和 prompt 响应各 256 KiB，
command stderr 最多 64 KiB，避免先无限读取再验证短正文；原始响应超限视为失败。
这些读取限制仅用于本次新增追加模式，不借此改变现有校验器或转换器的返回兼容性。

command 超时、超限或取消时清理并 wait 子进程；在部署平台支持的范围内清理受控进程组，
不承诺撤销已经发生的外部操作或终止已脱离控制的进程。
HTTP/prompt 取消本地请求与消费任务，不承诺远端撤销已接受的操作或免计费。

## 消息身份、持久化与恢复

附加消息固定为 runtime 创建的 `role: assistant` 消息，正文是 `appendText`。
Handler 不能自行选择 system/user 角色，不能指定受信的消息身份和来源字段。
每条消息创建一次稳定 Msg ID，并附带运行时控制的 `metadata.stop_append` 对象，
包含 `message_id`（与 Msg.id 相同）、原 Answer Turn 身份、来源/Handler 身份和 append 顺序。
现有 runtime adapter 会另建自己的 Message.id，不能假设它自动等于 Msg.id。
实时消息聚合仍用 runtime Message.id 与 content.msg_id 保持事件关联；
在独立气泡分组/upsert 边界统一使用 `stop_append.message_id` 作为逻辑 ID，
历史侧使用相同标记/Msg.id，因此刷新与 SSE 回放能对应同一消息，不修改通用事件 ID 语义。
runtime adapter 已可透传 Msg.metadata；历史转换目前可能把它嵌入外层 metadata.metadata。
新增共用的标记读取/规范化 helper，兼容这两种既有包装位置，不在各个渲染分支散写判断。
metadata 用于 UI、恢复和诊断；不把其中的执行信息
自动串入模型正文，也不把附加内容提升为系统指令。

附加消息真实进入 Agent memory；后续模型读取相同正文，遵守普通消息的上下文预算、
压缩与 Context Epoch 规则，不保证永远不被压缩。保留原正文，不把附加消息改写为
原正文后缀或覆盖原候选。历史/分享沿用同一 Answer Turn 的归属，不能多创建一个用户问答轮次。

每条消息交付采用现有 session execution 事务：

1. 在提交边界内检查停止是否已接受，校验本轮/来源身份尚未提交。
2. 将消息加入 Agent memory，生成完整会话 state，保留其他模块和 turn ledger 元数据。
3. 通过现有提交入口持久化；不要自行写 session JSON 或另建追加消息数据库。
4. 提交成功后发送包含相同逻辑消息 ID 的 runtime 消息；成功提交时即为已交付，
   断线后的历史/回放可重新显示它。

提交与停止竞争的边界在现有 Answer Turn coordinator 中新增窄的单消息提交仲裁入口，
验证当前 turn 身份并在同一仲裁边界内完成停止检查与持久化回调。
runner 不访问 coordinator 私有锁，单次 `status()` 检查不构成原子提交保证。
入口只包装短暂的本地持久化，不持锁运行脚本或模型，不重入终态 settlement，
复用已持有的 session execution 而不重新获取会话锁。
停止先赢则不提交新消息，
消息提交先赢则保留该消息。提交期间的取消必须等待该次原子写入结果，不能把已落盘消息
当作未提交而从后续 state 覆盖删除。每条成功提交更新当前执行 state，最终 cleanup 必须保留它。

持久化失败属于存储基础设施错误，不等同于 Handler 的 best-effort 失败：
不得发送未保存消息，撤销本地尚未提交的 memory 变更，停止追加剩余结果，
沿用现有持久化失败/settlement 恢复路径，不为此调用模型或脚本重试。
不能声称在存储不可用时仍成功完成持久交付；正文已放行的事实与存储失败分别记录。

重连依赖现有活动流回放或终态历史，不启动新的追加阶段。前端按稳定 Msg ID upsert，
实时重放与历史切换不能产生重复气泡（以受控逻辑消息 ID 为准）。进程重启沿用既有中断轮次恢复规则，
只恢复已保存消息，不恢复执行未完成的追加任务；不新增 durable job 或 exactly-once 脚本执行保证。
同一请求的通用 query retry 也不能重跑收尾：进入已放行后的追加阶段即设置请求级收尾标记，
此后基础设施错误不能再被通用 TimeoutError/连接错误分类器送回模型或追加处理器。
追加持久化错误须通过不可重试的收尾错误路径进入 settlement/持久化恢复，
保留已开始状态与已提交消息；恢复只能重试状态写入，不重新执行模型或 Handler。
外部脚本副作用与本地落盘之间的崩溃窗口属于已接受的尽力交付限制。

## 停止、界面与诊断

模型结束后的追加阶段须主动检测 STOPPING：阶段入口、任务启动前、任务返回后和每条持久化前。
停止时取消并回收剩余任务，丢弃未持久化结果；已提交消息保留。
没有显式停止的 SSE 断线不取消阶段。

Console 为带追加标记的消息生成独立 Markdown 气泡，实时 SSE 聚合和历史转换共用相同分组规则。
不复用 cron 标记，不仅靠新 Msg ID 推断独立气泡，不将后续追加内容重新合并到正文气泡。
消息仍属于原用户问题；分享原 Answer Turn 时包含其已持久化附加消息。
复制继续使用现有文本操作；不为附加气泡新增独立模型重试或新问答入口。
阶段在原请求完成前结束；沿用现有请求运行/停止状态，不增加逐处理器进度气泡。

Hook 管理页为 Stop 的三种类型显示“追加独立消息”模式，和同一 Handler 的输出转换互斥；
开启时清除并禁用 once。显示固定有效失败策略、长度预算和返回示例。
原有配置分发、脚本扫描/路径边界、权限和租户隔离沿用既有实现。
保存接口拒绝非法阶段组合，不悄悄降级成普通校验器。

人工测试执行一个真实追加 Handler，要求非空示例 `assistant_response`，
显示是否追加、UTF-8 字节数、failed/failure_type、有效失败策略和时长；
不写入真实 Chat、不添加 Agent memory，不投递气泡，不把追加正文回显到诊断摘要。
单处理器测试不能证明跨来源顺序、累计上限、并发预算和恢复行为，后者由自动化测试验证。

日志记录 turn/trace、来源、Handler、消息 ID、顺序、耗时、字节数、哈希及失败类别；
不记录候选正文、附加正文、原始 stdout/stderr、凭据或含正文的异常字符串。
聊天中不插入失败气泡；日志记录失败本身也不能阻断已放行正文。

## 实现落点

| 范围 | 最小必要变化 |
| --- | --- |
| hook_runtime/models.py、output.py、executor.py | 追加声明与共享返回协议、固定有效失败策略、有界读取和取消清理 |
| hook_runtime/resolver.py、runtime.py | 隔离三种阶段、冻结有序追加计划、并发收集及预算 |
| runner/turn_lifecycle.py、runner.py | 普通/Goal 最终放行出口复用追加 helper；停止和逐条提交 |
| runner/query_attempt.py、重试分类边界 | 追加开始后只恢复持久化，禁止通用 query retry 重跑模型和 Handler |
| app/answer_turn/coordinator.py | 单消息提交与停止的窄仲裁入口，沿用当前 turn 的终态与持久化边界 |
| runner/session.py、历史/turn ledger 适配 | 复用事务并验证 memory/state 原子一致性，不另建消息旁路 |
| Console 实时聚合、sessionApi 历史转换 | 追加标记分组、稳定 ID 去重、同轮归属 |
| Hook 管理页与人工测试服务 | 三类型模式编辑、冲突提示、追加摘要 |
| wiki/hook 与 analysis/playbook | 新增脚本样例，说明流式、失败、上限与恢复排查入口 |

实施前按仓库约束对实际编辑的每个符号做 GitNexus impact；
本设计调查不等同于已完成实现影响分析，不改动无关代码。

## 验收标准

1. 仅追加配置时，Stop 执行前已能收到正文文本增量；工具/审批等实时事件不受影响。
2. 三种类型共用 allow/appendText 协议；非法事件、once、互斥模式、非法字段和空白文本均有明确失败。
3. 连续 block 再 allow 的普通请求只在最终 allow 后执行一次追加；Goal 重试遵循相同行为。
4. 追加专用 Handler 不在转换/校验阶段提前执行；校验 block、空正文及跳过路径不触发追加。
5. 三来源、多 Skill、乱序完成、相同本地 Handler ID 的结果按来源限定身份和固定顺序展示。
6. 每处理器至多一条，无文本不创建气泡；UTF-8 多字节边界、累计不足后跳过/继续规则准确。
7. 单项失败、超时、原始响应超限、prompt 非法决策不否决正文；总预算耗尽仍能交付截止前成功项。
8. 取消能清理受控子进程和本地 HTTP/prompt 任务；已接受停止后不提交剩余消息。
9. 实时有多条独立气泡，刷新/重连顺序和 ID 一致；重复回放无重复，原正文气泡不变化。
10. 下一轮实际模型输入含已提交附加正文，不含 reason/执行日志，不改变消息的权限角色。
11. 先正文后附加，先提交后发送；覆盖提交前/中/后停止、持久化失败、
    提交后发送前断线/崩溃；追加开始后的 TimeoutError/连接错误不得触发通用 query retry 重跑脚本或模型。
12. session schema v2/v3、turn 查询/分享/上下文压缩均保留既有契约和附加消息同轮归属。
13. 管理页三类型配置、分发和单处理器人工测试正确；诊断不泄露正文；非 Console 不执行追加处理器。
14. 原有普通 Stop、输出转换、有限续跑、Goal 正式交付、停止及恢复回归通过。

## 方案取舍与非目标

采用放行后独立阶段，而非普通校验器附带返回内容：避免 block 尝试仍生成附加内容，
也避免 prompt 默认 failPolicy 重新否决已经展示的正文。
采用真实 assistant 消息进入 memory，而非仅展示旁路：保证下一轮模型与用户所见一致。
采用先保存再发送，而非乐观显示后保存：用“保存即交付”的明确边界换取刷新与停止竞态的一致性。
以上取舍记录于 [ADR 0037](../../adr/0037-stop-appends-run-after-approval.md)。

首期不做交互卡片、附件协议、追加正文流式生成、追加内容二次 Stop 审批、
会话 once、其他 Channel 投递、跨进程追加任务续跑或任意外部副作用的 exactly-once 保证。
独立追加不替代输出转换；需要改写/脱敏原正文时仍使用原有转换机制。
