# Stop 附加消息排查

适用于“正文已显示但没有附加气泡”“刷新后气泡重复/消失”和人工测试诊断。
协议与资源限制见 [Hook runtime](../../wiki/hook/hook-runtime.md#stop-附加消息)。

## 先确认资格与配置

1. 首期必须是 Console。其他 Channel 直接跳过追加 handler，不执行脚本。
2. 检查最终 Stop 是否 allow：普通问答最终放行、Goal 正式 Finalization 放行才追加；
   block 尝试、空正文、计划边界、工具终止和固定 fallback 等跳过 Stop 的路径不追加。
3. 检查 handler 为 `outputAppend: true`，事件是 Stop，未同时设置 `outputTransform: true`
   或 `once: true`。检查启用来源、matcher、overlay、针对最终正文求值的 `if`，
   以及 Skill 是否在当前会话激活。阶段入口冻结计划，运行中修改配置不会重选。

## 用人工测试区分协议与交付

Hook 管理页人工测试需要非空 `context.assistant_response`，真实执行一个 handler；
脚本副作用仍然发生，但不会写真实 Chat、memory 或保存草稿配置。
摘要中的 `output_append` 表示配置模式，`append_applied` 表示结果具有有效文本，
`append_bytes` 使用 UTF-8 实际字节数；`failed`、`failure_type`、`duration_ms` 保留诊断。
追加的 `effective_fail_policy` 固定为 allow，与存储的 failPolicy 无关。
成功但没有 appendText 是 no-op，不创建空气泡。
摘要不回显 appendText、reason、stdout/stderr 或 HTTP body。

单 handler 默认 10 秒，阶段默认 30 秒，可由 Agent Profile 的
`running.hook_runtime.max_stop_append_seconds` 调整阶段预算。单条 16,384 字节、
每轮 65,536 字节，超限跳过、不截断。原始响应 256 KiB、command stderr 64 KiB。
多项并发完成后按“租户 → Agent Profile → Skill-name → 配置声明顺序”应用累计预算并交付；
人工测试不能证明多来源顺序、累计配额、并发预算或历史恢复。

## 持久化与停止

- 追加消息先进入真实 memory 并持久化，再发送。后续模型输入应能看到其正文，
  但普通上下文预算、压缩和 Context Epoch 仍然生效。
- 用户停止保留成功提交消息，丢弃未提交结果；不回滚外部脚本副作用。
  只断开 SSE 不表示停止，后台继续。
- 重连恢复已有消息，不重跑 handler；崩溃后未提交结果允许丢失，不补跑脚本。
- 存储失败停止后续交付，不发送未保存消息；进入会话持久化恢复，不重新调用模型或脚本。
  不要把它当成普通 handler 的 best-effort 失败。
- 重复或合并气泡时检查 `metadata.stop_append.message_id`：实时 runtime 包装和历史包装
  可能不同，但规范化后的逻辑 ID 应一致；消息属于原 Answer Turn，不另建用户问答。

## 代码与诊断入口

| 范围 | 入口 |
| --- | --- |
| 配置与严格协议 | `src/swe/agents/hook_runtime/models.py`、`output.py` |
| 有界执行、取消清理与安全失败码 | `src/swe/agents/hook_runtime/append.py` |
| 计划冻结、并发与有序预算 | `src/swe/agents/hook_runtime/runtime.py::emit_stop_append` |
| 放行后交付和提交 | `src/swe/app/runner/stop_append.py`、`answer_turn/coordinator.py` |
| 禁止收尾重试 | `src/swe/app/runner/query_attempt.py`、`runner.py` |
| 管理人工测试 | `src/swe/app/hook_management.py::manual_test` |
| Console 标记读取与分组 | `console/src/pages/Chat/stopAppend.ts`、`sessionApi/index.ts` |

按 turn/trace、source、handler、message_id 关联 `Stop append` 运行日志和
`agent_hook.audit` 人工测试日志。只使用失败类别、耗时、字节数和哈希排查；
不要为排查开启候选正文、附加正文、原始 stdout/stderr 或凭据日志。
handler 失败不会插聊天错误气泡、否决正文或触发 Stop 递归/模型续跑。

最小回归范围：`test_stop_append.py`、`test_runner_stop_append.py`、
`test_hook_management.py`（服务与路由）及 Console 的 `stopAppend.test.ts`。
