# 独立 Agent 可观测：正文证据恢复运维

本文说明当前独立 Agent 可观测的正文证据恢复、密钥和工作区绑定操作。它使用独立 MySQL 投影和会话 NAS，不使用旧 Tracing 的身份或数据。日志采集器仍由部署方负责：SWE 输出受限 JSONL，采集器须将其送入独立观测库的 `swe_agentobs_inbox`；仅看到容器 stdout 不代表已入库或已投影。

## 部署与启用

1. 为独立观测库准备基础五表及索引，DDL 和字段契约见[独立观测设计](../../docs/superpowers/specs/2026-11-15-independent-agent-observability-design.md)及其所引用的 schema。该基础 schema 标为待 DBA 审核的草案，目标为 MySQL 8.0.21+，须先按现场数据库能力审核；运行时不自动建表。再由 DBA 对**同一观测库**执行 [`scripts/sql/agent_observability_outbox_recovery.sql`](../../scripts/sql/agent_observability_outbox_recovery.sql)。脚本增加 scope 登记表、恢复租约表，以及 inbox 的 `(event_id, processing_state)` ACK 索引。两张表使用 `IF NOT EXISTS`，最后的 `CREATE INDEX` 不具备重复执行保护；已有索引时先核对名称、列和顺序，避免重复执行失败。该脚本不负责创建基础 inbox 表。
2. 配置独立数据库 `SWE_AGENTOBS_DB_HOST`、`SWE_AGENTOBS_DB_PORT`、`SWE_AGENTOBS_DB_USER`、`SWE_AGENTOBS_DB_PASSWORD`、`SWE_AGENTOBS_DB_NAME` 及现场连接/TLS 参数；这些配置不读取 `SWE_DB_*`。确认采集器持续写入该库，投影器能处理 inbox。准备共享 NAS 及当前会话存储 authority（`legacy` 或 `mysql-metadata`），并配置 `CMB_CAAS_SERVICEUNITID`：恢复和离线工具要求此值显式存在，且所有参与同一 scope 的实例一致。不要依赖观测身份代码的默认值代替恢复配置。
3. 通过部署的 Secret 机制为所有写入和恢复实例配置相同的 `SWE_AGENTOBS_OUTBOX_KEY_ID` 与 `SWE_AGENTOBS_OUTBOX_SECRET`。key ID 非空且不超过 128 字符，secret 至少 32 个 UTF-8 字节。`SWE_AGENTOBS_OUTBOX_VERIFY_KEYS` 是旧 key ID 到 secret 的 JSON 对象，默认 `{}`；每个值同样至少 32 字节。同一 key ID 同时出现在当前配置和验证集合时，值必须一致。不要将实际密钥写入仓库、命令行参数、工单或日志。
4. 配置 `SWE_AGENTOBS_CURSOR_SECRET`（至少 32 UTF-8 字节）以支持证据续读；它与 outbox 签名密钥是不同用途。设置 `SWE_AGENTOBS_ENABLED=true` 后新 Run 才采集；采集快照在 Run 准入时固定。关闭采集后历史查询仍依赖观测库、授权和保留的 NAS 内容。
5. 启动后检查恢复初始化和 scope 登记日志，并以当前 Source 的 manager/admin 验证一次真实工具或模型答案：先观察 `pending`，提交后应能读取正文；确认 collector 入库、投影及重启后恢复。scope 身份为 service unit、tenant、source、agent，登记同时绑定规范化 workspace 根目录和 authority。恢复调度器扫描已登记 scope，以租约避免多个实例同时推进同一 scope；事件以固定 event ID 重放，直到 inbox 中对应事件处理状态为 `1`。入队本身不是 ACK。

outbox 保存的是随实际消息一起提交的签名事件信封和引用，事件不含正文。查询会复核作用域、Chat、Session、签名、引用及在线/归档唯一性；公开消息不返回内部信封。受影响的会话、归档、配置和密钥须作为恢复所需数据一起维护。不能通过手工修改登记表、复制签名或猜测 message ID 修复缺失正文。

在线正文核验对单个 Chat 的完整归档扫描设有 64 MiB 总量上限。超过上限后，即使目标消息仍在在线会话中，该 Chat 的正文也无法核验，相关 outbox 事件无法恢复；这是当前已知限制。离线迁移的 64 MiB 总量上限是独立限制。

## 密钥轮换

先在所有实例的 `SWE_AGENTOBS_OUTBOX_VERIFY_KEYS` 中同时加入新、旧 key ID 及各自 secret，并保持现有 active key 不变；确认每个实例已加载一致的验证集合。然后分批将 active key ID/secret 切换到新值，继续保留旧 key，验证新消息写入和旧消息恢复。保留旧验证密钥直至使用它签名的会话及归档记录已按保留策略清除，且相关恢复、离线迁移不再需要验证；仅 inbox ACK 不代表签名记录已从 NAS 删除。当前配置通过进程环境读取，变更后按部署流程滚动重启并验证每个实例。不要复用同一 key ID 表示不同密钥。

## 故障与降级

- 观测库初始化、scope 登记和重试在后台运行。聊天请求直接继续，启用采集时模型元数据仍会写 stdout。初始化缺少观测库、恢复表、ACK 索引、service unit 或有效密钥时，后台每 5 秒重试。scope 成功登记后，新保存的工具结果和答案可以建立证据关联。无法安全留存关联时发出 `not_saved` 元数据，事件仍依赖 emitter 和下游采集。
- 恢复读取配置和解析工作区路径在独立线程执行，每个 scope 等待最多 5 秒，超时后调度其他 scope；同一路径和 Agent 的在途读取不会重复提交，整个进程最多同时执行两次读取，不排队。底层 NAS 系统调用无法被线程超时或取消中断；若两个读取均持续卡住，配置恢复会暂时全部跳过并重试，且 Python 进程退出时可能等待这些线程。应恢复 NAS 连接；若无法恢复，按容器运维流程强制结束实例。
- 检查容器 stdout/stderr 中的 `Evidence recovery unavailable`、`Evidence recovery registration failed`、`Evidence scope recovery failed`、`Evidence outbox recovery cycle failed`、`Evidence outbox replay was not accepted`、`Evidence outbox backlog` 和 `Evidence outbox record failed verification`。结合观测库连通性、表索引、collector、workspace 根、authority 和密钥配置定位。
- 恢复扫描的内部归档游标可跨实例接管；旧版或失效游标会从归档开头重新扫描，重复投递仍以事件 ID 和 inbox ACK 去重。`Evidence outbox checkpoint reset`、`Evidence outbox Chat cursor reset` 和 `Evidence outbox archive cursor reset` 表示扫描位置已重置。`Evidence outbox chat scan failed` 表示该 Chat 的损坏或读取错误，本轮会继续扫描其他 Chat，并在下一完整轮次重试；持续出现时须修复对应存储，不能将跳过视为 ACK。离线迁移核验仍会对损坏数据直接失败。
- `Evidence recovery skipped invalid Chat directory entry` 或 `Evidence recovery skipped ambiguous Chat directory entry` 表示目录记录损坏或 ID 不唯一。后台恢复会跳过这些记录并继续处理可验证的 Chat，之后每轮重新检查；须修复原始目录才能恢复被跳过的记录。整个目录无法解析，或数据库行的分页身份本身损坏时，恢复暂停该 scope 并重试；离线迁移仍要求完整目录有效且无重复。
- `pending` 可表示事件已观测但消息尚未证明提交；`not_saved` 表示未留存关联；`unresolved` 表示身份不唯一或不匹配；`unavailable` 表示当前无法读取；`compacted` 表示正文在归档。删除、损坏、大小/扫描上限、权限或历史记录缺少可证明关联，都可能使正文不可用。不能将缺失事件或正文当作业务执行失败的证明。
- 日志输出、数据库写入和投影均可能延迟或失败。恢复扫描会重试已提交且仍可验证的签名信封；进程崩溃前从未提交到会话根或归档的消息不在恢复保证内。已 ACK 只证明 inbox 处理成功，不证明 NAS 正文仍存在或当前操作者仍有权限。

## 工作区路径或 authority 离线变更

scope 已登记后，workspace 根或 authority 改变会使普通登记和恢复拒绝旧绑定。使用下列离线流程；该操作只更新观测恢复绑定，**不复制会话数据、不切换配置，也不负责停写**。

1. 先让现有恢复链路排空，并确认相关 linked 事件在 inbox 已处理；备份观测库、会话元数据库（如使用）与源 NAS，在同一恢复点核对。停止所有相关写入实例、恢复 worker 和会修改源/目标会话或归档的进程；命令参数只是操作员声明，不会自动停机。保留停止状态直到核验和绑定更新完成。
2. 按会话迁移流程准备并验证完整目标数据与配置，确保新根目录存在且为目标配置解析出的规范路径。若涉及 `mysql-metadata`，工具使用现有 `SWE_DB_*` 连接相应元数据库，校验目标 scope 的工作区登记与新根一致。保持旧根及其所需 metadata 可读；工具分别完整扫描源和目标。离线扫描上限为 10,000 条目及 64 MiB，超限、损坏或歧义会拒绝操作。
3. 在受控环境加载与线上一致的 service unit、观测数据库、当前及所有需验证的旧签名密钥，执行（参数替换为实际非秘密身份和路径）：

   ```bash
   venv/bin/python scripts/agentobs_rebind_outbox.py \
     --tenant <tenant> --source <source> --agent <agent> \
     --old-root <旧工作区绝对路径> --old-authority <legacy或mysql-metadata> \
     --new-root <新工作区绝对路径> --new-authority <legacy或mysql-metadata> \
     --all-writers-and-workers-stopped
   ```

4. 工具核对完整 scope、旧绑定、目标配置、两侧唯一的签名事件与公开正文投影摘要、所有事件的 inbox ACK 和无有效恢复租约，成功后事务内更新绑定并清空扫描游标。摘要不输出正文；不一致、未 ACK、密钥缺失、在途租约或扫描不完整均拒绝更新。摘要证明两侧公开投影一致，不能证明扫描之前两个副本没有遭受相同篡改。
5. 保留源备份与密钥，启动受控实例，检查登记、恢复与证据查询，再逐步恢复流量。若命令失败，保持停写，检查失败原因并在相同冻结数据和配置上重新核验；不要直接手改绑定绕过屏障。数据库连接中断或结果不明时，先只读核对当前绑定和租约，再决定重试。回退同样需要以当前绑定为源、旧位置为目标执行完整离线核验。

会话 metadata 迁移的一般步骤见[会话元数据迁移](../../analysis/playbook/conversation-metadata-migration.md)。现场发布前须在目标 MySQL、共享 NAS 和实际采集器上演练数据库断连、实例重启、租约接管、恢复 ACK、轮换及离线回退。
