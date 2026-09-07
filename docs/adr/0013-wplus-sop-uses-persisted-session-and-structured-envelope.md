# W+ SOP 使用持久化会话、专用工作台和结构化交互信封

## 决策

平台为 `wplus-sop-miner` 提供绑定所属 Chat 的专用工作台，路由为 `/wplus-sop/:sessionId`。工作台不是第二套 Chat，也不是独立应用；它是同一 Chat、同一 Agent 工作流的结构化交互视图。

后端持久化的 `W+ SOP Clarification Session` 是工作流唯一事实来源。版本化的 `Structured Interaction Envelope` 在 Agent、后端状态机、Chat 投影和工作台之间传递环节队列、题组、回答、预跑、反馈、环节确认、修订、结果和状态变化。前端不得从 Markdown、JSON 代码块、React 内存或 Chat 消息反推按钮、当前环节或恢复状态。

底层信封保持技能中性，以复用类型校验、幂等、状态版本和审计机制；第一版产品入口、路由、页面和业务状态机仍只服务 `wplus-sop-miner`，不同时建设通用交互式技能平台。

## 页面与 Chat 的责任边界

- 用户通过 Chat 技能选择器选择 `wplus-sop-miner`，或在消息正文手动输入独立、精确的 `@wplus-sop-miner` 提及时，Chat 使用原始消息渲染“进入 W+ SOP 工作台”卡片；用户确认后创建 SOP 会话并跳转，用户不需要重复输入需求。
- 普通 SOP 文本、裸技能名、近似名称和模糊语义不触发新的 W+ SOP 入口；未显式调用 Miner 的请求继续由普通 Chat 处理。已有活动或暂停会话的返回、恢复入口不受影响。
- 用户点击进入后，后端必须先完成会话持久化，再返回 `session_id` 和 `/wplus-sop/:sessionId`。页面不得先跳转再依赖前端补建会话。
- Chat 中的入口卡在会话创建后变为只读状态卡，显示当前环节、运行状态和“继续工作台”按钮；暂停后也通过该卡恢复，不重新创建会话。
- 工作台是题组回答、预跑启动与重试、预跑反馈、环节确认、历史修订、记忆授权和结束操作的唯一写入口。
- 工作台提交继续触发所属 Chat 的同一 Agent 回合，不创建第二条对话历史。
- Chat 只保存不可变审计投影、会话控制卡和恢复入口；Chat 中的问题卡和答案卡只读，不提供第二套提交控件。
- 活动会话锁定所属 Chat 的普通输入。只有明确保存退出、完成或彻底结束后才恢复普通输入；普通导航离开工作台不会自动暂停。
- 一个 Chat 同时最多存在一个活动或暂停的 W+ SOP 会话。完成或彻底结束后可以创建新会话，同时保留历史记录。

## 首轮与逐环节状态机

Miner 的第一次响应只能生成 2–4 个业务环节的候选队列。这个上限只约束自动候选；用户在确认页可以继续手工增加环节，人工确认的最终队列不设上限，但始终至少包含 2 个环节。用户确认或调整队列之前，不得生成第一个澄清问题。

平台状态机必须显式表达以下主路径：

```text
GeneratingStageProposal
→ AwaitingQueueConfirmation
→ GeneratingQuestions
→ AwaitingAnswer
→ GeneratingTrial
→ ExecutingTrial
→ AwaitingTrialFeedback
→ AwaitingStageConfirmation
→ GeneratingQuestions（下一环节）
→ FinalizingOutputs
→ MemoryReview
→ Completed
```

每个环节内部仍遵守 Miner 的业务状态：

```text
clarifying
→ ready_for_trial
→ trial_running
→ feedback_review
→ awaiting_stage_confirmation
→ confirmed
```

当前环节没有到达 `confirmed` 时不得进入下一环节。反馈改变流程时返回当前环节重新预跑；环节确认与下一环节激活必须作为一次原子状态转换。生成期间请求退出时进入 `PendingExit`，等当前完整响应持久化后再转为 `Paused` 或 `Terminated`；该状态只接受“继续等待”或“取消本轮并暂停”，不得用第二次 `save_and_exit` 覆盖原恢复态。终止性失败进入 `RecoverableFailure`，重试不得重复保存答案或触发第二个 Agent 回合。

`ExecutingTrial` 由工作台已经启动的同一个后台 Agent 回合负责。该回合先持久化 `trial_plan`，再直接执行 Miner references 中已确认的 OpenCLI 命令，并依次回写 started/progress/completed/failed 事件；平台不会在 `trial_plan` 后自动创建第二个执行任务。工作台只发起带状态版本和幂等键的运行请求、订阅进度并展示结果，不在浏览器中执行 OpenCLI。业务执行不得改用其他 Agent 工具；结构化事件工具只承担工作流状态回写。只读能力可以在当前授权范围内自动运行；权限拒绝、超时或执行失败必须回写 `trial_execution_failed`，不得因为属于 SOP 预跑而绕过权限确认或伪造结果。

## Structured Interaction Envelope

公共信封至少包含：

```json
{
  "object": "structured_interaction",
  "protocol_version": 1,
  "interaction": "wplus_sop",
  "event_id": "evt_...",
  "session_id": "sop_...",
  "chat_id": "chat_...",
  "revision": 1,
  "round": 1,
  "state_version": 3,
  "kind": "question_batch",
  "payload": {}
}
```

第一版事件至少覆盖：

- `stage_proposal`
- `stage_queue_confirmed`
- `lifecycle_progress`
- `question_batch`
- `answer_accepted`
- `trial_plan`
- `trial_execution_started`
- `trial_execution_progress`
- `trial_execution_completed`
- `trial_execution_failed`
- `trial_feedback_accepted`
- `stage_confirmation_required`
- `stage_confirmed`
- `revision_applied`
- `sop_result`
- `memory_candidates`
- `session_state_changed`
- `recoverable_failure`
- `termination_summary`

每个 `question_batch` 包含 1–3 题，并在完整题组通过 schema 校验后一次性开放。题目必须有稳定的 `question_id`，选项必须有稳定的 `option_id`；支持单选、多选和自由文本。需要用户补充自定义内容的选项必须设置 `requires_custom_input: true`；选中后工作台就地显示文本输入，回答同时提交稳定选项 ID 与自定义文本，不能把文本拼接进选项 ID。回答提交携带 `expected_state_version` 和幂等 `request_id`，后端原子保存整轮回答并只启动一次 Miner 回合。

## 能力证据与嵌套出参

工作台在预跑或证据面板中展示 Miner 实际引用的能力契约。用户提交完整题组后，已经启动的后台 Agent 回合按照冻结的输入快照执行当前环节的完整预跑，只运行 references 中已确认且已授权的 OpenCLI 命令，并通过结构化事件流回传步骤级进度、脱敏结果摘要、schema 校验和失败位置。该回合必须以一个且仅一个 `trial_execution_completed` 或 `trial_execution_failed` 成功持久化后才能结束；用户不需要复制命令或切换到外部环境。

每次预跑必须生成稳定的 `run_id`，记录能力版本、输入快照、开始与结束时间、步骤结果、授权决策和最终状态。原始客户响应只能存在于受控运行环境；工作台和 Chat 投影仅接收脱敏结果、计数、结构校验、警告和可审计的执行摘要。

当能力出参包含对象、对象列表或多层列表时，信封保留能力目录中的递归结构，不得把子字段平铺成无父级的字段清单：

```json
{
  "name": "items",
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "name": { "type": "string" },
      "children": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {}
        }
      }
    }
  }
}
```

界面将这类契约渲染为可展开的字段树，并显示能力的 `verification_status` 与 `output_contract_status`。只展示字段定义和脱敏示例结构，不展示客户姓名、客户标识、账号、卡号、余额、交易明细或原始响应。

## 工作台交互基线

- 页面持续显示环节队列、当前环节、已确认环节和未开始环节；当前主任务始终只有一个。
- 中央工作区承载当前题组、预跑进度、脱敏结果、反馈输入或最终结果，底部主操作与当前状态一致。
- 预跑开始后展示正在执行的步骤、当前能力、运行 ID 和耗时；完成后固定展示反馈输入框。用户可以确认结果符合预期，也可以填写卡点、字段缺失或结果偏差并提交，提交反馈后由系统重新执行受影响步骤。
- 生成期间的调试悬浮层只展示所属 Chat 中普通 assistant message 的文本内容，并以纯文本方式呈现；reasoning、工具调用、工具参数、工具输出和非文本内容均不进入该视图。追踪内容保持有界、仅存在于进程内，并在运行结束后清理。
- 预跑执行中禁用重复启动；刷新或 SSE 重连后根据持久化 `run_id` 恢复同一次运行，不得启动第二次预跑。
- 能力证据、已确认事实、明确未知项和历史修订属于上下文信息，不能与当前主操作争夺视觉焦点。
- 完整题组未通过 schema 校验、回答未填写完整或状态版本已经过期时，提交按钮不可用。
- `AwaitingAnswer` 只表示题组已经持久化并可编辑，不等同于所属 Chat 的上一轮 Agent 已完成资源清理。会话 HTTP 快照必须附带非持久化的 `runtime_status`，SSE 在同一 `state_version` 下独立推送该状态的变化；只有 `runtime_ready: true` 时前端才开放回答提交。清理期间用户仍可填写和修改回答，草稿不得因等待或 409 冲突而丢失。
- 保存退出、彻底结束和历史修订必须解释影响范围；保存退出成功后返回准确的所属 Chat，Chat 通过 active-session 恢复“继续工作”入口，不依赖消息元数据刚好完成刷新；彻底结束生成只读摘要并醒目标记“不是有效 SOP”。
- 刷新、重复点击、双标签页和 SSE 重连不得产生重复回答、重复 Chat 审计消息或重复 Agent 回合。
- 窄屏下环节队列变为横向进度区，证据与历史进入抽屉；主操作保持至少 44px 点击高度并提供明确焦点状态。

## 最终结果与记忆

所有环节完成澄清、预跑、反馈和确认后，Miner 才能生成结果。最后一次累计刷新与最终化是两个串行的所属 Chat Agent 回合：累计刷新 run 必须先提交 `cumulative_refreshed` 并完整结束，服务端原子结算该 run 后才认领和启动新的 `FinalizingOutputs` run；旧 run 不得越过完成边界提交 `sop_result`。最终化回合生成 `sop_spec.json`，运行 Miner 校验与渲染脚本，使用模板生成 `example_result.html`，再逐一调用 `copy_file_to_static` 交付 `sop_spec.json`、`sop_render.md`、`sop_render.html` 和 `example_result.html`。`sop_result` 必须携带工具返回的真实 static URL、文件名和哈希；服务端只校验四个文件均位于所属 Workspace static、实际存在且哈希一致后才持久化。服务端不按 ownership 重建或限制 static URL，也不比较文件内容与事件顶层 `sop_spec` / `readable_sop` / `html` / `example_result_html` 是否一致。工作台以 `artifacts` 中 `sop_render_md` 和 `sop_render_html` 的 `static_url` 为预览源，顶层字段仅作内联兼容；当两者不一致时以 artifact URL 为准。下载接口只重定向到已持久化的真实 static URL，不在请求时拼接文件。模板缺失属于可恢复阻塞，不得省略第四个文件或伪造成功。

最终文件生成不立即完成会话。`sop_result` 与 `memory_candidates` 持久化后，工作台先进入 `OutputReview`，立即展示 Markdown/HTML 预览和四个真实文件；用户明确确认结果内容后，有候选时进入 `MemoryReview`，无候选时进入 `Completed`。每个记忆候选必须展示类型、完整脱敏内容、准确对话证据和真实 JSONL 目标。用户必须为全部未决候选逐项选择保存或不保存后统一提交；缺项、重复项、未知候选或附带目标篡改都会被原子拒绝。全部拒绝时直接完成且不启动 Agent；至少一项获批时，服务端先等待所属 Chat 的上一轮 Agent 完全结束，再把全部获批候选及目标绑定到同一个 `WritingMemory` Agent 回合。Agent 只能逐项调用当前 Miner 的 `scripts/memory_store.py ... --approved`，处理完后只提交一次 `memory_write_batch_result`。平台不在命令请求线程执行写入，只校验批次候选集合、候选身份、目标和逐项结构化回执。三类目标分别是 `memory/common-wplus-knowledge.jsonl`、`memory/users/{user_scope}/wplus-usage-preferences.jsonl` 和 `memory/cases/sop-cases.jsonl`；不得写 Agent 根目录 `MEMORY.md`。`user_scope` 只能来自调用方结构化提供的匿名范围；缺失时跳过个性化候选。成功项以 appended 或 duplicate 标记 approved；失败项回到 `MemoryReview` 并允许重新批量授权，不影响同批其他成功项。旧单候选 active 字段和 `memory_write_completed` / `memory_write_failed` 只保留持久化历史读取与在途恢复兼容，新命令不再产生。旧版只读候选允许缺少类型、证据和目标，仅能在已完成会话中作为历史状态展示，永不提供操作入口，也不得将“历史已批准”表述为已经写入；没有结构化回执时必须明确标记为不可验证。第一版只提示结果可以交给 `wplus-skill-builder`，不自动调用 Builder。

## 持久化与一致性

后端至少持久化会话归属、技能版本或内容快照标识、状态版本、修订号、轮次、环节队列、当前有效题组与回答、失效历史、预跑运行 ID、输入快照、步骤进度、脱敏结果摘要、用户反馈、结果确认记录、调用方匿名 user scope、授权记录、命令 receipt、运行 attempt 谱系、幂等请求、待执行退出动作、持久化 Chat 投影 outbox、最终结果、记忆授权状态和 W+ memory store 写入回执。

所有读写、SSE 订阅、结果下载和 active-session lookup 都校验租户、来源、用户、Agent 与所属 Chat；缺失身份或归属不匹配时 fail closed，并以 404 避免泄露会话是否存在。所有写操作还校验当前状态、预期状态版本和幂等请求。每个用户动作使用稳定的 `command_request_id`；每次真实执行创建新的 `run_id`/`attempt_id`，失败重试或反馈重跑通过 `retry_of_run_id`/`rerun_of_run_id` 关联旧运行。

W+ 事件日志是唯一提交点；同一提交持久化 Chat 投影 outbox，随后确定性补写 Chat，并按投影事件 ID 去重。这样即使 Chat 写失败或进程在两次写之间重启，也能对账补齐而不产生半提交或重复卡片。SSE 只承担实时提示，持久化 Session 投影才是恢复真源；重复或旧版本事件被忽略，版本缺口触发重新读取投影。V1 的本地 JSON 写路径只支持单进程桌面部署，多 worker 部署必须使用支持跨进程事务与锁的数据库 store。

所属 Chat 的任务占用状态不写入 W+ 事件日志，也不递增 `state_version`。服务端从运行时任务追踪器投影 `ready`、`finalizing`、`running` 或 `stopping`，并在无可用追踪器时仅为兼容旧运行环境按 `ready` 处理。任何会启动新 Agent 回合的命令在持久化业务转换之前仍必须等待所属 Chat 释放；等待超时返回带 `code: owning_chat_finalizing` 和 `retry_after_ms` 的机器可读 409，且不得消费幂等键、保存回答或推进状态。前端门禁只是提前反馈，不能替代这道服务端并发防线。

历史答案修订采用追加式审计：旧记录保留并标记失效，修订点之后的题组、回答、派生结果和未执行记忆选择全部失效。

## 后果与被否决方案

这一选择增加了持久化模型、状态转换、结构化 Agent 输出、前后端协议和测试成本，但换取刷新恢复、暂停恢复、答案修订、单一写入口、双标签页并发控制和不可变审计。

被否决的方案包括：

- 从 Markdown 或 JSON 代码块解析交互卡片；
- 只在前端保存工作台状态；
- 让 Chat 和工作台同时提交答案；
- 创建不绑定原 Chat 的第二套对话；
- 第一版扩张为所有技能共用的通用工作台；
- 把对象列表出参平铺为失去父子关系的字段清单。
