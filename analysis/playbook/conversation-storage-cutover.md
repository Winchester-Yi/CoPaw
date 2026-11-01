# Conversation MySQL/NAS 离线切换

适用：旧文件实现 → 七张 MySQL 元数据表及原 NAS 上的不可变 bundle。整个过程是一次连续停机；不做双写，不启新 workspace，不把 `SWE_DB_NAME` 改为源快照，也不备份/恢复整个共享业务库。

## 准备和清单

使用审核过的源码 checkout（安装器读取该 checkout 的 `scripts/sql/`）；普通 wheel 不包含这些离线 DDL。运行账号需要目标实例的 schema/global CREATE、ALTER、DROP、INSERT、SELECT、UPDATE、DELETE，以及 information_schema / performance_schema 可见权限。仅通过 role 获得的权限保守拒绝，须另行审核。

先使用 `MigrationManifest.create(path, SourceBoundary(...), workspaces)` 创建持久化清单。`SourceBoundary` 必须包含最终只读快照身份、带时区时间、`old-image@sha256:<64 hex>` 和停 writer / 只读挂载证据引用。每个 workspace 显式记录源快照 `root`、`tenant_id`、`source_id`、`agent_id`；可选 registry/sessions/dialog 相对路径及 `session_paths: [{user_id, channel, session_id, path}]`。不得根据散列/文件名猜测身份。

清单增加以下 `offline` 配置；路径示例须替换为已有挂载和审计目录：

```json
{
  "offline": {
    "application_schema": "shared_business_schema",
    "nas_root": "/existing/original/nas/root",
    "shares": [],
    "export": {
      "backup_path": "/audit/consistent-conversation-backup",
      "destination": "/audit/new-empty-export",
      "session_version": 3,
      "checkpoint_version": 2
    }
  }
}
```

`shares` 是在同一最终边界导出的完整旧分享八字段，另加 `snapshot_workspace_index` 和 `snapshot_path`。没有分享时才使用空列表。不得漏掉已删除 Chat 的永久分享。安装器会把原分享表八字段全部行与 staging 对比，包含 token、访问计数和时间。

staging 名称固定为 `conversation_staging_<migration_id 去掉连字符>`，旧 share 名称固定为 `swe_chat_shares_old_<同一后缀>`。不能任填 staging 或 SQL 表名。导入器把 schema/NAS/migration 绑定持久化到 manifest。目标 NAS 必须为原来的、已存在的根目录；冻结源应为独立只读快照挂载，不能把它用作目标 NAS。新 bundle 只占用生成路径，不删除旧目录或旧文件。

报告引用：所有命令 stdout 是 JSON，应保存到审计目录。安装器另写 `<manifest>.install.json`，其中含验证覆盖清单、同实例能力探针、执行前/后名称、精确正/逆 SQL。该文件是持久化切换凭据，应在变更记录中与 manifest 一同引用。禁止改一个 `passed: true` 绕过核验；`install --apply` 总会重跑完整语义核验并核对每个对象和源文件覆盖。

## 连续停机步骤

每步审计记录同一组字段：作业 ID、操作者、UTC 开始/结束时间、命令（脱敏）、退出码、report 路径、证据路径、判断、恢复动作。任何一步失败都保持入口关闭，不向后跳步。

1. **停入口。** 关闭 HTTP/通道入口、定时触发、自动重试和外部调用。记录开关状态和部署版本。验证没有新增请求。
2. **排空。** 等待全部实例活跃 Turn 收敛。核查 admitted/running/stopping/settling/unknown、进程内队列和任务写入。排空超时：取消本轮、保留旧版和当前数据，按旧版流程结算；不得把未结算 Turn 改成 completed。
3. **停全部 writer。** 停所有 Pod、cron、后台 worker、迁移脚本和 GC；验证副本均为零且无自动伸缩重启。分享访问会更新计数，也属于 writer。保存调度器与 Pod/进程证据。
4. **最终备份和边界。** 在 writer 全停后建立 NAS 只读快照、完整 workspace mapping、源 registry/session/archive、旧 share 八字段快照、旧镜像 digest。对源目录及文件去写权限，并验证挂载本身只读。记录 snapshot ID/时间和恢复演练报告。备份失败：保持停机或恢复旧入口，不能继续导入。
5. **Inventory / Plan。** 执行下列只读命令，保存报告并审核身份映射、原 NAS 根、生成 staging 和阻断项。所有预览都不连接数据库、不写文件。任何源可写、路径/身份冲突、未识别历史或未固定镜像都会停止。
6. **Import。** 先执行无 `--apply` 的预览；再显式导入。只创建作业生成的 staging schema 和七张表，share 使用原表 `CREATE TABLE ... LIKE` 保持八字段定义和索引，再应用扩展。导入使用有界批次和持久化 manifest，重跑保持原 ID/operation key。导入失败：业务表未切换，保留 staging/manifest 定位后续跑；不删除 NAS bundle。
7. **Verify。** 运行只读完整语义核验。任何 blocker 都禁止安装；报告不完整也禁止安装。该命令不更新 manifest 的 batch 状态。
8. **Install。** 审核预览后执行 `--apply`。安装器核对同实例、七张表结构/索引、原 share 全量行、触发器、外键、视图、权限和 MDL；在两个全新隔离 schema 中实测冲突失败全回退及成功七表切换。探针结束会清理自己的 schema。旧业务已有任一六张新表时拒绝覆盖。正式安装只运行一条包含八个 rename 对的原子 RENAME：原 share → staging 的生成旧名，七张 staging 表 → 业务原名。不会触碰其他业务表。
9. **启动前审核点。** 保存 `<manifest>.install.json`，核对 phase、before、after：staging 仅剩生成旧 share，业务七张目标名均存在，其他业务表不变。此时尚未启动任何新版进程。
10. **受控启动和冒烟。** 首次启动一个新 Pod前记录 UTC 时间和不可逆回退分支切换。从启动动作本身开始，统一按“可能已有新写”处理。验证相同原 workspace/NAS、历史、checkpoint、分享 token、Turn、并发和恢复。启动失败或冒烟失败仍保持入口关闭，进入完整反向导出分支。
11. **恢复入口。** 全部冒烟/监控/审计签字后扩容并恢复入口。保存首笔新写时间、版本 digest、NAS 验收和停机预算实测。没有真实双 Pod/NAS 与旧镜像读写验收，不宣称生产可发布。

```bash
swe conversation-storage inventory --manifest /audit/manifest.json
swe conversation-storage plan --manifest /audit/manifest.json
swe conversation-storage import --manifest /audit/manifest.json
swe conversation-storage import --manifest /audit/manifest.json --apply
swe conversation-storage verify --manifest /audit/manifest.json
swe conversation-storage install --manifest /audit/manifest.json
swe conversation-storage install --manifest /audit/manifest.json --apply
swe conversation-storage inspect --manifest /audit/manifest.json
```

## 回退分支和故障演练

**安装前失败：** 旧 share 和全部旧文件未改变。排空失败先结算旧 Turn；备份失败重新备份；导入失败使用同 manifest 续跑；验证失败修复明确 blocker 后重验；原子能力/MDL/权限失败停止安装。决定取消时仅恢复原镜像和原 workspace/NAS/入口，保留 staging、bundle、报告供审计。不执行共享业务库整体恢复，不批量 unlink NAS。

**安装后、任何新版启动动作之前：** 必须有连续停 writer 的审计证据，而且确实没有任何新进程初始化、冒烟、分享读取或管理员写入。先保持入口关闭并再次检查没有 writer/MDL，审核 receipt 的精确实际名称。仅此窗口可执行 receipt 中 `reverse_sql_before_any_startup_or_write`，一条原子 RENAME 把七表移回 staging、原 share 移回业务原名；旧 NAS 本来就未改。核对原 share 八字段/所有 token、六张新表均不在业务 schema、原快照文件不变后，以固定旧镜像启动。receipt 的 phase 只记录安装时状态，**不能证明此后没有写入**；无法证明从未启动则走下一分支。CLI 不提供盲目自动逆向。

**任何新版启动、初始化、冒烟、分享访问或新写之后：** 禁止执行逆向 RENAME、恢复旧 NAS 快照或恢复整个共享业务库。这会丢新会话和计数。重新停入口、排空并停全部 writer，使用 conversation 一致性备份能力记录全部 scope 的当前七表和所引用 NAS bundle；每个 scope 使用非空即拒绝的独立导出目录执行 export。清单 `offline.export.backup_path` 必须指向一致性 backup；导出的 `rollback_ready` 默认 false。必须在固定旧镜像隔离实例上验证全部原生格式可读且能继续写，形成每个 scope 的旧镜像验收证据，再由审核过的定向导入恢复原 workspace 和分享索引。任何源格式/完整性不支持则保持新数据和停机，不能部分恢复。

```bash
swe conversation-storage export --manifest /audit/manifest.json
swe conversation-storage export --manifest /audit/manifest.json --apply
```

**RENAME 响应丢失或检查失败：** receipt 保持 `rename_pending`。这表示结果未知，不表示回滚成功。保持全部 writer 停止，独立连接查询两个 schema 的精确七表及生成旧 share 名；核对只有“完整旧态”或“完整新态”。不得直接重跑 install（已有 receipt 会拒绝），不得手改 phase 伪造成功。若业务六表存在且 staging 只剩旧 share，按完整新态处理；若观察到混合态，保留数据并升级数据库诊断。只有完成状态对账和审核后才能采取对应回退分支。

演练至少逐项记录：排空超时、备份不可读、导入批次中断并续跑、故意缺失正文的 verify blocker、原子探针冲突/不支持、MDL 阻塞、RENAME 响应丢失、启动失败、冒烟后新会话和分享计数更新。最后两项必须证明选择完整反向导出而非直接恢复旧快照。

本地隔离副本的十阶段 operator ledger：
`/private/tmp/conversation-local-stages-20261101-r2/ledger/report.json`。
十阶段 exit 0，总 98.1515 秒；逐阶段 UTC/argv/exit 和边界见
`docs/superpowers/reports/2026-11-01-conversation-local-simulation.md`。
该证据覆盖一个历史 Session/Chat、独立旧 writer 超时/停止和当前树 legacy 续写；
不覆盖目标 ingress、全部队列/Pod writer 排空、全 scope 或固定旧镜像。

## 本地证据与生产门禁

`tests/integrated/conversation_storage/test_install.py` 的真实数据库用例只创建/删除随机生成的隔离 schema；覆盖探针失败原子性、七表安装、启动前逆向、无关业务表保持不变。连接缺失时跳过，不能把 skip 视为验收。运行时应使用项目虚拟环境及测试数据库环境变量，不输出连接配置/凭据。

生产门禁仍需目标数据库实例的完整探针/权限/MDL验收、真实原 NAS 原语与双 Pod、停机预算、固定旧镜像读取并继续写、以及全 scope 新写后反向导出。代码测试通过不能替代这些环境证据。

## SQL authority 进程退出后的结算

请求仍在本进程登记时，Runner 使用已关闭并排空 IO 的 IndexedQuery 对原 operation 对账；这不证明进程重启后的恢复。进程退出、Pod 被删除或一致性 restore 后，由离线 recover 命令从 SQL 重建原 Session/Turn/publication 身份。禁止凭 lease 到期判断进程死亡；先关闭该 scope 入口和全部重试，核查精确 Pod UID/进程 incarnation 已退出、模型/工具子任务已终止、NAS 写入已排空。保存控制面和 IO 排空证据。外部工具的业务副作用不会重放或撤销。

建立经过人工核对的 JSON 证据；owner_key、generation、turn_id 来自原准入 publication 和 Turn，不使用当前 Session generation 猜测；scope/Session 必须为原始逻辑身份。原 NAS 和 workspace 必须已经存在。

    {
      "version": 1,
      "schema": "shared_business_schema",
      "workspace_root": "/existing/workspace",
      "nas_root": "/existing/original/nas/root",
      "scope": {"tenant_id": "tenant", "source_id": "source", "agent_id": "agent"},
      "session": {"user_id": "alice", "channel": "console", "session_id": "original-session"},
      "turn_id": 123,
      "owner": {
        "owner_key": "exact-original-owner-incarnation",
        "generation": 7,
        "evidence_ref": "change-record/pod-uid-exit-and-io-drain",
        "operator": "reviewed-operator",
        "owner_stopped": true,
        "io_drained": true
      }
    }

    swe conversation-storage recover --evidence /audit/owner-exit.json
    swe conversation-storage recover --evidence /audit/owner-exit.json --apply --confirm-ended-owner exact-original-owner-incarnation --receipt /audit/recovery.jsonl

预览不连接数据库、不写文件；apply 要求确认字符串与原 owner 完全相同，且配置中的数据库与证据 schema 一致。命令在任何 DB 写入前将完整证据写入并 fsync receipt，结束记录 completed 或 unavailable。证据是操作者核实的声明，CLI 无法自行验证 Kubernetes/进程退出，自动化调用必须通过 EndedOwnerRecovery.verify_stopped 接入真实控制面验证。命令不初始化业务 workspace、不执行 DDL。

恢复先校验完整关联、原 owner/generation 和 active Turn，再 fence writer。已有 durable prepared 结果按原 key 和原不可变正文提交；缺失且尚未确认 durable 的原操作留下 aborted tombstone；未准入且没有正文的 reservation 标为 rejected 并释放。没有可提交终态时，以已确认完整 recovery root（含未知模块/Cron receipts）发布 interrupted；Stop 已获胜则发布 stopped。任何已确认正文缺失/截断、身份漂移、另一 owner 或未能确定提交结果都保持 busy/unavailable，修复后用相同证据重试，不伪造 completed、不重跑模型或工具。receipt 中 unavailable 不表示回滚。还需在目标 NAS 和真实 Pod 崩溃环境演练本段流程。
