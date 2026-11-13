# 会话迁移：失败租户隔离与正常租户独立上线

本篇是 **显式 `--tenant-isolation` 模式**的操作增补。未使用该选项的全量严格流程仍按 `2026-11-02-conversation-migration-and-launch-runbook.md` 执行。本篇覆盖的部分发布顺序、预览连接数据库、runtime v3 和分享最终 DDL 与旧流程不同，不能混用。设计依据是 `2026-11-13-conversation-tenant-cutover-design.md` 和 ADR 0046。

## 1. 边界与上线单位

- `(service_unit_id, tenant_id)` 是发布单位。同账号全部 source、Agent、workspace 和分享必须一起通过，不能跳过坏 Agent 后发布该账号。
- `source_root/config_root/snapshot_root` 是冻结输入；`original_root` 是新版实际工作区根；`nas_root` 是新正文存储。冻结输入必须与所有服务的在线 `original_root` 分离，不能把在线目录当快照。`snapshot_root` 是旧分享正文根，不等于“在线工作区根”。
- 启动迁移前停止全部旧 writer、定时任务和其他写入口并建立备份；冻结源持续保留。程序接受停写证据引用，不替代停止进程或存储冻结。
- 新运行时仅放行 SQL 状态为 `published` 且凭证核对通过的账号。失败、未注册账号返回 HTTP 503，错误码 `conversation_tenant_not_published`，不回退旧文件。
- 已发布 A 可在新版写入，B 的后续普通预检、导入、续跑、校验和发布直接跳过 A，不再读取其冻结正文。多实例从 SQL 动态读取准入，无需为 B 发布重启 A。
- 无可信账号归属的错误不能随意跳过：会保守阻断受影响服务/账号。MySQL、共享盘 EIO/空间耗尽、锁丢失、提交结果不确定和取消会终止本次命令；不是可忽略的坏租户。

## 2. 环境准备

先按旧手册完成版本/依赖核验、备份、停写与冻结、应用库及 DML 账号准备。除现有会话表、分享 extension 和迁移控制表外，本分支的 `scripts/sql/conversation_migration_control_tables.sql` 还创建 `swe_conversation_migration_tenants`；已有控制表部署也须由 DBA 执行最新幂等建表脚本。迁移 CLI 不执行 DDL。

命令在当前发布镜像/本工作树执行，Python、配置加载方式须与现场一致：

```bash
export SWE_PYTHON=/opt/python/bin/python
export PYTHONPATH="$PWD/src"
export AUDIT_DIR=/audit/conversation-tenant-20261113
export APP_SCHEMA=REPLACE_APPLICATION_SCHEMA
export MIGRATION_WORK_DIR=/migration-work
mkdir -p "$AUDIT_DIR" "$MIGRATION_WORK_DIR"
chmod 700 "$AUDIT_DIR" "$MIGRATION_WORK_DIR"
CS_RESOURCES=(--workers 4 --io-workers 4 --memory-budget-mib 512
  --work-dir "$MIGRATION_WORK_DIR")
"$SWE_PYTHON" -m swe conversation-storage discover --help
"$SWE_PYTHON" -m swe conversation-storage retry-review --help
"$SWE_PYTHON" -m swe conversation-storage rollback-tenants --help
```

`AUDIT_DIR` 是受限审计目录，与冻结输入、NAS 根和工作区互不包含；`APP_SCHEMA` 是目标应用数据库名。CLI 读取既有数据库配置，`APP_SCHEMA` 只用于命令参数核对，不会自动覆盖配置中的 `SWE_DB_NAME`。配置已正确加载时无需重复设置环境变量；环境实际解析的库名必须与冻结 manifest 一致。不要把数据库密码写入命令、文档或报告。

执行镜像必须安装项目要求的真实 `trace_sdk`。本地无替身的 CLI help 核验仍因缺少该包而失败；测试套件中的 tracing fake 不能代替镜像依赖验收。上述 help 命令必须在实际执行镜像全部退出 0，才可开始停机迁移；不要将 `tests/fakes` 加入生产 `PYTHONPATH`。

`workers/io-workers` 范围为 1–32，内存预算为 128–65536 MiB。并发用于解析/IO，账号仍逐组处理；不保证多个账号同时发布。临时卷须真实可写并与源、NAS 分离。

## 3. 首次发现、冻结与导入

复用旧手册经审核的 `services.json`（真实绝对目录）、`boundary.json`（真实停写/快照/旧镜像证据）。首次 discovery 使用**新文件名**：

```bash
"$SWE_PYTHON" -m swe conversation-storage discover \
  --tenant-isolation --services "$AUDIT_DIR/services.json" \
  --output "$AUDIT_DIR/discovery.json" "${CS_RESOURCES[@]}"
```

退出 0 表示全部通过；退出 2 且 JSON `status=partial_success` 表示已完成发现但有隔离账号。检查 `discovery.failed-tenants.json` 中账号和原因，以及受限 `discovery.json` 的完整 blockers。不要把任意退出 2（例如 Click 参数错误）都视为成功；退出 1 或无有效报告应先处理共享/配置错误。

发现报告的 `ready=false` 保持“全体无阻断”的含义；隔离模式允许保留失败账号登记并冻结正常组。不要手改 `ready`、tenant state 或删除失败记录：

```bash
"$SWE_PYTHON" -m swe conversation-storage freeze \
  --discovery "$AUDIT_DIR/discovery.json" --boundary "$AUDIT_DIR/boundary.json" \
  --application-schema "$APP_SCHEMA" --manifest "$AUDIT_DIR/manifest.json" \
  "${CS_RESOURCES[@]}"
"$SWE_PYTHON" -m swe conversation-storage preflight \
  --manifest "$AUDIT_DIR/manifest.json" "${CS_RESOURCES[@]}"
"$SWE_PYTHON" -m swe conversation-storage import \
  --manifest "$AUDIT_DIR/manifest.json" --apply "${CS_RESOURCES[@]}"
"$SWE_PYTHON" -m swe conversation-storage verify \
  --manifest "$AUDIT_DIR/manifest.json" "${CS_RESOURCES[@]}"
"$SWE_PYTHON" -m swe conversation-storage finalize \
  --manifest "$AUDIT_DIR/manifest.json" --output-dir "$AUDIT_DIR/runtime" \
  --apply "${CS_RESOURCES[@]}"
```

每一步均检查输出，不因前一步退出 2 自动跳过后续正常组。启用 `set -e` 的脚本须显式保存退出码并解析报告后决定继续，不能用无条件 `|| true`。各步骤在审计目录写入独立时间戳 `*.failed-tenants.json`，仅包含账号、服务、阶段、错误码及安全原因；详细源路径/内容只留受限审计输入。正常租户无需混入失败清单。

**隔离模式的预览也连接数据库**，以可靠排除已发布账号；与旧严格模式无 `--apply` 预览不同。预览不等于发布。普通预览不登记导入状态，但可能恢复上一次中断的证据重审 pending。

会话输入预算只覆盖 `chats.json`、`sessions/`、`dialog/`、经审核的 session override 及关联 ledger，分享按明确记录路径读取。不递归业务附件/代码/技能目录。真正会话输入仍受字节、单条记录和条目预算约束；路径越界、软链接、特殊文件或未映射会话数据仍拒绝。

## 4. 正常租户独立上线

`finalize --apply` 对逐租户语义校验成功、源未变化、路径/目标结构正常的账号提交 `published`；该 SQL 状态是权威，`import` 或 `verify` 不会上线。部分失败时可正常生成每服务 SHA-256 文件名的 `*.runtime.json`，内容是程序签发的 version 3 部署引用，不能手造映射或编辑凭证。

按旧手册的 Kubernetes 配置/密钥/挂载步骤，将**当前服务**生成文件安装到可信只读路径，并设置：

```bash
SWE_CONVERSATION_STORAGE_AUTHORITY=mysql
SWE_CONVERSATION_SCOPE_MANIFEST=/etc/swe/conversation/conversation-scopes.json
CMB_CAAS_SERVICEUNITID=REPLACE_REAL_SERVICE_ID
# SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET 由部署密钥注入，不能为空
```

文件名和挂载名可由部署系统映射，内容不改。服务身份、NAS 根、数据库 server UUID 和 migration ID 必须一致。所有实例升级至支持 v3 准入的同一版本；旧 writer 保持停止。发布后检查正常账号读写、分享访问、失败账号稳定 503。注册账号的可信 scope 与配置仍需正确，任意请求传 tenant ID 不会获得新账号准入。

`published>0` 与整体 `ready=false` 可以同时成立。所有账号发布后，分享列仍允许 NULL 时会返回 `pending_schema_finalize=true`、退出 2；这不撤销已发布账号。

## 5. 失败重试

### 输入未变的失败

修复临时环境/容量问题后，继续**同一 manifest 和迁移 ID**：

```bash
"$SWE_PYTHON" -m swe conversation-storage resume \
  --manifest "$AUDIT_DIR/manifest.json" --apply "${CS_RESOURCES[@]}"
"$SWE_PYTHON" -m swe conversation-storage verify \
  --manifest "$AUDIT_DIR/manifest.json" "${CS_RESOURCES[@]}"
"$SWE_PYTHON" -m swe conversation-storage finalize \
  --manifest "$AUDIT_DIR/manifest.json" --output-dir "$AUDIT_DIR/runtime" \
  --apply "${CS_RESOURCES[@]}"
```

verify/finalize 失败的租户先 resume 恢复完整 imported 状态，再 verify/finalize。不要清控制表、删 bundle、改账号/service ID 或重新 freeze 后写入同一半成品库。

### 首次 discovery 的配置/输入需要修正

初次失败组不会因普通 resume 自动恢复。仅当该账号**尚无业务行且尚无迁移票据**时，可以审核修复冻结输入，再重新设置只读并生成新的完整发现报告。服务/root、账号集合和原始分享记录必须保持不变；不得修改已发布账号。修改冻结输入是受控证据修订，记录修复人与理由，不是开放旧 writer：

新 discover 仍会盘点全部冻结账号的正文，包括已发布 A 的冻结正文；不会读取 A 的在线新正文。`retry-review` 内部再次发现时则使用已发布组的固定映射与计数，跳过其正文盘点。普通 resume/verify/finalize 也跳过已发布组，不能把这几种不同操作混为一谈。

重审须使用原 manifest 的完整 `source_shares`，不能默认读取在线分享表：A 上线后新增分享或增加访问计数属于正常业务，不得写回冻结证据。先只读提取原始完整分享集合；以下命令独占创建私有文件，已存在时拒绝覆盖：

```bash
"$SWE_PYTHON" - "$AUDIT_DIR/manifest.json" "$AUDIT_DIR/shares.frozen-review-1.json" <<'PY'
import json, os, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    shares = json.load(stream)["source_shares"]
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump({"shares": shares}, stream, ensure_ascii=False)
PY
"$SWE_PYTHON" -m swe conversation-storage discover \
  --tenant-isolation --services "$AUDIT_DIR/services.json" \
  --shares-file "$AUDIT_DIR/shares.frozen-review-1.json" \
  --output "$AUDIT_DIR/discovery.review-1.json" "${CS_RESOURCES[@]}"
"$SWE_PYTHON" -m swe conversation-storage retry-review \
  --manifest "$AUDIT_DIR/manifest.json" \
  --reviewed-discovery "$AUDIT_DIR/discovery.review-1.json" \
  --apply "${CS_RESOURCES[@]}"
```

首次使用过 overrides 时，重审 discover 也须传入完整审核后的 `--overrides` 文件，保留未修改账号的可信身份映射。新报告可能仍退出 2；核对剩余失败集合符合审核结果后，才继续 retry-review。

接着执行上一节 resume/verify/finalize。重审会重新核对完整配置覆盖，对可修改组更新证据和同步起点，保留已发布组；SQL CAS、只读快照及 pending 文件用于审计和中断恢复。不要删除 `*.review-pending.json` 或修订快照；重跑命令会在命名锁内判断 SQL 已提交还是未提交，未知状态拒绝继续。

已有迁移业务行/票据的账号不支持直接修改输入后 retry-review，也不提供自动删除部分数据的 reset。应保持隔离，另行制定受控数据修复/恢复方案。

## 6. 全部账号完成后的分享 schema 收口

DBA 确认全部登记账号均 published，并安排元数据锁维护窗口后，用 fail-fast 客户端执行：

```bash
mysql --defaults-extra-file="$DBA_CNF" --database="$APP_SCHEMA" \
  < scripts/sql/conversation_tenant_publication_finalize.sql
```

此专用脚本**不删除分享**，未全部发布或定位/归属不完整会拒绝 ALTER。不要在部分上线期间执行旧 `conversation_share_service_unit_finalize.sql`（含 DELETE）。之后重跑同一 `finalize --apply`，整体才能进入 ready；已发布账号仍跳过源盘点。

## 7. 部分上线后的整体回滚

上线后的 A 有新 SQL/NAS 数据，不能简单恢复全部旧快照。必须停止新旧全部 writer、GC 和迁移，确认 IO 排空，审核当前配置完整覆盖；导出 A 的最新权威状态，并合并 B 的冻结会话输入。初次 discovery 失败导致输入闭包无法证明时拒绝合成，先补齐证据。

`rollback-tenants` 的 `--rollback-inputs` 是 JSON：含 `boundary`（SourceBoundary）、`writers_stopped_ref`、`io_drained_ref`、`current_config_review_ref`，以及 `target`（`image_digest`、`session_version`、`checkpoint_version`）和 `current_configs` 数组。每项配置映射恰为 `service_unit_id/tenant_id/source_id/config_path/review_ref`，路径必须在对应 original_root 内，真实身份与 Agent 配置必须核对。版本数值以固定旧镜像实际支持为准。

```bash
"$SWE_PYTHON" -m swe conversation-storage rollback-tenants \
  --manifest "$AUDIT_DIR/manifest.json" \
  --rollback-inputs "$AUDIT_DIR/rollback-inputs.json" \
  --output-dir "$AUDIT_DIR/rollback-new" --apply "${CS_RESOURCES[@]}"
```

输出必须是全新目录，不覆盖旧数据。CLI 只生成可审查内容，**没有运行固定旧镜像的读取并继续写入验证**，因此 `rollback_ready=false`、退出 2。停写引用只是运维声明。只有固定旧镜像逐服务根实际读取并继续写入通过，且隔离验证不破坏待恢复副本后，才可按受控恢复流程切换旧版。真实 NAS 与旧镜像验证必须另行完成；本地/隔离 MySQL 测试不代替现场验收。
