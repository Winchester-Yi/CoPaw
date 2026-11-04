# 运营看板 MySQL/NAS 会话历史读取修复设计

日期：2026-11-04。状态：代码修复已实施；专用 MySQL 和真实页面验收待完成。
工作树：`.worktrees/conversation-mysql-nas`；核对基线：`099ecaeb847dd174a7f2e5fce0739775745dcdb6`。

## 1. 目标与范围

修复运营看板在 MySQL authority 下缺失归档入口、准入问题、失败消息和 Chat metadata 的问题。采用现有聊天历史装配能力，继续读取目标用户 workspace。

完成后的接口契约是：详情返回当前可见消息、目标 ChatSpec、当前状态及归档可用性/边界；较早归档正文单独分页读取。详情不一次返回全部归档。

本次只修改 Tracing 对象校验与 SQL 路由分派，并增加测试和排查说明。不改会话表结构、迁移工具、NAS bundle 格式、publication/CAS、checkpoint、compact、GC、列表接口或 snapshot。当前初次 get_chat 与历史读取的重复 metadata hydrate 留待独立性能优化。

前提沿用当前系统边界：操作员访问目标用户的入口权限由现有网关/应用访问策略提供。本次不建立角色权限系统；路由内的校验负责目标 Chat 的对象归属与 source/agent 一致性，不能把它当作完整的操作员授权机制。

## 2. 问题依据

当前 SQL Tracing 详情调用 indexed.tracing_view。HistoryStore.tracing_view 指定 layers=(memory,task)，导致：

- error、anchor 被排除。
- _snapshot 在指定 layers 时不读 boundaries，archive.has_more 恒 false。
- _assemble(tracing=True) 返回 chat=None，并沿用旧的隐藏上下文处理差异。

新 ReadOnlySessionChat 依赖 history.chat.meta/name、archive.boundaries 和 archive.has_more，后者为 false 时不会调用归档分页。

同时，_target_chat 的初次 chat_manager.get_chat 可能已经进行 MySQL/NAS hydrate，却没有经过 _indexed_history_call 的存储错误映射。仅修改详情最后一次读取不能覆盖这一故障入口。

## 3. 方案选择

| 方案 | 评价 |
|---|---|
| 仅补 Tracing boundaries/has_more | 不能同时解决 error/anchor、ChatSpec 和隐藏上下文差异，不采用 |
| 全局修改 HistoryStore.tracing_view 为 current 语义 | 会改变已有内部 API 和直接 store 测试语义，扩大本次范围，不采用 |
| Tracing SQL 路由使用目标 indexed.current；归档使用目标 indexed.archive_page | 推荐。复用已存在的装配与保护，旧内部方法保持原义，路由变更集中 |

不修改 runner/api.py 的通用 _build_chat_history/_archive_page 签名。SQL Tracing 路由显式调用支持 authorize 的 reader；legacy 分支继续使用现有通用 helper。

## 4. 目标用户解析与对象校验

保留 _get_target_workspace 的现有行为：解析目标 user/source、初始化工作区、选择 Agent 并取得目标 workspace；不修改 request.state 的操作员身份，不调用普通 Chat 的 _authorize_chat 冒充操作员的“当前用户”请求。

将 _target_chat 内已有校验提取/组织成一个可复用的 authorize(ChatSpec) 闭包。_target_chat 返回 `(workspace, chat, authorize)`，供两个路由复用。只在 tracing.py 内使用，不引入新的通用授权框架或上下文类。

闭包在 workspace 解析后捕获固定的：目标 user_id、请求 chat_id、source_id，以及显式 X-Agent-Id 或已选择 workspace 的 agent_id。它不在后续调用中重新读取可变请求状态。

校验规则：

1. Chat.id 等于请求 chat_id，Chat.user_id 等于目标 user_id，否则 404。
2. 对 source_id/agent_id，保留当前兼容规则：存在期望值且 Chat.meta 有非空字符串值时必须匹配；缺失的历史 metadata 不新增拒绝条件。
3. metadata 仅作补充检查。SQL reader 持有可信 WorkspaceScope，真实的 service_unit/tenant/source/agent 隔离由 scoped 查询保证，不允许客户端 metadata 改变读取 scope。
4. 不存在或无权访问的目标统一按 404 处理，不返回其他用户 ChatSpec 或正文。

SQL 模式的首次 chat_manager.get_chat 通过现有 _indexed_history_call 包装，以覆盖 metadata hydrate 的存储异常；返回 None 时仍是 404。workspace 解析/初始化错误保持现有语义，不把整个解析过程笼统包装成“存储不可用”。legacy 查询维持现有错误行为。

首次查询执行 authorize，随后同一个闭包传入 SQL current/archive_page。reader hydrate 后和返回前调用闭包；数据库 revision、删除与 locator 的变化仍由已有 _validate_chat_row/_revalidate_rows 检测。

注意：最后一次 authorize 校验的是已复核的 ChatSpec；它不重新查询网关权限。外部权限撤回机制不在本次设计中新增。

## 5. 详情读取

路由：`GET /tracing/chats/{chat_id}?user_id=...`。

数据流：

```text
操作员请求
  → 解析目标 workspace
  → 读取目标 Chat metadata 并校验对象
  → indexed.current(chat_id, authorize=同一个回调)
  → ChatHistory
```

SQL 分支使用 `_indexed_history_call(indexed.current, chat_id, authorize=authorize)`。

复用 current 既有语义：

- memory/task/error 加目标 Chat 适用的 anchor。
- 按现有规则消除已进入 primary 或已归档的重复 anchor，不自行改成全局 source ID 去重。
- 返回目标 ChatSpec、SQL 投影 status、归档可用性及 boundary metadata。
- 执行与 Chat 相同的 hidden-context redaction。
- 审批状态覆盖继续在 HistoryStore 内用目标 tenant/source/user context，退出后恢复操作员 context。
- 保留现有 pins、NAS IO、复核与清理流程；不取得会话执行锁，不触发模型、工具、compact 或迁移。

legacy 分支继续 `_build_chat_history(..., non_blocking=True)`，保持上游合入的当前历史行为。

## 6. 归档分页

路由：`GET /tracing/chats/{chat_id}/history?user_id=...&before=...&limit=...`。

SQL 分支显式调用 `_indexed_history_call(indexed.archive_page, chat.id, before=before, limit=limit, authorize=authorize)`，避免通用 _archive_page helper 没有透传 authorize。
legacy 分支继续调用原 _archive_page。

保留：

- limit 的原范围 1..50；它限制 source occurrences，消息 fragment 数不必等于 source 数。
- before 的现有 scope/chat/epoch/high 签名和校验，不新建 cursor 版本。
- 页内顺序、边界锚点、has_more、next_cursor 和 hidden-context redaction。
- 读取后复核与 pins 释放。

详情 archive.has_more 表示有归档可供读取；page.has_more 表示该分页视图下尚有更早 source。两者不要混为同一个全局实时状态。

详情与首个归档请求是两个独立快照，不承诺跨请求同一事务。首次归档页捕获其 high，后续页沿用当前 cursor 协议。并发 reset/删除/变化按现有错误契约处理，不保证多请求拼接期间历史永不变化。

空归档返回 200、空 messages、has_more=false。详情后发生变化导致首个归档页为空属于合法情况，前端显示耗尽；不能为此伪造消息或无限重试。

## 7. 响应与错误契约

HTTP 路径、参数及 ChatHistory/ChatArchivePage schema 不变。SQL 详情内容按以下有意变化发布：chat 从 None 变为完整目标 ChatSpec；error/anchor 可见；隐藏上下文按聊天页规则过滤；archive 字段反映真实归档。
ChatSpec 类型与现有目标用户列表已返回的类型一致，不增加临时内部字段。旧消费者对新增消息内容需接受，不能继续依赖 chat=None 或“没有归档”的旧缺陷。

| 场景 | 结果 |
|---|---|
| 初次查找无 Chat、user/chat/source/agent 对象检查失败 | 404 |
| reader 查找时 Chat 已不存在（KeyError） | 404 |
| before 无效、跨 scope/chat、epoch 不符 | 保留现有 422 |
| limit 越界、必填 user_id 缺失 | FastAPI 参数校验 422 |
| MySQL/NAS/StorageError、现有 revision ConflictError | 按 _indexed_history_call 现有规则返回 503 |
| workspace manager 未初始化等装配问题 | 保持现有错误语义，不伪装 200/空历史 |
| 任务取消或客户端断开 | 不吞 CancelledError；走既有 worker 排空/pin 清理 |

不新增 409，不做自动重试，不在 SQL 存储故障后调用 legacy Session/文件 reader。任何阶段失败都不返回看似成功的空 ChatHistory。

## 8. 前端范围

ReadOnlySessionChat 已具备详情解析、边界渲染、分页、错误重试与空归档处理，预计无需修改生产 TSX/API 类型。
增加/调整测试，验证：真实形状的详情 has_more=true 时加载更早消息；最后一页停止；Chat metadata 支持任务消息转换；归档失败可重试；失败详情不显示“无聊天记录”的成功空态。

只为满足修复验收修改必要的 fixture/断言。不顺带调整滚动、布局、summary 列表或 sessionApi 的 metadata 合并设计。

## 9. 变更文件与职责

| 文件 | 本次职责 |
|---|---|
| src/swe/app/routers/tracing.py | 共享目标校验闭包、初次 SQL metadata 错误包装、current/archive_page 分派 |
| tests/unit/routers/test_tracing_chat_history_parity.py | SQL 分派与回调、初次/正文故障、legacy回归 |
| tests/unit/app/conversation_storage/test_history.py | 用真实 HistoryStore 覆盖 current 的内容、边界、目标审批 context 和清理 |
| tests/integrated/conversation_storage/test_api_mysql.py | 修订旧 Tracing API 断言；真实 SQL/NAS HTTP 对照与隔离/并发 |
| console/src/pages/Analytics/BusinessOverview/components/UserDetailModal/ReadOnlySessionChat.archive.test.tsx | 详情驱动分页及 Chat metadata 消费契约 |
| analysis/playbook/common-errors.md | 记录“有归档但看板无法加载”的根因、排查入口和回归入口 |

HistoryStore.current/archive_page 实现、旧 tracing_view 实现、runner/api.py 通用 helper 和生产前端代码不作为预定修改文件。若新测试揭示 reader 自身缺陷，另行列明原因与范围后处理，不把其他长会话优化混入本次补丁。

## 10. 测试与完成标准

### 10.1 最小失败回归

先增加真实 HistoryStore 行为 + 路由组合测试：同一目标 Chat 有 memory、task、error、独立 anchor 和已 compact 归档。旧路由应因排除消息、chat=None、has_more=false 而失败；修改后通过。

AsyncMock 分派测试仍保留，但不得仅返回人为 has_more=true 就宣称问题修复。已有 test_indexed_tracing_uses_target_view_and_archive 改为断言 current 被调用、旧 tracing_view 不被调用，并验证 current 与 archive_page 都收到可执行的 authorize。

### 10.2 后端语义矩阵

- 比较同一目标 Chat 的 /chats 与 /tracing/chats 当前可见 messages/status/chat/archive；两请求间不发生写入时结果一致。
- memory、task-run、error、未进入 memory 的 anchor、已被 primary/归档抑制的 anchor、重复 source IDs 与 fragment、hidden-context。
- 有/无归档；多页；相同 cursor 高水位下 append；reset epoch；空页；边界所在页。
- 初次 lookup 与 indexed reader 返回不同归属的 ChatSpec，回调拒绝且无正文返回。
- 不同 service_unit/tenant/source/agent 下相同外部 Chat ID；操作员与目标用户不同；历史 metadata 缺失仍可访问正确 scope。
- 审批状态使用目标 context；成功/异常/取消后操作员 context 不受污染。
- 在初次 metadata hydrate、current/archive 正文读取、最终复核各阶段注入故障，验证 404/422/503 及不回退文件 reader。
- 删除/revision/binding 变化在 reader 复核阶段明确失败；成功、授权拒绝、异常与取消后验证 reader pins 释放。NAS worker 未结束时不得提前释放。
- 活跃任务期间读取不争抢 Session 执行权，不改变 SQL execution/terminal 状态。

已有 test_api_mysql.py 的 HTTP Tracing 用例不再断言只返回 approval/task、chat=None、has_more=false；新增真实归档数据并对照 Chat API。直接调用 store.tracing_view 的测试保持旧语义，因为本设计不修改该内部 API。

### 10.3 真实环境与页面链路

使用现有专用隔离 MySQL fixture 和真实本地 NAS bundle，执行 API/current/archive 测试；未配置库而 skip 时明确记为未验收。
在测试环境以真实 SQL authority 后端打开已知有归档的目标会话，确认网络请求依次为详情与 history，最终呈现准入问题/失败记录/归档并停止分页；并验证越权和NAS故障。前端模拟API测试不能单独替代这一端到端验收。

无需引入新的全套端到端基础设施；可以复用测试环境的固定数据执行页面验收并保留请求/响应证据。

## 11. 发布与回退

该修复无需 DDL、数据回填或重跑迁移。作为一个独立修复提交交付，使用现有受控发布路径上线。

发布验收包括详情和归档响应正确、错误码正确，以及读放大和延迟未出现明显异常。current 比旧 tracing_view 读取更多层和 boundary，属于预期语义成本；用相同固定数据记录SQL/NAS次数与延迟，不编造尚未定义的SLO。

回退只恢复本次路由和校验改动，不回滚存储数据；回退会重新出现已知看板展示缺陷，需作为决策代价记录。不为这个小修复新增 feature flag。

## 12. 设计自检

- 范围仅限第一个问题，无 checkpoint/compact/summary/数据库驱动重构。
- 明确选择路由切换而非全局改变 tracing_view；保留直接调用该方法的旧测试。
- SQL 初次 metadata 查询与后续 reader 的错误映射均有覆盖。
- 当前消息与归档分页分开；不承诺跨请求的一致快照或把 page limit 当 fragment 数。
- 对象检查保持历史 metadata 缺失兼容；真实 scope 不由 metadata 决定。
- 没有宣称网关权限撤回、容量治理或真实环境验收已由本次设计实现。

2026-11-04 已按本设计完成路由修复与回归测试。真实 HistoryStore 的装配、回调及清理组合测试集中放在 `tests/unit/routers/test_tracing_chat_history_parity.py`，复用已有 `test_history.py` 和 retention 测试，未修改 reader 实现。

实施结果与验证限制见[修复报告](../reports/2026-11-04-tracing-history-parity-fix.md)。代码验证通过不代表专用 MySQL 或真实页面已验收；这两项仍待对应环境补齐。
