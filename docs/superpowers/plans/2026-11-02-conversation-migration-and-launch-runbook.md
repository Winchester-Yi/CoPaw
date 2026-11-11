# 会话元数据迁移及上线全流程操作方案

更新日期：2026-11-11。核对工作树：`.worktrees/conversation-mysql-nas`，HEAD `9b72c860bc5b73135b8102c5ee6190663dffbf32`，并包含核对时已有的 SQL 与测试未提交修改。
正式执行前须固定审核后的完整发布版本；仅记录 HEAD 不能代表本次工作树内容。
本文只交付方案，没有操作生产数据库、NAS 或 Kubernetes。

## 0. 当前实现分析与执行适用范围（2026-11-11）

本手册适用于**旧 JSON/NAS 权威首次整体迁移到 MySQL 权威**。以下三个概念必须区分：

1. 元数据入库：已实现；六张会话表保存身份、索引、执行状态、正文定位及引用关系。
2. 旧文件不搬动：可直接读取冻结原目录，但源与新正文根必须真正分离。
3. 历史正文零复制：**未实现**。`DirectImporter` 继承 `ConversationImporter`，仍生成不可变 bundle，保留必要旧格式材料；没有已接入运行时的 immutable legacy-source 注册/解析路径。不要按“只增 SQL、不增 NAS”申请容量。

### 0.1 代码链路及一致性边界

`discover → freeze → inventory/plan → preflight → import/resume → verify → DBA share finalize → finalize → mysql authority 启动`。

- `discovery.py` 用配置和磁盘证据确定服务/租户/source/Agent/workspace，读取全库旧分享，异常阻断；不是仅扫描 chats.json。
- `v3_manifest.py` 固定源、目标应用库及每服务正文根；`inventory_v3.py` 重新解析输入，并按已记录的租户同步起点做 mtime 变化检查。当前新 discovery 不持久化全源内容哈希，不能证明从 discovery 起字节始终不变；强冻结须由停写和存储措施保证。
- `conversation_storage_v3_cmd.py` 连接配置指定的**原应用库**，校验 `DATABASE()` 与 `@@server_uuid`，取得逻辑名 `conversation-migration:<schema>` 对应的命名锁（连接层默认哈希为实际锁 key，不能直接把逻辑名用于 IS_USED_LOCK 排查）；`DMLDatabase` 限定迁移 SQL。无 staging、无 RENAME、无自动 DDL。
- `journal.py` 两张控制表持久化 ID 分配、批次票据及来源/目标绑定；导入是多批提交，不是全库一个事务。中断使用同作业续跑。
- `direct_import.py` / `importer.py` 为各服务向 NAS 发布 bundle 并提交 SQL 定位。不可变正文与 SQL 发布协议配合，不能单独删除 SQL 行或正文“重置进度”。
- `verify.py` 做全量语义及引用核对；`finalize` 再验一次，SQL 作业置 ready，原子写出完成证明及每服务运行时文件。`verify` 本身不等于 ready。
- `authority.py` 与运行时启动检查凭证、服务、路径、库身份及最终 schema；SQL ready 后文件输出失败可以在仍冻结且未试写时重跑 finalize。

### 0.2 首次迁移与已有 MySQL 部署升级

| 现场状态 | 执行路线 |
| --- | --- |
| 六张会话表不存在、旧分享表存在 | 执行本文首次迁移流程 |
| 六张表已按当前定义预建且为空 | 核对结构后跳过已完成 DDL，不重放 CREATE |
| 同 migration_id 半完成导入 | 保留清单/库/NAS，核对绑定后 resume |
| 已有其他作业或已有正常新版会话 | 不执行首次迁移；需要专项增量/升级方案 |
| 旧版六表缺 Chat.external_session_key 等当前字段 | 不重放 CREATE；先取得现场 SHOW CREATE TABLE、行数与索引，再制定停写 ALTER/backfill/验证方案；当前仓库没有通用现网升级脚本 |

Chat 新增投影列 `external_session_key`、40 KiB 的九字段显示元数据投影及状态维护已接入当前实现。不能将更新后的 CREATE 文件当成已部署数据库的增量升级 SQL。

### 0.3 当前分享 DDL 的额外执行门禁

核对时 `conversation_share_publication_extension.sql` 是直接 ALTER，没有旧八列结构的自动 guard；必须先人工审核原表并确认没有同名新增列/索引。
`conversation_share_service_unit_finalize.sql` 会先 **DELETE 四个新增定位字段全部为空的行**，然后收紧服务字段。该行为可能使旧链接失效，并与 v3 的“原分享八字段集合保持不变”检查冲突。

本手册选择保留全部旧分享的迁移路线：第 10 节执行前必须确认 all-null、部分回填、服务不匹配、缺失 publication 均为零。非零时停止，先调查导入和原始分享正文，不能靠 SQL 删除绕过全量校验。即使业务批准废弃旧分享，也必须另行修订发现清单/保留策略与校验契约，不能在已冻结作业中直接删记录。

### 0.4 本次核对结论

本次仅做源码、SQL、文档及本地测试分析；未操作数据库、NAS、Kubernetes。当前工作树还存在未提交 SQL/测试变更，未改动这些实现文件。
修复前迁移契约回归为 **81 passed、13 failed**，失败全部在 `tests/unit/cli/test_conversation_storage_cmd.py`，主要表现为旧清单测试进入 v3 分派后缺少 `services`，以及不支持的旧命令/报错断言不匹配。不能将旧手册的 88 passed 当成本版本验收。后续诊断已确认这些失败源于旧清单工厂与 direct-v3 路由共用 version=3 的冲突，不能简单归为旧测试过时；见[13项CLI失败专项分析](../reports/2026-11-11-conversation-cli-contract-failure-analysis.md)。
本地实际执行 `PYTHONPATH=src ../../venv/bin/python -m swe conversation-storage finalize --help` 另因缺少 `trace_sdk` 导致命令加载失败（退出 2）；pytest 的依赖替身不能证明完整 CLI 可运行。执行镜像必须按项目依赖/内部包发布流程补齐该模块，并先让第 4 节全部 help 命令退出 0，不能在此状态开始停机迁移。

这些契约差异随后已通过独立 workflow 标识和向后兼容路由修复；扩大回归 **1087 passed、0 failed**，见第17节。正式放行前仍须补齐发布依赖，使用专用 MySQL 和实际 NAS 完整演练；本手册描述可执行流程，不宣称当前分支已经具备生产放行证据。

## 1. 先把结论讲清楚

推荐流程：**提前建表 → 关闭入口并停掉所有旧写入 → 冻结旧数据 → 自动发现 workspace → 固定迁移清单 → 预检 → 导入 → 全量校验 → DBA 收紧分享字段 → 生成上线凭证 → 各服务启动验证 → 开放入口。**

采用新版出问题后修复或恢复新版的路线，不把退回旧版、反向导出旧文件格式作为上线条件。

这次核对发现分支已继续提交，不能再按 `881ab1764` 的旧操作discover单执行：

| 事项 | 当前实际情况 |
| --- | --- |
| 自动发现 workspace | 已有 `discover`，按已有配置识别，包括 disabled Agent；异常归属阻断 |
| 自动生成迁移清单 | 已有 `freeze`，生成 v3 manifest，不用手写全部 workspace |
| 向预建表导入 | 已有 v3 `import` / `resume`，写目标应用库，不用 staging 和切表 |
| 程序执行 DDL | v3 迁移路径禁止；表结构由 DBA 的账号提前处理 |
| 不复制旧 NAS | 有条件支持：直接读原目录，但原目录必须冻结，且与新正文输出根互不包含 |
| 同一根目录里边扫描边生成新正文 | 仍不支持，不能靠换一个挂载别名绕过检查 |
| 不停机追赶旧系统的增量 | 不支持；这是一次停写批量迁移 |
| 上线后再补另一服务的历史数据 | 当前首次迁移模式不支持；同一库全部服务要一次协调完成 |

版本依据：`184310732` 实现直接导入 CLI，`56a740af9` 增加真实 MySQL 用例，
`07ebaae69` 扩展到多服务，`436a10505` 修复 inspection 兼容性。
设计稿和 SQL 文件中仍有“待实现/草案”等历史说明；本文以当前代码和命令帮助为准。

## 2. 迁移什么，什么仍放 NAS

| 数据 | 迁移后的存放位置 |
| --- | --- |
| Session、Chat、轮次、消息索引、执行状态、正文位置、发布与保留关系 | MySQL 六张会话表 |
| 历史分享 token、创建时间、访问计数等 | 保留原 `swe_chat_shares`，只回填新增的服务和正文定位字段 |
| 会话正文、工具内容、归档、恢复需要的完整内容 | 新 NAS bundle，不是把所有正文塞进 MySQL |
| 迁移进度、原数据到新 ID 的映射、完成证明 | 两张迁移控制表，以及审计目录中的文件 |
| Agent 配置、技能、附件等其他业务文件 | 继续使用原业务目录；本次不迁移整个应用配置体系 |

一共涉及 **七张业务表 + 两张控制表**。首次导入时六张新会话表必须为空，原分享表可以且通常应该有历史记录。
不要删除分享表重建，不要把各服务都填成迁移机器当前的服务标识。

不复制迁移输入也仍会产生新正文及保留恢复材料，不能按“原文件都在，所以不增加 NAS 空间”估算。
在完整演练中测量新 bundle 实际总量、文件数、备份/快照占用和临时余量，再确认容量；不假设固定一倍或两倍空间足够。

还需预留迁移执行机/Pod 的临时盘：`migration/legacy.py` 会通过 `tempfile.TemporaryDirectory` 和 `shutil.copyfile` 为旧格式解析建立工作副本。为 `TMPDIR` 指定独立可写临时卷并配置足够 ephemeral-storage/卷容量，在大 Session 和归档样本上实测峰值、失败清理及运行内存；“不复制源 NAS”不代表不使用临时空间。临时卷不得与冻结输入或正文根重叠。

同一服务的多个实例使用同一稳定 `CMB_CAAS_SERVICEUNITID` 和同一正文 NAS 根；
不同服务可以共用 MySQL，但正文 NAS 根必须隔离。所有服务使用同一个迁移作业、同一份 manifest 协调全局 ID。

## 3. 上线前必须确认的条件

- 发布镜像固定到 digest，包含这次核对的代码、`scripts/sql/` 和所需 Python 依赖；不要只从主工作区的旧安装包执行。
- 迁移执行环境能同时访问本次共享库涉及的全部服务源目录及各自正文输出根；执行机、迁移 Pod 和应用 Pod 中的冻结路径绑定要一致。
- 所有实际 workspace 都保留，配置中的原绝对路径能通过 `original_root` 映射；自定义路径越界时先处理，不能静默漏迁。
- 数据库须兼容脚本和当前驱动。当前代码会查询 `@@server_uuid`、使用 MySQL 命名锁并绑定服务器身份，实际 MySQL/TDSQL 及高可用代理必须演练通过。
- 在生产同类 NAS 上验证跨节点独占发布、fsync、可见性及进程中断后的读取；本地磁盘通过不能代替这一项。
- 使用完整、代表性的生产快照演练，记录扫描、导入、两次全量校验、备份和启动耗时，按实测确定停机窗口；目前没有可信的固定分钟数。
- 用实际只有 DML 权限的迁移账号完成一次演练；当前真实 MySQL 测试文件使用能准备隔离库的 fixture，不能仅凭该测试声称生产最小权限已经验证。
- 验证数据库和 NAS 的成套备份能在隔离环境恢复；包含 ready 完成证明和运行时配置，不只检查备份文件存在。

真实 NAS 已有验证资源，可在专用验证 PVC 上执行；不要用业务 PVC 做故障注入。执行环境需有 `envsubst`、`kubectl`：

```bash
export VERIFICATION_NAMESPACE=REPLACE_TEST_NAMESPACE
export VERIFICATION_IMAGE=REPLACE_REPOSITORY_AT_SHA256_DIGEST
export VERIFICATION_PVC=REPLACE_DEDICATED_TEST_RWX_PVC
envsubst '${VERIFICATION_NAMESPACE} ${VERIFICATION_IMAGE} ${VERIFICATION_PVC}' \
  < deploy/conversation-storage-verification-job.yaml > "$AUDIT_DIR/conversation-nas-verification.yaml"
kubectl apply -f "$AUDIT_DIR/conversation-nas-verification.yaml"
kubectl -n "$VERIFICATION_NAMESPACE" wait --for=condition=complete \
  job/swe-conversation-storage-verification --timeout=660s
"$SWE_PYTHON" scripts/rehearse_conversation_storage.py collect \
  --namespace "$VERIFICATION_NAMESPACE" \
  --job swe-conversation-storage-verification \
  --output "$AUDIT_DIR/nas-cluster-evidence"
```

上面的 `SWE_PYTHON`、`AUDIT_DIR` 按下一节准备；镜像必须是 `仓库@sha256:64位十六进制`。
已有同名 Job 时先保留旧证据、按测试环境流程清理，再建新一轮，不把旧 Job 通过当作本轮通过。
采集结果必须 `status=passed`，包含实际两个不同节点的证据；还要演练 Pod 被终止与恢复，不以单个健康检查替代。

## 4. 准备执行环境和账号

后续 shell 片段按 **Bash** 编写。示例路径、库名、namespace、服务标识都要替换为实际值。
命令分阶段执行，每阶段看报告再继续，不要一次把全文粘贴运行。

```bash
set -euo pipefail
umask 077
cd /app
export SWE_PYTHON=/app/venv/bin/python
export PYTHONPATH="$PWD/src"
export APP_SCHEMA=REPLACE_APPLICATION_SCHEMA
export AUDIT_DIR=/audit/conversation-20261102
mkdir -p "$AUDIT_DIR"
export DBA_CNF=/run/secrets/conversation-dba.cnf
export MIGRATION_CNF=/run/secrets/conversation-migration.cnf

"$SWE_PYTHON" -m swe conversation-storage --help
"$SWE_PYTHON" -m swe conversation-storage discover --help
"$SWE_PYTHON" -m swe conversation-storage freeze --help
"$SWE_PYTHON" -m swe conversation-storage finalize --help

db_dba() { mysql --defaults-extra-file="$DBA_CNF" --database="$APP_SCHEMA" "$@"; }
db_read() { mysql --defaults-extra-file="$MIGRATION_CNF" --database="$APP_SCHEMA" "$@"; }
run_cs() {
  local report_name="$1"
  shift
  "$SWE_PYTHON" -m swe conversation-storage "$@" \
    > "$AUDIT_DIR/$report_name.json" 2> "$AUDIT_DIR/$report_name.stderr.log"
  "$SWE_PYTHON" -m json.tool "$AUDIT_DIR/$report_name.json"
}
```

`/app`、Python 路径按镜像实际内容调整；本地目标工作树可用 `../../venv/bin/python`。
必须指向本分支的 `src`：这次核对发现，共用 venv 的已安装包仍可能指向主工作区，
不设 `PYTHONPATH` 会出现 `No such command 'conversation-storage'`。

两类数据库账号分开：

- DBA 账号：你人工运行建表、扩列、收紧等 SQL 所用。迁移 CLI 不使用这个账号。
- 迁移账号：只允许本次所需表的 SELECT/INSERT/UPDATE/DELETE 和相关元数据可见性、命名锁访问；无 CREATE/ALTER/DROP/RENAME/TRUNCATE 权限。原分享表只需读取及更新新增字段，可进一步收紧。

CLI 从现有环境读取 `SWE_DB_HOST`、`SWE_DB_PORT`、`SWE_DB_USER`、`SWE_DB_ACCESS`、`SWE_DB_NAME` 等。
由 Secret 注入迁移账号，`SWE_DB_NAME` 必须等于 `APP_SCHEMA`，不要指向临时库。
当前 `SWE_DB_ACCESS` 读取逻辑会去掉前四个字符，沿用部署已有前缀约定，不要直接塞裸密码。
mysql 客户端的 cnf 使用真实密码，不使用这个前缀规则。两种配置须指向同一个目标。
应用配置另有启动限制：`SWE_DB_HOST` 不能是字面值 `localhost`，因为 `_app.py` 会在此值下禁用数据库连接，mysql authority 随后会启动失败。使用已验证的数据库服务 DNS/地址；CLI 能连接 localhost 不代表应用能启动。
不要开 `set -x`，不要把密码或原始分享 token 输出到公共日志。

```bash
test "$SWE_DB_NAME" = "$APP_SCHEMA"
db_read -e 'SELECT DATABASE(),@@server_uuid,@@version,@@version_comment,@@innodb_page_size,@@sql_mode;'
db_read -e 'SHOW GRANTS FOR CURRENT_USER;'
```

MySQL 命令使用预先安全分发的 cnf。本文不提供在命令行拼明文密码的做法。

## 5. 你提前建表：两次 DBA 操作，程序不建表

先检查现状并备份原库，尤其是原分享表。下面的脚本不是重复执行脚本，发现已有对象时先核对，不能直接重放。

```bash
db_dba -e 'SHOW CREATE TABLE swe_chat_shares\G' > "$AUDIT_DIR/share-before-ddl.txt"
db_dba -e 'SHOW TABLES;' > "$AUDIT_DIR/tables-before-ddl.txt"
```

DBA 按原库备份流程完成备份后，人工准备结构：

```bash
db_dba < scripts/sql/conversation_metadata_tables.sql \
  > "$AUDIT_DIR/ddl-six-tables.log" 2>&1
db_dba < scripts/sql/conversation_migration_control_tables.sql \
  > "$AUDIT_DIR/ddl-control-tables.log" 2>&1
```

已有分享表扩列单独处理：

```bash
db_dba < scripts/sql/conversation_share_publication_extension.sql \
  > "$AUDIT_DIR/ddl-share-extension.log" 2>&1
```

分享扩列脚本直接执行 ALTER，不会自动验证原八列结构；DBA 必须依据 SHOW CREATE TABLE 人工审核原结构和新增列/索引均符合预期。不符就停，不要删列或建一个空分享表绕过。
扩列涉及原业务表，可在演练证明旧版本兼容及锁影响可接受后提前执行；否则放到正式停写后做。
六表和控制表可以提前准备。`preflight` 会精确核对列、默认值、编码和索引，九表要求 InnoDB/DYNAMIC；分享表只接受旧八字段加四个指定定位字段，额外字段也会失败。建表成功不代表预检通过；现场差异应专项处理，不删除业务列绕过。分享表的最后一步 NOT NULL 收紧必须等导入和校验通过后，见第 10 节。
mysql 不加 `--force`，任何 SQL 失败立即停止；DDL 不能靠外层事务回滚。

## 6. 关闭旧写入并建立同一批数据的备份

覆盖共享库的**全部服务**：网页入口、通道、分享访问、Cron、重试、后台任务和维护脚本。
分享访问也会改计数，只关闭“发送消息”不够。

Kubernetes 操作模板：

```bash
export NAMESPACE=REPLACE_NAMESPACE
SWE_DEPLOYMENTS=(REPLACE_SERVICE_A_DEPLOYMENT REPLACE_SERVICE_B_DEPLOYMENT)
kubectl -n "$NAMESPACE" get deployment "${SWE_DEPLOYMENTS[@]}" -o yaml \
  > "$AUDIT_DIR/deployments-before.yaml"
kubectl -n "$NAMESPACE" get pods -o json > "$AUDIT_DIR/pods-before.json"
kubectl -n "$NAMESPACE" get hpa,cronjob -o yaml > "$AUDIT_DIR/autoscalers-and-cron-before.yaml"
```

先通过已有网关发布维护页/关闭入口，停止新任务，等待活动任务结束；
暂停 HPA、GitOps 自动纠偏和外部拉起机制，再把全部旧 writer 缩到 0：

```bash
for deployment in "${SWE_DEPLOYMENTS[@]}"; do
  kubectl -n "$NAMESPACE" scale "deployment/$deployment" --replicas=0
done
kubectl -n "$NAMESPACE" get deployment "${SWE_DEPLOYMENTS[@]}"
kubectl -n "$NAMESPACE" get pods -o wide > "$AUDIT_DIR/pods-after-stop.txt"
```

若还有 StatefulSet、独立 worker 或 Kubernetes CronJob，也必须暂停并确认正在运行的 Job 已结束。
只 suspend CronJob 不会终止已经运行的 Job。不能只看 Deployment desired=0 就断定进程已退出。
将网关、进程退出、后台任务停止记录保存为 `$AUDIT_DIR/writers-stopped.txt`。
若无法确认某个执行中的外部工具是否完成，先对账，不自动重放工具调用。

在这个停写边界做成套备份：原数据库、各服务完整 NAS、配置及旧镜像 digest。
可使用现有备份平台的数据库恢复点与 NAS 快照，记录真实恢复点/快照 ID。
如 DBA 采用逻辑备份，可用下列命令；这是备份步骤，不是建表步骤：

```bash
mysqldump --defaults-extra-file="$DBA_CNF" --single-transaction \
  --hex-blob --no-tablespaces "$APP_SCHEMA" \
  > "$AUDIT_DIR/database-stopped.sql"
test -s "$AUDIT_DIR/database-stopped.sql"
```

备份不能放在被扫描的源根或正文输出根内。共享库还有其他业务时，恢复不能盲目覆盖整库，
须按 DBA 审核的表级/时间点恢复方案保护其他模块数据。

## 7. 选好目录：优先不复制，但不要伪装目录边界

### 7.1 原目录可直接使用的布局

同一 NAS 可以这样组织，`legacy` 和 `conversation-body` 是真实独立子目录：

```text
/mnt/service-a/
├── legacy/                         # 原业务根；这里已有租户配置和 workspaces
│   └── default_RMASSIST/workspaces/default/
└── conversation-body/              # 新正文根；工具在里面发布 bundles
/audit/conversation-20261102/        # 审计文件，独立持久卷
```

`source_root=original_root=/mnt/service-a/legacy`，所以直接读原文件，原 workspace 路径不变。
`nas_root=/mnt/service-a/conversation-body`。不同服务用不同的正文根，不共用一个根。
最终运行时文件会把这两个角色分开记录，新版继续从原 workspace 加载业务配置。

在已审核的新输出位置创建目录，并给迁移账号和应用账号安排正确的 NAS UID/GID/ACL；不能把已有源文件移动进去：

```bash
mkdir -p /mnt/service-a/conversation-body /mnt/service-b/conversation-body
```

正式输入必须通过不可变快照、只读挂载或可靠权限控制实现强冻结。当前新 discovery 的 fingerprints 为空，不执行其中的全源 mode/哈希检查；不能声称 0755/0644 一定会被 CLI 拒绝，也不能把 CLI 通过当作已冻结的证明。
在所有 writer 已停、已备份的前提下，若 NAS 支持 POSIX ACL 且已演练恢复，可以先保存权限再临时去掉写权限。
以下对每个服务实际的源/配置/分享根执行；相互包含的根只处理最外层一次：

```bash
export ORIGINAL_INPUT_ROOT=/mnt/service-a/legacy
getfacl --absolute-names --recursive "$ORIGINAL_INPUT_ROOT" \
  > "$AUDIT_DIR/service-a-permissions-before.acl"
# 确认输入没有符号链接、特殊文件；不能用 chmod 跟随链接改到其他目录。
"$SWE_PYTHON" - <<'PY'
import os, stat
from pathlib import Path
root = Path(os.environ['ORIGINAL_INPUT_ROOT'])
assert root.is_absolute() and root.resolve() == root and root.is_dir()
for path in [root, *root.rglob('*')]:
    mode = path.lstat().st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise SystemExit('unsafe input object: ' + str(path))
PY
chmod --recursive a-w "$ORIGINAL_INPUT_ROOT"
```

这是正式停写窗口内的权限调整，不是工具自动修改原业务文件。NAS 不支持可靠的 ACL 导出/恢复时，不照抄这一段；
改用经存储平台验证的冻结方式，或先补充权限保存恢复工具。不得让应用带着只读配置目录上线。
首次 import/resume --apply 才记录各租户同步起点，其后检查文件/目录 mtime 是否晚于该起点；这不是字节哈希校验，禁止用保留/回写 mtime 的工具修改输入。discovery 到首次导入之间的内容一致性尤其依赖运维冻结。冻结从首次正式 discovery 持续到 finalize 成功，期间不要恢复权限、修改配置或启动任何旧 writer。

### 7.2 旧数据本来就在整个 NAS 根里的布局

如果原根是 `/mnt/service-a`，输入也必须覆盖这个根，就不能把输出放到 `/mnt/service-a/bundles` 或其任何子目录。
当前版本没有“排除新输出子树后继续扫整个原根”的功能。

此时有两种实际选择：

1. 存储平台提供独立冻结快照输入，挂到 `/mnt/frozen/service-a`；不必做应用层全量复制，但需实际验证迁移账号只能读取、快照不能被其他 writer 改写。
2. 提供真实独立的正文目标根，且不破坏原业务 workspace 绑定。做不到就需要先修改目录扫描边界，再安排上线。

使用冻结快照时：`source_root=/mnt/frozen/service-a`，`original_root` 仍填原业务根，
`config_root` 和 `snapshot_root` 指向快照对应位置，`nas_root` 指向独立正文输出根。
不能通过给同一物理目录增加另一个挂载名、软链接或伪造空目录绕过保护。

## 8. 写短服务清单，CLI 自动发现全部 workspace

以下是第 7.1 节直接读原目录的双服务示例，只需服务级根映射，不手填每个 workspace。
按实际服务数量增减；共享库中存在分享的服务不能漏。

```bash
cat > "$AUDIT_DIR/services.json" <<'JSON'
{
  "services": [
    {
      "service_unit_id": "SERVICE_A",
      "source_root": "/mnt/service-a/legacy",
      "config_root": "/mnt/service-a/legacy",
      "original_root": "/mnt/service-a/legacy",
      "snapshot_root": "/mnt/service-a/legacy",
      "nas_root": "/mnt/service-a/conversation-body"
    },
    {
      "service_unit_id": "SERVICE_B",
      "source_root": "/mnt/service-b/legacy",
      "config_root": "/mnt/service-b/legacy",
      "original_root": "/mnt/service-b/legacy",
      "snapshot_root": "/mnt/service-b/legacy",
      "nas_root": "/mnt/service-b/conversation-body"
    }
  ]
}
JSON
run_cs 01-discover discover --services "$AUDIT_DIR/services.json" \
  --output "$AUDIT_DIR/discovery.json"
```

所有根必须事先存在、绝对且规范，不含软链接；应用 Pod 的最终原 workspace 也必须真实存在。
`snapshot_root` 是**旧分享 JSON 的查找基目录**，不是必须创建的 NAS 快照。
代码会找 `snapshot_root/<tenant_id>/chat_shares/<token>.json`，并校验原 `snapshot_key`。
如果旧分享实际存放在另一处，填那个已经冻结的服务级根，不要为了凑路径把 token 内容重新复制到 workspace。

`discover` 默认从数据库分页读取**全表**旧分享八字段，自动确定唯一服务归属；生产推荐不传 `--shares-file`。
同一 token 在声明的各服务快照根必须恰好找到一份。已删除原 Chat 的分享也要迁移。
`--shares-file` 只用于经过核对的完整导出或隔离演练，不能为了通过扫描传一个假的 `[]`。

特殊布局的服务级补充字段：

| 情况 | 如何补充 |
| --- | --- |
| 根目录直接放 config.json，没有租户/source 目录证据 | 填真实 `tenant_id`、`source_id` |
| 裸租户目录缺 source 编码 | 用可信登记填写 `tenant_sources`，例如 `{"alice":["RMASSIST"]}`；多来源不能猜 |
| profile 中 workspace_dir 是相对路径 | 填旧进程真实 `original_cwd`，不能按迁移进程当前目录解析 |
| 孤立 workspace / Session 身份缺失 | 使用证据绑定的 `--overrides`；不能把所有孤立数据归到 default |

必须检查 `discovery.json` 的 `ready=true`、`blockers=[]`，并由业务/运维核对服务、租户、来源、Agent、workspace 和分享覆盖。
自动发现不是自动证明输入清单包含了所有生产服务；服务清单需要与部署及存储资产清单核对。

遇到 blocker 时，保留报告，处理原因后输出新文件名重新扫描，工具不会覆盖现有 discovery 文件：

```bash
run_cs 01b-discover discover --services "$AUDIT_DIR/services.json" \
  --overrides "$AUDIT_DIR/overrides.json" \
  --output "$AUDIT_DIR/discovery-reviewed.json"
```

只在确实需要异常映射时使用上面这条。override 的 `position` 原样取报告候选键，
填写 `discovery_version=1`、真实 tenant/source/agent、原因和证据引用；必要时填写真实 Session 三元身份和相对路径。
不能修改现有报告中的 ready/blockers，不能删掉失败数据只迁移成功子集。

需要例外 Session 路径时，可按以下结构准备 overrides.json；值全部取可信证据，position 原样复制发现报告候选键，不自行拼接：

```json
{
  "overrides": [{
    "position": "REPLACE_POSITION_FROM_DISCOVERY",
    "discovery_version": 1,
    "tenant_id": "REPLACE_REAL_TENANT",
    "source_id": "REPLACE_REAL_SOURCE",
    "agent_id": "REPLACE_REAL_AGENT",
    "reason": "REPLACE_REVIEWED_REASON",
    "evidence_refs": ["REPLACE_IDENTITY_EVIDENCE_REFERENCE"],
    "session_paths": [{
      "user_id": "REPLACE_REAL_USER",
      "channel": "console",
      "session_id": "REPLACE_REAL_SESSION",
      "path": "sessions/REPLACE_ACTUAL_FILE.json"
    }]
  }]
}
```

Session path 相对于对应 workspace，不能越界。已登记 workspace 的 override 不得改变 tenant/source/agent，且必须有非空的例外 session_paths；不要为正常 workspace 批量添加空 override。


## 9. 固定清单、预检、导入、续跑和全量校验

记录真实停写边界。即使不退旧版，当前数据结构仍要求真实旧镜像 digest，作用是记录解析来源；不要伪造。
下面文件中所有 `REPLACE_` 值必须换成真实值，时间必须带时区；多服务旧镜像不同，要把各服务 digest 完整写入证据文件。

```bash
cat > "$AUDIT_DIR/boundary.json" <<'JSON'
{
  "snapshot_id": "REPLACE_REAL_FREEZE_OR_SNAPSHOT_ID",
  "captured_at": "REPLACE_REAL_TIME_WITH_TIMEZONE",
  "old_image_digest": "REPLACE_OLD_REPOSITORY@sha256:REPLACE_REAL_64_HEX_DIGEST",
  "evidence_refs": [
    "/audit/conversation-20261102/writers-stopped.txt",
    "/audit/conversation-20261102/source-freeze-record.txt",
    "/audit/conversation-20261102/backup-record.txt",
    "/audit/conversation-20261102/old-images.txt"
  ]
}
JSON
run_cs 02-freeze freeze --discovery "$AUDIT_DIR/discovery.json" \
  --boundary "$AUDIT_DIR/boundary.json" \
  --application-schema "$APP_SCHEMA" --manifest "$AUDIT_DIR/manifest.json"
run_cs 03-inventory inventory --manifest "$AUDIT_DIR/manifest.json"
run_cs 04-plan plan --manifest "$AUDIT_DIR/manifest.json"
run_cs 05-preflight preflight --manifest "$AUDIT_DIR/manifest.json"
```

如果采用 reviewed discovery，上面 `--discovery` 指向那份最终文件。证据文件须真实存在且可审计，
程序接受字符串引用不代表已经验证停写。`freeze` 不会替你停止进程或给 NAS 上锁。

`inventory/plan` 是源数据预览；`preflight` 才连接数据库核对预建结构、原分享未变化、
六表为空或已有行归本作业所有、作业绑定及锁等条件。预检 `passed=true` 才能继续。

```bash
# 只预览，不写目标库；不代替上一条数据库 preflight。
run_cs 06-import-preview import --manifest "$AUDIT_DIR/manifest.json"
# 正式导入：向预建应用表写数据，同时向各服务 nas_root 发布正文。
run_cs 07-import import --manifest "$AUDIT_DIR/manifest.json" --apply
# 只读的全量语义验证。
run_cs 08-verify verify --manifest "$AUDIT_DIR/manifest.json"
```

每条命令退出 0 后检查报告。`import` 输出迁移 ID 和批次状态，不带 `passed=true`，不能按那个字段判断它。
全量 `verify` 必须 `passed=true`、`blockers=[]`。它比较消息每次出现及顺序、Session、Chat、
归档、事件、checkpoint、分享和引用等，不只是统计总行数或抽看几条会话。

中断后保持停写和输入不变，复用**同一份 manifest、同一作业 ID、同一目标**：

```bash
run_cs 07b-resume resume --manifest "$AUDIT_DIR/manifest.json" --apply
run_cs 08b-verify verify --manifest "$AUDIT_DIR/manifest.json"
```

SQL 控制表是续跑权威，NAS 已写而 SQL 未提交、SQL 已提交而外部进度镜像未更新，都需由同作业对账。
不要清空控制表、删 bundle、改服务标识、重新 freeze 出新 ID 后继续写同一半成品库。
库中已有另一作业或未知行时先调查，不能覆盖。job ready 后不再执行 import/resume。

查看数据库阶段用 SQL；`inspect` 当前是源预览，不是数据库独立对账工具：

```bash
db_read -e "SELECT CONVERT(migration_id USING utf8mb4) AS migration_id,manifest_version,state,updated_at FROM swe_conversation_migration_jobs;"
db_read -e "SELECT entry_kind,state,COUNT(*) AS n FROM swe_conversation_migration_entries GROUP BY entry_kind,state;"
```

## 10. DBA 收紧分享归属，再生成上线凭证

全量 verify 通过后，DBA 先检查分享定位缺失和服务错误必须为 0。当前 finalize SQL 含 DELETE，执行前额外核对 all-null 行数为 0，确保不会删除任何历史分享：

```bash
db_read -e "SELECT COUNT(*) AS must_be_zero FROM swe_chat_shares WHERE publication_id IS NULL AND byte_offset IS NULL AND byte_length IS NULL AND service_unit_key IS NULL;"
```

上述结果不是 0 时立即停止，不执行下面的 SQL。保存导入前后分享行数和八个旧字段的完整受限导出，不能仅用总数证明 token/计数不变。

```bash
db_read -e "SELECT COUNT(*) AS invalid_shares FROM swe_chat_shares s LEFT JOIN swe_conversation_publications p ON p.id=s.publication_id WHERE s.service_unit_key IS NULL OR OCTET_LENGTH(s.service_unit_key)=0 OR p.id IS NULL OR s.service_unit_key<>p.service_unit_key OR s.byte_offset IS NULL OR s.byte_length IS NULL;"
db_dba < scripts/sql/conversation_share_service_unit_finalize.sql \
  > "$AUDIT_DIR/ddl-share-finalize.log" 2>&1
run_cs 09-finalize-preview finalize --manifest "$AUDIT_DIR/manifest.json" \
  --output-dir "$AUDIT_DIR/runtime"
run_cs 10-finalize finalize --manifest "$AUDIT_DIR/manifest.json" \
  --output-dir "$AUDIT_DIR/runtime" --apply
```

`finalize` 这个名字不代表程序 ALTER 表：它重新做最终结构检查和全量语义校验，写 SQL ready 状态并输出完成证明。
第一个 finalize 没有 `--apply` 只是预览，不替代正式验收。

成功后应有：

- 数据库 jobs 中本次作业 `state=ready`。
- `$AUDIT_DIR/runtime/completion.json`，包含本次全部服务的完成集合。
- 每个服务一个 `<服务标识的SHA256>.runtime.json`，其中保留原 workspace 路径和完整 migration_authority。

SQL 已 ready 但文件输出中断时，在**源仍冻结、没有启动新版写入**的前提下可重跑同一 finalize 重建一致文件。
不要手工拼 migration_authority 或改成别的路径来通过启动检查。
上线已经产生写入后，离线 verify/finalize 不再适用，不能拿冻结期内容去覆盖新的正常变化。

## 11. 恢复原目录正常权限，准备各服务部署

确认正式 finalize 已完成、所有 runtime 文件安全保存后，恢复第 7 节临时改过的权限：

```bash
setfacl --restore="$AUDIT_DIR/service-a-permissions-before.acl"
# 对其他实际修改过权限的服务逐一恢复，并核对与备份一致。
```

此后不要再启动旧 writer。恢复权限只为了新版继续使用原配置、附件等目录，
不代表允许回到 legacy 写入，也不要再对变动后的原源执行离线导入。
使用独立冻结快照的路线不需要恢复快照权限，只须保证实际业务 workspace 可正常访问。

先在原部署副本数为 0 时完成镜像、配置和挂载更新。每个服务分别操作，以下以 SERVICE_A 为例：

```bash
export SERVICE_UNIT_ID=SERVICE_A
export DEPLOYMENT=REPLACE_SERVICE_A_DEPLOYMENT
export APP_CONTAINER=REPLACE_APP_CONTAINER_NAME
export NEW_IMAGE=REPLACE_NEW_REPOSITORY_AT_SHA256_DIGEST
export SCOPES_CONFIGMAP=conversation-scopes-service-a-20261102
export CURSOR_SECRET_NAME=conversation-cursor-service-a

export RUNTIME_FILE
RUNTIME_FILE=$("$SWE_PYTHON" - <<'PY'
import hashlib, os
from pathlib import Path
name = hashlib.sha256(os.environ['SERVICE_UNIT_ID'].encode()).hexdigest()
print(Path(os.environ['AUDIT_DIR']) / 'runtime' / (name + '.runtime.json'))
PY
)
test -s "$RUNTIME_FILE"
kubectl -n "$NAMESPACE" create configmap "$SCOPES_CONFIGMAP" \
  --from-file=conversation-scopes.json="$RUNTIME_FILE" \
  --dry-run=client -o yaml > "$AUDIT_DIR/service-a-scopes-configmap.yaml"
kubectl apply -f "$AUDIT_DIR/service-a-scopes-configmap.yaml"
```

通过已有密钥平台预建 `$CURSOR_SECRET_NAME`，其中 key 为 `SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET`。
使用稳定随机值，同一服务所有副本一致，重启不更换；不要写入 ConfigMap、Git 或迁移报告。
沿用原应用数据库 Secret，应用使用应用账号，不能把迁移执行机的临时账号顺手发布进去。

生成挂载及 Secret 引用补丁，保留部署已有其他 volume 和 env：

```bash
"$SWE_PYTHON" - <<'PY' > "$AUDIT_DIR/service-a-runtime-patch.json"
import json, os
patch = {'spec': {'template': {'spec': {
    'containers': [{
        'name': os.environ['APP_CONTAINER'],
        'volumeMounts': [{'name': 'conversation-scopes', 'mountPath': '/etc/swe/conversation', 'readOnly': True}],
        'env': [{'name': 'SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET', 'valueFrom': {'secretKeyRef': {
            'name': os.environ['CURSOR_SECRET_NAME'], 'key': 'SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET'
        }}}]
    }],
    'volumes': [{'name': 'conversation-scopes', 'configMap': {'name': os.environ['SCOPES_CONFIGMAP']}}]
}}}}
print(json.dumps(patch))
PY
kubectl -n "$NAMESPACE" patch deployment "$DEPLOYMENT" --type=strategic \
  --patch-file "$AUDIT_DIR/service-a-runtime-patch.json"
kubectl -n "$NAMESPACE" set image "deployment/$DEPLOYMENT" "$APP_CONTAINER=$NEW_IMAGE"
kubectl -n "$NAMESPACE" set env "deployment/$DEPLOYMENT" \
  "CMB_CAAS_SERVICEUNITID=$SERVICE_UNIT_ID" \
  SWE_CONVERSATION_STORAGE_AUTHORITY=mysql \
  SWE_CONVERSATION_SCOPE_MANIFEST=/etc/swe/conversation/conversation-scopes.json
```

审核 patch 后执行：上例假设容器已有同名 env 时不带冲突的 `value`，挂载名/path 未被占用；
若已有同名配置，合并到实际部署文件，不能同时留下 value 和 valueFrom。
原 workspace 以及新 nas_root 必须按完成文件中的**同一绝对路径**挂载进应用 Pod，不能只挂旧目录而漏掉新正文目录。
同一服务各副本要挂同一份 NAS；各服务重复上述步骤，选择自己的 runtime 文件和服务 ID。
GitOps 管理的部署应把这些变更写回审核后的发布配置，不能让控制器随后恢复旧值。

## 12. 先内部验证，再逐步开放

入口仍保持关闭，每个服务先启动一个新版实例：

```bash
kubectl -n "$NAMESPACE" scale "deployment/$DEPLOYMENT" --replicas=1
kubectl -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" --timeout=300s
kubectl -n "$NAMESPACE" logs "deployment/$DEPLOYMENT" -c "$APP_CONTAINER" --since=10m \
  > "$AUDIT_DIR/service-a-startup.log"
```

启动会核对 ready 作业、完整完成集合、数据库身份、服务和 NAS/原 workspace 绑定以及最终表结构。
默认 authority 是 legacy，必须明确设置 mysql；不能通过移除完成证明来绕过启动失败。
当前完成证明绑定 `@@server_uuid`：数据库切换实例/恢复到新服务器时可能拒绝启动，
上线前必须演练实际高可用切换。当前没有独立的生产原库 server_uuid 原地重绑定入口；不能临时 UPDATE 控制表伪造成功，也不能把隔离恢复 API 当作 HA 原地修复工具。现场缺少实际可执行的恢复/发布切换方案时，不满足放行条件。

逐项做真实操作并保留结果：

| 验证项 | 通过要求 |
| --- | --- |
| 原会话列表与历史 | 多用户、多 Agent、归档分页可读，内容和顺序正确 |
| 新问答与重开 | 能写入，刷新/重启后仍存在，不把旧 JSON 当新权威 |
| Stop、重连、审批、工具/子任务事件 | 真实交互成功，状态不丢；请求按现有会话归属到正确实例 |
| 旧分享 | token 原值可用，原 Chat 已删的分享也可读，计数按原规则增加 |
| 服务/租户/来源隔离 | 使用相同外部会话 ID 的不同服务和用户不会串数据；错误服务请求被拒绝 |
| 多实例 | 扩到至少两个实例，在不同 Pod 间读到相同正文；并发和停止路由正确 |
| 重启 | 产生新写后重启可通过完成证明，仍能读到新增记录 |

健康探针 `/api/health/health` 只作存活参考，不能代替以上业务验收。
首次业务试写之后，若失败，也必须保护这批试写，不能直接恢复停写前备份让它消失。

全部服务内部验收成功后，按已记录的副本数扩容，恢复入口、小流量观察，再开放全部流量和定时任务。
恢复 HPA/GitOps 前确认它们的期望镜像、配置和副本策略已经更新，不会拉起旧版。
维护页/网关开关、HPA 暂停恢复以及原副本数来自第 6 节现网记录，不能在通用方案中猜测平台命令。

观察错误率、会话读写延迟、数据库连接/锁等待、NAS IO/错误、磁盘空间、分享成功率以及隔离异常。
阈值沿用本服务 SLO 和演练基线；超阈值先关入口，不边报错边继续扩量。

## 13. 失败怎么处理，以及上线后的备份

| 失败位置 | 操作 |
| --- | --- |
| discovery / preflight | 不导入；检查权限、路径、归属、缺文件或表结构；保留失败报告 |
| import 进程中断、网络故障 | 保持所有 writer 停止，保留 SQL/manifest/NAS，同作业 resume，再全量 verify |
| 源文件或分享计数变化 | 说明冻结失效，停止；先找到漏停的 writer，不能改 manifest 指纹掩盖变化 |
| 数据库身份/锁丢失 | 立即停止写入，确认实际连接目标、持锁状态和提交结果；不新建 ID 盲重试 |
| DBA 脚本失败 | 不启用；按实际 SHOW CREATE TABLE 对账，DDL 无事务回滚，不能直接重复全文 |
| finalize 已 ready，输出文件缺失 | 尚未试写且输入仍冻结时，同作业 finalize 重建；不要直接手造文件 |
| 应用启动失败 | 保持入口关闭，检查最终结构、ready 凭证、Secret、绝对挂载路径及数据库身份 |
| 已上线后业务错误 | 关闭受影响入口、保留新增数据，在新版修复；恢复需按同一恢复点处理 MySQL+NAS+完成证明 |

不提供自动退旧版步骤，也不把原文件没改等同于可以随便恢复数据库。
直接导入会分批提交，原分享表也已回填，失败时目标库可能处于半完成状态。
若决定上线前放弃本次迁移，由 DBA 按已演练的恢复方案处理全部相关表和 NAS，并保护共享库其他业务；
不要临时写 DELETE/TRUNCATE 清理，也不要只恢复数据库而留下不匹配的正文和完成证明。

上线后：

- 保留旧源、停写备份、manifest、报告、生成的 runtime/completion 文件和控制表；先不做旧数据物理清理或启用未经演练的 GC。
- 备份须覆盖所有服务的七张业务表对应范围、被引用 NAS 正文及最小完成证明；两张控制表不属于原七表行集，但 jobs.ready 不能随意删除。
- 当前代码有备份/恢复 API 及完成证明处理，没有 `conversation-storage backup/restore` 子命令；不要凭空运行这样的命令。
- 暂无成熟自动备份接入时，安排一次维护停写，将应用库备份、各服务 NAS 一致快照和运行时文件成套保存；不能只定时 mysqldump，放任正文/GC 同时变化。
- 在隔离目标库和 NAS 实际恢复验证。数据库服务器/根路径变更须区分第 16.2 节两条恢复路线；现有 API 仅支持隔离目标，不能为生产原库做 HA 原地重绑定。缺少实际恢复驱动和发布切换操作单时，这是上线前需完成的工作，不能靠“原库直接重放”替代。

## 14. 放行清单和本次核对证据

只有以下全部满足才开放正式流量：

- [ ] 全部服务及旧分享覆盖已人工核对；不能以局部成功子集代替全量。
- [ ] 真实 NAS 多节点与中断验证、完整数据演练及停机耗时评估通过。
- [ ] DBA 已备份原表并准备六表、分享扩展、两张控制表；DML 账号演练通过。
- [ ] 全部旧 writer 停止，停写边界的数据库与 NAS 备份可恢复。
- [ ] discover 无 blocker，preflight 通过，import/resume 完成，完整 verify 通过。
- [ ] DBA 分享收紧完成，finalize 通过，SQL ready 与所有 runtime/completion 文件一致。
- [ ] 原目录权限恢复，新镜像/服务 ID/mysql authority/Secret/挂载正确，没有旧实例混跑。
- [ ] 各服务单实例和多实例、试写、Stop/分享/隔离/重启验收通过。
- [ ] 高可用数据库身份切换和成套恢复流程已实际验证；新增数据保护措施明确。
- [ ] 流量开关、值守负责人和失败时关入口的操作明确。

2026-11-11 本轮核对使用第 0 节的当前工作树内容。修复前下述命令结果为 **81 passed、13 failed**；后续修复及扩围回归已 **1087 passed、0 failed**，范围见第17节。原失败原因见第0.4节。未配置/执行实库与集群 NAS 验收，历史合成扫描记录不视为本轮重跑结果。

此前三份交付报告能证明历史功能和样本流程的验证范围，但不能直接作为当前版本全量上线凭据：

- `2026-11-01-conversation-local-simulation.md` 明确记载，原完整样本扫描有 6 个缺少源文件的 Session 和 107 个缺少可信身份映射的 Session 文件，扫描未通过。
  后续成功往返只覆盖 40 Session / 55 Chat 的可解析子集。缺失原文件是数据问题，CLI 增加自动发现不会凭空补齐。
- 初始审计的 R1 多 scope 恢复、R2 超过 4096 publication 已有后续修复；不能把旧审计问题照抄为当前未修复。
  R3 旧格式导出依赖原源目录不作为本次不退旧版路线的放行条件。
- 服务隔离报告覆盖过相关单元及真实 MySQL 批次，但不是当前 v3 全量生产数据、实际 Kubernetes 和网络 NAS 的验收。

因此，应在正式停机前用这次完整源快照先跑 discovery，及早确认上述历史数据问题是否仍存在；
有 blocker 就先处理，不等停机后才发现，也不自动排除坏记录。预演使用独立冻结输入和隔离目标，正式停写后必须重新生成最终 discovery/manifest。

当前测试可重复命令：

```bash
PYTHONPATH=src ../../venv/bin/python -m pytest \
  tests/unit/cli/test_conversation_storage_discovery_cmd.py \
  tests/unit/cli/test_conversation_storage_cmd.py \
  tests/unit/app/conversation_storage/test_v3_discovery.py \
  tests/unit/app/conversation_storage/test_v3_preflight.py \
  tests/unit/app/conversation_storage/test_v3_journal.py \
  tests/unit/app/conversation_storage/test_v3_cli_orchestration.py \
  tests/unit/app/conversation_storage/test_v3_authority.py -q
```

真实 MySQL 隔离集成用例是 `tests/integrated/conversation_storage/test_v3_migration_mysql.py`，
需要专用测试连接，fixture 会创建/删除随机 child schema；没有测试配置时会 skip，不构成验收。
不要给它生产库连接。本地 88 项通过与上述合成扫描都不能替代第 14 节的生产等价验收。

## 15. 代码核对入口

- CLI：`src/swe/cli/conversation_storage_discovery_cmd.py`、`conversation_storage_v3_cmd.py`。
- 发现与冻结：`src/swe/app/conversation_storage/migration/discovery.py`、`inventory_v3.py`、`v3_manifest.py`。
- DML 边界与续跑：同目录 `preflight.py`、`journal.py`、`direct_import.py`。
- 语义验证与启动：同目录 `verify.py`、`authority.py`，以及上一级 `runtime.py`。
- SQL：`scripts/sql/conversation_metadata_tables.sql`、`conversation_migration_control_tables.sql`、
  `conversation_share_publication_extension.sql`、`conversation_share_service_unit_finalize.sql`。
- 多服务及提交后中断续跑用例：`tests/integrated/conversation_storage/test_v3_migration_mysql.py`。

本方案替代此前按 v2 staging/换表流程编写的迁移操作单。`install --apply` 不用于本次上线，
v2 import/install 写入口已经拒绝执行；以 v3 的 `finalize --apply` 完成离线验收和上线凭证生成。

清单的格式版本与工作流已分离：新 freeze 文件含 `workflow=direct`，旧工厂文件含 `workflow=offline`，格式版本均为3。已有无workflow文件按services/discovery_version结构识别；无需手工改版本号或重建迁移ID。旧预览/导出兼容不代表允许旧import/install写入。

## 16. 作业状态、执行交接与恢复操作单

正常持久状态链为 `planned → importing → imported → verified → ready`；`blocked` 需要查明原因。只读 verify 通常不会把 SQL 状态推进到 verified，finalize 才负责最终推进；不要手工 UPDATE state。

每个阶段交接必须保存：执行人/复核人、开始结束时间、命令退出码、报告路径、实际发布 digest、DB server_uuid/schema、服务和 NAS 映射、备份恢复点、下一步是否允许执行。每服务另记录旧副本数、镜像、挂载、ConfigMap、Secret 引用、HPA/Cron/GitOps 状态及入口开关负责人。

正式停写前，由 DBA 和存储负责人完成一次成套恢复演练，实际记录以下值，而不是只写“已有备份”：

| 恢复要素 | 必填内容 |
| --- | --- |
| 数据库 | 实际恢复平台任务/命令、备份 ID、binlog 截点、表范围、目标 schema/server_uuid |
| NAS | 每服务快照 ID、恢复任务/命令、目标绝对根、权限/UID/GID、bundle 完整性结果 |
| 配置 | 对应镜像 digest、runtime/completion、manifest、稳定游标 Secret 的保管引用 |
| 校验 | 恢复后结构/引用/内容验证报告，单实例启动及读写验收，RPO/RTO 实测 |

故障恢复按以下顺序执行：

1. 关闭入口，停止全部受影响 writer 和 GC，阻止自动拉起；记录故障后的数据库和 NAS 恢复点，保护新增数据。
2. 判断是可续跑的离线导入、尚未试写的准备失败，还是已有新版写入。仅第一种使用原 manifest 的 resume；SQL ready 后不再 import。
3. 尚未试写且决定放弃迁移：由 DBA/存储按已演练操作恢复停写边界的相关表与全部服务 NAS，恢复原权限和部署配置；在隔离环境核对后再决定恢复服务。保留失败作业证据，不手工 TRUNCATE 清场。
4. 已有试写/生产新写：先保存新增 SQL、被引用正文和工具副作用记录，按批准的恢复点和数据补偿方案恢复新版。不能只退镜像、切回 legacy 或仅恢复旧数据库。
5. 恢复到不同 server_uuid/schema/NAS 根时，当前权威绑定可能拒绝启动；必须使用已演练的隔离恢复与发布切换工具和生成的凭证；当前无生产原库原地 HA 重绑定入口。仓库没有通用 backup/restore CLI；若现场尚无审核过的可执行恢复工具及平台操作单，则本次上线条件尚不满足，不得手改控制表伪造凭证。
6. 保持流量关闭，完成结构与引用核对、历史/新会话/分享/隔离/重启验证，再恢复流量；记录恢复点导致的数据损失范围及已处理的外部工具副作用。

### 16.1 实库验收执行入口

在独立测试实例创建符合测试约束的空测试库，配置 `SWE_TEST_DB_HOST`、`SWE_TEST_DB_PORT`、`SWE_TEST_DB_USER`、`SWE_TEST_DB_PASSWORD`（裸密码）和 `SWE_TEST_DB_NAME`。库名须匹配 `test_*` 或 `*_test`，不得与应用库相同；fixture 会创建并删除自身随机子库，账号须具备对应测试权限。测试凭据与生产迁移 DML 账号分离。

```bash
PYTHONPATH=src ../../venv/bin/python -m pytest \
  tests/integrated/conversation_storage/test_v3_migration_mysql.py \
  tests/integrated/conversation_storage/test_schema.py \
  tests/integrated/conversation_storage/test_service_unit_schema.py -q
```

生产镜像中将 Python 路径替换为真实 venv。任何 skip 仅表示未执行，不能作为通过；集成测试通过后仍须按第 3、12 节在生产等价 NAS/Kubernetes 环境验收。DDL 与运行时必须来自同一冻结发布包，归档四份 SQL 的校验和及源码 diff；不得用本地 HEAD 号掩盖未提交 SQL 差异。

### 16.2 恢复路线必须分开验收

| 路线 | 输入和限制 | 执行与验收 |
| --- | --- | --- |
| 平台整套恢复 | mysqldump/备份平台恢复点 + 同边界各服务 NAS 快照 + 配置/凭证；不能直接传给应用 restore_backup | 由现场 DBA/存储平台执行；原库覆盖前保护其他业务及新增写入。恢复后须核对数据库身份绑定，身份变化没有现成原地重绑定入口 |
| 应用格式隔离恢复 | ConversationBackup.capture 生成的 metadata.json/正文备份集合；不是 mysqldump | 用独立恢复驱动调用 restore_backup，在空隔离库及全新 NAS 恢复，验证后另行发布切换 |

应用格式隔离恢复驱动需落实以下步骤，目前仓库没有覆盖整条链的通用运维 CLI；这份列表不能代替现场已审核脚本：

1. 停止源 writer 并保存证据，准备 `RestoreEvidence(source_writers_stopped_ref=..., isolated_target_ref=...)`，两项须为非空、真实可审计的停写/隔离目标引用；保管完整应用格式备份集合。
2. 生成 `conversation_staging_<32位小写十六进制>` 或 `test_conversation_restore_<32位小写十六进制>` 隔离 schema，既不能等于备份源 schema，也不能等于恢复进程环境当前应用 schema。用当前发布 SQL 预建六张会话表及两张控制表。仓库分享脚本只有 ALTER，空隔离库不能直接执行：须额外按备份源审核后的 `SHOW CREATE TABLE swe_chat_shares` 创建最终分享表，或先创建审核后的旧八列表再执行 extension 和空表 finalize，确保最终列集合与备份一致。七业务表全部为空。各服务新 NAS 目标须为空的新目录、规范绝对路径、无软链接，彼此以及与备份根互不包含/重叠。多服务传入 `bundles={service_unit_id: BundleStore(new_empty_root)}`，服务集合精确覆盖备份；`workspace_roots` 的键为 `(service_unit_id, tenant_id, source_id, agent_id)` 四元元组，值为真实存在的规范绝对 workspace 目录。
3. 调用 `restore_backup` 并保存完整返回报告。必须 `passed=true`、`complete_metadata_and_readers=true`、`restore_ready=true`、`needs_settlement=[]`，元数据、正文及 reader 校验通过；若有待结算项，先按 ended-owner 证据处理并重新验收，不能开放流量。
4. API 返回 `runtime_manifests` 字典，不会替你写文件或更新 Kubernetes。由恢复驱动安全落盘每服务 runtime manifest，保留返回的 migration_authority，再按第 11 节发布对应配置。
5. 发布切换须显式修改应用数据库 Secret/连接目标、NAS 挂载、scope manifest 和镜像，禁止只替换正文目录。恢复进程与应用切换后的环境角色不同，不能为通过 _restore_target 检查伪装源身份。
6. 全服务仍保持入口关闭，按第 12 节完成单/多实例、历史与新写、分享/隔离/重启验收，记录切换和回退的实际平台命令后才开放。

**完整性结论：首次迁移步骤可按本文逐阶段演练；当前恢复与 HA 仍依赖现场未交付的执行驱动/平台操作单，不能将本文视作已验证的生产应急恢复脚本。**

## 17. 独立子代理复核记录（2026-11-11）

三个子代理分别审计 CLI/源发现、SQL/数据完整性、运行时/恢复链路，均显式指定本工作树。发现并修正：源 mode 检查及哈希保证的错误描述、租户 mtime 截止边界、override 示例缺失、临时盘预算遗漏、localhost 应用启动限制、schema 精确匹配限制、HA/隔离恢复概念混淆及恢复输出落盘遗漏。

复核时目标 HEAD 为 `9b72c860bc5b73135b8102c5ee6190663dffbf32`，SQL 存在未提交修改；最终分享收紧 SQL SHA256 为 `d878ce2105d501d2aae58ddbac01185408eb6af90fa8728c0a54992adb853724`，含 DELETE，保留第 10 节零删除门禁。实际执行前重新固定发布包和校验和，禁止以 HEAD 替代未提交文件版本。

本轮本地验证：discovery/CLI discovery/preflight/journal/orchestration/authority 六组 **77 passed**；子代理定向三组 **42 passed**（与前者重叠，不能相加）；手册 25 个 Bash 代码块语法检查通过。随后已修复旧CLI工作流版本冲突，保留原13项用例并补有/无workflow、未知/冲突/缺字段路由回归；扩围发现并修复演练脚本目录fsync故障注入时机，最终 **1087 passed、0 failed（53.58秒）**。范围为整个 `tests/unit/app/conversation_storage`，两个迁移CLI测试文件及演练/语义基准脚本测试；Black、Flake8和diff空白检查通过。缺 trace_sdk 的完整发布CLI加载问题未处理，真实 MySQL、NAS、Kubernetes 和恢复演练未执行。

审计不等于生产验收。第 0.4、14、16 节中的未满足条件全部关闭并保存证据前，结论仍为“不放行生产迁移”。
