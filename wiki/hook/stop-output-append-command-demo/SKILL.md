---
name: stop-output-append-command-demo
description: "Use this skill to collect Markdown reference links from an approved final response into one independent Stop appended message."
license: Proprietary. LICENSE.txt has complete terms
metadata:
  builtin_skill_version: "1.0"
  swe:
    uses_tools:
      - execute_shell_command
---

# Stop Output Append Command Demo

这个样例演示 skill 级 `Stop + command + outputAppend: true`：读取已放行正文中
实际出现的 Markdown HTTP/HTTPS 链接，按首次出现顺序去重，生成一条独立的参考链接消息。
脚本不访问链接、不生成虚构报告或数据；没有链接时返回成功但不追加消息。
它只识别简单的 `[标题](https://...)` 链接，不是完整 Markdown 解析器。

## 执行与协议

正文继续流式输出。普通 Stop 校验全部放行后，脚本才从 stdin 接收 HookContext；
`assistant_response` 是最终正文。stdout 只输出严格 JSON，退出码为 0：

```json
{"decision":"allow","reason":"reference links collected","hookSpecificOutput":{"appendText":"### 回复中的参考链接\n\n1. [参考链接 1](https://example.org/spec)"}}
```

示例域名仅用于本地输入，脚本不会自行添加该链接。无链接时省略 `hookSpecificOutput`。
command、HTTP、prompt 共用 `allow` / `reason` / `appendText` 协议；reason 仅供诊断。
每个 handler 最多一条 Markdown 消息，进入真实 Agent memory、会话历史和后续模型上下文。
首期仅 Console 执行追加阶段；其他 Channel 不执行这些 handler。

## 配置与边界

- `outputAppend` 只能用于 Stop，不能与同一 handler 的 `outputTransform: true`
  或 `once: true` 同用。附加内容不会再次触发 Stop，也不会触发模型续跑。
- 多个追加 handler 并发读取同一正文与追加前快照，按“租户 → Agent Profile →
  Skill（skill_name 排序）→ 各来源配置声明顺序”交付独立消息。
- 单 handler 默认 10 秒；阶段默认 30 秒，由 Agent Profile 的
  `running.hook_runtime.max_stop_append_seconds` 配置。单条最多 16,384 UTF-8 字节，
  每轮累计最多 65,536 字节，超限跳过、不截断。
- 原始 stdout / HTTP 解码响应 / prompt 响应最多 256 KiB，command stderr 最多 64 KiB。
- 失败尽力跳过，有效失败策略固定为 allow，即使保存的 failPolicy 是 block；
  聊天不出现错误气泡，正文成功状态不被否决。
- 每条消息先持久化再发送。用户停止保留已提交消息，丢弃未提交结果；外部副作用不回滚。
  单纯断线后台继续；重连恢复已保存消息，进程崩溃后不重跑脚本。

## 本地调试

在此 demo 根目录运行：

```bash
printf '%s' '{"hook_event_name":"Stop","assistant_response":"参考 [规范](https://example.org/spec)。"}' | python scripts/append_reference_links.py
```

预期输出包含上面的 `appendText`。将正文换成没有 Markdown 链接的文本，应只返回 allow 和 reason。
在 Hook 管理页人工测试时提供非空 `assistant_response`：会实际执行一个 handler，
只显示是否追加、UTF-8 字节数、有效失败策略、状态和耗时，不回显追加正文，
也不写入真实 Chat、Agent memory 或保存草稿配置。脚本本身的外部副作用仍会发生。

目录包含 `hooks/hooks.json`、`scripts/append_reference_links.py` 和当前 `SKILL.md`。
完整语义见 [hook-runtime.md](../hook-runtime.md#stop-附加消息)。
