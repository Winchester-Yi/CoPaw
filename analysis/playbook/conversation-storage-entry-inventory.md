# 会话存储入口审计

2026-11-01，Asia/Shanghai。对象为迁移分支当前实现；本清单不是目标环境验收。
`SWE_CONVERSATION_STORAGE_AUTHORITY=mysql` 后数据库不可用即拒绝服务，不回退
旧文件。业务 workspace 与 NAS 根复用原有目录。

## 在线入口

| 入口 | SQL authority 分支 | 文件实现为何保留 / 验证 |
| --- | --- | --- |
| `_app.py` 生命周期 | `configure_runtime` 验证数据库、安装映射、精确 schema；关闭时 reset | legacy 不装配；readiness 无 DDL，`test_factory.py` / `test_factory_mysql.py` |
| `workspace/workspace.py` | 从可信 tenant/source/agent 解析 scope，要求现有目录并取得 `workspace_storage` | 正常 provision 的新空目录可接入；遗漏旧正文/非空 registry 必须迁移，不能静默收养 |
| `workspace/service_factories.py` | `storage.bind_workspace` 优先注入 SQL ChatManager、Session、archive | 仅 storage=None 的 legacy 分支构造 JsonChatRepository/ConversationArchiveStore |
| `runner.py:init_handler` | 优先 `storage.session_for` 并返回 | SafeJSONSession 创建在该 return 后，非 SQL 故障 fallback |
| `runner_integration.py` | 按 request.channel 选 Session，先持久化准入，再运行 Agent；终态提交后发终帧 | unknown 停止模型/工具推进，原 operation 对账；真实 MySQL Runner 专项 |
| `runner/api.py` | SQL ChatManager；详情/Turn/归档/context 的 `_indexed_history` 在旧 reader 前返回 | `_archive_store` 仅 legacy 使用；SQL/NAS 错误返回 unavailable，`test_api_mysql.py` |
| `routers/tracing.py` | 先解析目标用户 workspace，再 `indexed.tracing_view` | SQL 分支在文件 Session reader 前返回，不使用当前登录用户 scope |
| `chat_sharing/router.py` / `service.py` | 可信 Chat.channel 选择 Session；全冷热候选、选中问答与 publication CAS | 永久 token 无 Chat 存活 JOIN；孤立分享保留原 publication scope；真实 API MySQL 测试 |
| `emit_wplus_sop_event.py` | 可信 runtime scope 调 `workspace_storage`，注入 SQL ChatManager | 仅 storage=None 才检查 chats.json 并构造 Json repo；配置错误直接失败 |
| `cli/chats_cmd.py` | 继续调用既有 /chats API，保留 Agent header | 无独立 file repo；Click→HTTP→SQL/NAS 契约测试 |
| Cron / managed receipts | 使用 workspace Runner 已注入的 Session adapter；完整 root 保留 receipts/task_run | 任务会话沿用 console Session；agent 请求进入统一 Runner wrapper，显式 channel 依请求身份选取；Cron 定义/执行库不迁移 |
| `reme_light_memory_manager.py` | 注入 `_conversation_archive`；prepare/create/recover/reset 使用 SQL façade | 旧 attach_conversation_archive 只在无绑定分支；archive_binding 原子提交完整 root/checkpoint/event/epoch |
| Hook transcript / 审批拒绝清理 | SQL 无 `_get_save_path`，transcript path 为空；清理用 execution/mutator | 私有文件路径调用有能力检查，不生成 SQL 假 JSON transcript |

## 离线与维护路径

| 路径 | 权威边界 | 验证 / 处理 |
| --- | --- | --- |
| `migration/{legacy,inventory,exporter}.py` | 明确离线源 reader 或反向导出目标 | 不作在线 fallback；ledger/journal 为旧格式兼容与回退保留 |
| `cli/conversation_storage_cmd.py` | 显式 manifest/source/target，默认 preview，apply 才写 | 七表安装、verify、backup/restore/export；recover 另需精确 owner/generation 退出证据和审计 receipt |
| `cli/memory_storage_cmd.py` | group 入口 `require_legacy_storage` | mysql authority 下拒绝 NAS-only 命令，只供离线 legacy 维护 |
| `backup/worker.py` / `memory_storage.py` | Python backup/restore preflight 拒绝 mysql authority | 完整 SQL+NAS 备份走 conversation-storage，文件 zip 不代表完整备份 |
| `backup/shell_worker.py` / `scripts/backup/*.sh` | worker 外部 IO 前拒绝 mysql；executor、直接脚本同样拒绝 | 专项验证脚本不运行、不下载、不覆盖目标；拒绝不记为成功回滚 |
| `file_governance/archive_maintenance.py` / `routers/dream_logs.py` | bundles 为保留目录，扫描不跟 symlink；resolver 重验 resolved path | 真实 bundle direct/alias 不能当 orphan 或被归档/删除；引用回收只走专用 GC |
| `app/migration.py` / `routers/agents.py` / `bootstrap_state.py` | 旧布局迁移和正常 provision 可生成空 registry | 空文件非 SQL 在线源；此次迁移不初始化新业务 workspace |
| `archive_reader.py` | 仅复用旧类纯过滤/时间解析 helper | 正文从 SQL locator/pin + BundleStore 读取，不构造 file archive store |
| `runner/session.py` / `turn_ledger.py` / repo exports | legacy 和离线工具仍导出这些类 | SQL 工厂不创建旧 reader；符号保留不等于在线旁路 |

## 发现项与回归

1. 通用治理会把真实 bundle 当 orphan，且 alias 能绕过词法目录保护。用实际
   BundleStore fixture 复现后修复两处保留目录、扫描和 resolved-path 校验；
   `test_storage_governance.py` 覆盖 direct/alias 的扫描与直接请求。
2. Python BackupWorker 已拒绝 NAS-only，Shell executor/直接脚本曾遗漏。已补
   authority 前置拒绝；`test_memory_storage_backup.py` 验证无外部 IO/脚本/目标写入。
3. 分享曾读 console Session 而非 Chat.channel，列表曾用本进程 idle 覆盖 SQL
   活动状态；API 矩阵增量包含修复与真实 HTTP 回归，以最终 XML 为证据。

原 NAS 原语、两节点双 Pod、固定旧镜像继续写和生产规模演练仍是独立环境门槛。
