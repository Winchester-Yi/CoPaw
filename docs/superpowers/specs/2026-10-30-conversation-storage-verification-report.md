# Conversation storage verification evidence

本报告属于 Task 14。它记录验证范围与复现方法，不构成上线批准。2026-11-01 本次执行
未部署任何集群资源，未修改生产库或生产会话目录。当前 kubectl context 为本地
`orbstack`，不能代替目标 Kubernetes/NAS。目标 namespace、专用 PVC、包含当前脚本的
固定镜像 digest、固定旧镜像和全量数据副本仍需操作者提供。用户随后确认当前
没有可用 Kubernetes/NAS，授权基于既有 `~/.swe` 只读副本做本地模拟；最新证据见
[本地模拟报告](../reports/2026-11-01-conversation-local-simulation.md) 与
[测试报告](../reports/2026-11-01-conversation-test-report.md)。本表的本地通过与目标
环境验收分开记录，不把本机目录或双进程替代真实网络 NAS/跨节点 Pod。

## 本次可复现证据

| 范围 | 命令/结果 | 结论 |
| --- | --- | --- |
| 脚本行为单测 | `PYTHONPATH=src venv/bin/python -m pytest tests/unit/scripts/test_rehearse_conversation_storage.py -q` | 本地 26 passed；包含真实子进程 exit7/RSS 与 preview 零执行检查 |
| 本地 bundle 原语 | `PYTHONPATH=src venv/bin/python scripts/rehearse_conversation_storage.py nas --mount /private/tmp` | passed；只证明本地文件系统。生成前缀 `.swe-conversation-verification-bb6187b4-9747-4aff-8b34-7d52eac83d60` |
| 同 adapter 新故障用例 | `PYTHONPATH=src venv/bin/python -m pytest tests/integrated/conversation_storage/test_faults.py -q` | 4 passed，19.34 秒；由主任务使用已核验隔离 schema 执行，配置已脱敏；仍不替代目标环境验收 |
| 文件检查 | `venv/bin/python -m pre_commit run --files scripts/rehearse_conversation_storage.py deploy/conversation-storage-verification-job.yaml tests/unit/scripts/test_rehearse_conversation_storage.py tests/integrated/conversation_storage/test_faults.py docs/superpowers/specs/2026-10-30-conversation-storage-verification-report.md` | 明确文件集合检查通过（适用 hooks passed；无对应类型 hooks skipped） |

原语检查直接使用 `BundleStore`，验证文件与父目录 fsync 成功、不可覆盖的硬链接发布、
文件 fsync EIO、发布后目录 fsync EIO、截短/缺失正文和 EACCES。目录 fsync 失败后
保留原 operation 对应文件，使用 `confirm_durable` 重确认；不覆盖或删除未知结果。
EACCES 用 open 边界故障注入，避免 root 运行时 chmod 仍可读造成假通过。
所有本地文件均位于生成前缀；脚本不递归清理、不接受会话目录作为测试对象。

## 真实双 Pod / NAS 复现

模板：`deploy/conversation-storage-verification-job.yaml`。在目标环境提供三项变量：
`VERIFICATION_NAMESPACE`、`VERIFICATION_PVC` 和
`VERIFICATION_IMAGE=repository@sha256:<64 lowercase hex>`。PVC 必须是独立空验收卷，
支持跨节点 RWX，StorageClass 与挂载选项应与目标 NAS 等价。镜像必须包含当前代码和
`/app/scripts/rehearse_conversation_storage.py`；不同镜像路径需在模板中显式调整。

由操作者渲染并审阅模板，确认没有 `${...}` 残留，再在确认的 context 上运行 Job。
本次没有执行下面的部署步骤：

```bash
# 操作者已设置并核验三个 VERIFICATION_* 变量之后
# VERIFICATION_IMAGE 必须为固定 digest；runner 在任何 NAS 写入前再次校验
python3 - <<'PY'
import os
from pathlib import Path
source = Path('deploy/conversation-storage-verification-job.yaml').read_text()
for key in ('VERIFICATION_NAMESPACE', 'VERIFICATION_PVC', 'VERIFICATION_IMAGE'):
    source = source.replace('${' + key + '}', os.environ[key])
if '${' in source:
    raise SystemExit('unresolved template variable')
Path('/tmp/conversation-storage-verification-rendered.yaml').write_text(source)
PY
kubectl --context TARGET_CONTEXT apply -f /tmp/conversation-storage-verification-rendered.yaml
kubectl --context TARGET_CONTEXT -n "$VERIFICATION_NAMESPACE" wait \
  --for=condition=complete job/swe-conversation-storage-verification --timeout=600s
# 即使 wait 失败，也保留 Job、Pod 和日志取证，不立即删除
PYTHONPATH=src venv/bin/python scripts/rehearse_conversation_storage.py collect \
  --namespace "$VERIFICATION_NAMESPACE" --job swe-conversation-storage-verification \
  --output /tmp/conversation-cluster-evidence
```

collector 使用当前 kubectl context；执行前显式切到审阅过的目标 context，并记录
`kubectl config current-context` 输出。其操作只有 get/logs，不授予 Pod ServiceAccount
API token。输出包含原始 Pod spec/status（节点、imageID、volumeMount、termination
exit code）、PVC/PV/StorageClass（含 mountOptions）与每 Pod 原始日志；Pod 日志还含
`/proc/self/mountinfo`。缺失权限或证据报错，不能判为通过。

Job 使用 required podAntiAffinity 保证不同节点，不满足时 Pending 是未验证。
测试前缀由 Kubernetes Job controller UID 生成，每个 Pod 只写该前缀；重试关闭，
失败后保留文件。双 Pod 以同 operation key 同时发布不同正文，必须恰好一方成功、
另一方独占冲突，双方读取相同完整获胜正文。collector 读取实际 Job UID，要求两个 Pod controller owner UID 与 Job 匹配，completion
indices 恰为 `{0,1}`。报告必须为 `indexed-nas`，其 UID、Pod 名、node、生成前缀和
operation key 一致且与 API 对应。必须恰好一份 `published` 和一份
`exclusive-conflict`，两方正文都精确等于获胜 index 对应的完整 `worker-N`。
collector 还逐项核对 verifier 容器的实际 `volumeMount.mountPath → volume.name → PVC`，
要求同一可写 PVC、无 subPath，报告 mountinfo 确认该实际挂载。仅声明 volume 或仅有
`{"status":"passed"}`/local-only 日志都不能通过。Job/Pod spec 必须使用同一固定 digest，
实际 verifier imageID digest 也须一致，且两容器 exit=0、原语检查完整。操作者应提供
单平台镜像 manifest digest；多平台 index digest 与运行平台 imageID 不同会保留为
unverified，需固定并复核平台镜像后重新运行。
这只验收 NAS 原语，不宣称验收节点电源故障/服务器缓存掉电持久性。

## 排空至恢复流量计时

`rehearse` 不内置停生产入口、备份或切表命令。操作者将已审阅的隔离副本演练命令
作为 argv 数组放入 JSON manifest。默认只 preview；`--apply` 才按序执行。
manifest 不是沙箱，`scope=isolated-copy` 是范围声明，执行前仍需核对每条命令的
数据库、NAS 根、namespace 和清单路径确实指向隔离副本。禁止复制业务凭据到报告。

必需阶段顺序：`drain, stop_writers, backup, import, verify, install, startup, smoke,
restore_traffic`；可在末尾追加 `rollback` 以计时受控回退。阶段命令返回非零时停止，
不自动继续恢复流量或回退；根据已发生的新写边界，按迁移手册选择恢复路径。
`verify` 应执行完整校验并以 blocker 返回非零，不能仅输出 success 文案。

```json
{
  "scope": "isolated-copy",
  "stages": [
    {"name": "drain", "argv": ["/path/to/reviewed-drain-command"]},
    {"name": "stop_writers", "argv": ["/path/to/reviewed-stop-writers-command"]},
    {"name": "backup", "argv": ["/path/to/reviewed-final-backup-command"]},
    {"name": "import", "argv": ["/path/to/reviewed-import-command"]},
    {"name": "verify", "argv": ["/path/to/reviewed-verify-command"]},
    {"name": "install", "argv": ["/path/to/reviewed-install-command"]},
    {"name": "startup", "argv": ["/path/to/reviewed-startup-command"]},
    {"name": "smoke", "argv": ["/path/to/reviewed-smoke-command"]},
    {"name": "restore_traffic", "argv": ["/path/to/reviewed-restore-traffic-command"]}
  ]
}
```

```bash
PYTHONPATH=src venv/bin/python scripts/rehearse_conversation_storage.py rehearse \
  --manifest /tmp/reviewed-isolated-stages.json --output /tmp/rehearsal-evidence
# 审阅 preview 后，在隔离副本明确执行
PYTHONPATH=src venv/bin/python scripts/rehearse_conversation_storage.py rehearse \
  --manifest /tmp/reviewed-isolated-stages.json --output /tmp/rehearsal-evidence --apply
```

每阶段输出独立日志，report.json 保存原 argv、实际退出码、单调时钟秒数、wait4 得到
的进程峰值 RSS 和总秒数。RSS 是 wait4 的单进程 high-water，不是并发子进程树总和；
脚本以 POSIX wait4 为前提。NAS 字节/文件数、DB 锁等待和查询 p95/p99 初始为 null，
需把真实监控采样作为附件，不能将 null 当零。不得将命令自身退出0解释为业务验收。
脚本单测仅以无害命令验证 preview 不执行、失败退出7被保留、后续阶段不执行和 RSS
确实有测量，不冒充真实停机演练。

## 尚未解除的交付门槛

| 门槛 | 当前状态 | 必需证据 |
| --- | --- | --- |
| 目标两节点/PVC/NAS | 未验证 | 真实 Job 双 Pod 日志和 cluster-evidence.json |
| 目标 MySQL/TDSQL 原子 DDL/索引/未知提交 | 未验证 | 隔离目标库的完整 integrated pytest 与 EXPLAIN/安装前后状态 |
| 文件 durable 前不可安装引用 | 隔离 MySQL 新增4例通过；目标环境仍待验收 | test_faults.py；跳过不能解除门槛 |
| owner 崩溃、迟到 writer、Stop/终态竞争、reader/GC/backup fence | 本地同 adapter 故障矩阵已有通过证据；目标 Pod/网络 NAS 故障仍未验收 | 当前 `35f749b4e`/runtime `74057c17b` 完整 MySQL 目录 274 passed/1 skipped（未配置样本的 app-bootstrap）；无 failures/errors，控制库清理；晚于收集的故障参数另跑。精确范围及 XML 见测试报告。 |
| §10 API 查询矩阵 | 本地实际 SQL/HTTP 矩阵已运行；部署环境 auth/bootstrap、NAS/外部 tracing 仍是独立边界 | [API 逐项矩阵](../reports/2026-11-01-conversation-api-evidence.md) 列出 58 个唯一 API/share case 的证据与未覆盖变体；既有 workspace 另有真实 lifespan/Auth/Tenant/SQL 1 项通过。 |
| Console 兼容 | 已有 8 文件、93 passed 的契约证据 | 四组前端测试精确范围见测试报告；不等于浏览器/跨 worker 实时 attach 全流程验收。 |
| 固定旧镜像反向导出验读/写入 | 未验证 | digest、原始命令、出口记录计数/身份及旧 reader/writer 结果 |
| 全量规模持续停机与回退预算 | 未验证 | 各阶段秒数、总时长、RSS、NAS规模、锁等待、p95/p99、回退耗时 |

§10 必须逐项验收：管理端无分页完整数组、page/count、双版本 cursor、完整详情、
无 user 的多 Session 匹配、同时间 registry tie、显式 chat_id 优先、冷 Turn 及旧无状态/
重复 source ID、archive 源 Msg limit/推进、context 缺失/未来 schema/stale、专家绑定
实时状态、目标 scope tracing/审批 overlay、全量 share-options、冷 Turn 选中分享、
孤立永久 token（原 Chat 已删/legacy_share）、断线/终态、CLI/Agent 路由、Cron receipts、
W+ 无 chats.json。未跑到真实 adapter 的条目须单列，不能用测试总数覆盖未运行项。

当前没有可测量的全量迁移时长，不给出分钟数或上线窗口承诺。后续执行应附原始
JSON/日志与运行环境，而非仅手工修改本表状态。

## 既有 workspace 本地模拟补充

原业务 workspace 未重新初始化。只读复制后的可解析 40 Session / 55 Chat 子集
完成导入、同 manifest 幂等重入、139 项语义核验和一致备份/空库恢复；新问答增量
又完成 fresh readers、增量备份/第二空库恢复、反向导出和当前树 legacy reader
继续写。源 workspace 205 个 regular files、4,140,461 字节逐文件 SHA-256 不变。
完整 inventory 仍因缺失源文件与可信身份映射 `ready=false`，不得当成全量验收。
本地 NAS 故障及双进程 20 轮竞争均有机器报告，但 `network_nas_tested=false`、
`two_nodes_tested=false`、`kubernetes_tested=false`。固定旧镜像没有提供，所有本地
导出 `image_verified=false`、`rollback_ready=false`；完整报告链接见本文开头。

## 本地切换故障证据矩阵

本轮仅扩充测试和验收记录，不修改生产实现。真实 MySQL 仍只操作生成的隔离
schema；NAS 为本机临时目录，原业务 workspace 保持只读。以下逐项区分真实
组件操作、应用边界注入和缺失的部署证据，不把测试函数退出 0 等同于生产演练。

| 故障 | 本地证据与断言 | 保留边界 |
| --- | --- | --- |
| 排空/未收敛超时 | `test_backup_prepared_timeout_leaves_no_fence_and_original_can_commit` 使用真实 prepared publication；期限设 0 后失败、无 active fence、原 operation 仍可 commit，重试成功；十阶段本地 ledger 另记录独立旧 writer running/持锁时等待超时，停止后提交 stopped 并退出 0，随后才导入/安装 | 独立本地 writer/备份收敛证据；部署入口关闭、全部活跃旧 Turn/队列与 Pod 排空仍未验证 |
| 备份正文不可读 | `test_backup_missing_body_retains_real_sql_pins` 实际删除 committed bundle；capture 失败、不产生成功 report，真实 SQL backup pins 保留；恢复正文后重试成功且旧 pins 未释放 | 本地文件缺失，不是真实 NAS 权限/网络故障 |
| 导入中断 | `import-interruption` 与 `v3-interrupted-message-batch` 分别在 source-artifact 和真实消息批次 commit 后、manifest 更新前抛错；磁盘 manifest 为 importing，SQL 为 committed，重载 manifest 后续跑、原行和 bundle 字节不变，verify 通过；v3 再重入计数不变并保留完整历史语义 | 应用异常注入，不是真实进程 kill；恢复必须保持原批次参数 |
| verify 正文缺失 | `missing-body` 实际 unlink 导入正文；verify 返回 position/detail blocker，install 拒绝且不生成 receipt，业务旧态与 staging 七表保留；恢复正文后核验成功 | 不跳过 blocker，不执行 RENAME |
| 原子冲突/版本不支持 | 既有真实探针验证冲突失败保持旧态、成功完整七表切换；新增四版本 fail-fast 单测确认任何 DDL 之前拒绝 | 版本返回值是 test double；不代表真实旧版本或目标 TDSQL 能力实测 |
| MDL 占用 | `metadata-lock` 独立真实连接事务 SELECT 原 share 持锁，install 被 preflight 拒绝、无 receipt、表名/计数不变；释放后同测试完成正常 install | 目标权限、instrumentation 和锁等待预算仍需目标环境验证 |
| RENAME 丢回复 | `lost-rename-reply` 先执行真实精确 RENAME，再在应用边界抛错；独立 observer 看到完整新态、旧 share 副本和无关表 91 不变，receipt 保持 rename_pending | 非真实网络断线；重试拒绝来自 staging 已移走后的 verify，receipt guard 另由单测覆盖 |
| 安装后启动失败 | `startup-failure` 实际安装后 readiness 先通过，真实删索引后 `verify_schema` 拒绝；禁止后续 DDL，仍 capture/export，永久 token/count=17 和无关表 91 保留 | readiness gate，非完整 FastAPI lifespan/进程启动失败；旧镜像未验证 |
| 已有新写后 smoke 失败 | `after-new-write-smoke-failure` 实际安装后正式 admission/start/finish，写问答/未知模块/解绑/新分享/新旧计数，再截断真实 share body 触发 StorageUnavailable；恢复正文后完整 backup/export，当前树 legacy reader 继续写 | 禁止逆向 RENAME；receipt 静态 phase 不充当无新写证明；固定旧镜像未验证，rollback_ready=false |

已核读的机器结果：四类 install fault 最终 XML
`/private/tmp/conversation-cutover-four-faults-final.xml` 为 4 passed，119.89 秒；
备份两项 `/private/tmp/conversation-backup-two-faults.xml` 为 2 passed，47.84 秒；
启动 readiness `/private/tmp/conversation-post-install-startup-failure.xml` 为
1 passed，33.92 秒；新写后 smoke
`/private/tmp/conversation-post-install-rollback-green.xml` 为 1 passed，47.54 秒；
四版本拒绝 `/private/tmp/conversation-install-version-failfast.xml` 为 4 passed，
1.25 秒。上述 XML 均为 0 failures/errors/skips，数据库包装器均确认隔离控制库清理
（版本 test double 用例不连接数据库）。不同运行不累计为一个 suite 总数。
消息批次专项 `/private/tmp/conversation-import-message-interruption-final.xml`
为 1 passed、0 failures/errors/skips，260.40 秒，包装器退出 0 且生成 control schema
清理；覆盖完整 v3 历史、消息重复 ID、archive、share 与源字节不变。

首轮四类故障 3 passed/1 failed 是测试重载 manifest 后未同步父对象，已修正并完整
重跑四类；不是生产导入器 bug 的有效 RED。新写后 smoke 首轮失败是非法 UUID
夹具；修正后通过。原始失败证据保留在测试报告，不把夹具失败描述为生产缺陷。

## 本地完整阶段 ledger

正式 rehearsal 工具以 `--apply` 执行的本地记录位于
`/private/tmp/conversation-local-stages-20261101-r2/ledger/report.json`：
drain、stop_writers、backup、import、verify、install、startup、smoke、
restore_traffic、rollback 十阶段均 exit 0，总计 98.1515 秒；每阶段 audit
含 operator、UTC 起止、argv、exit。原业务目录 205 个 regular files、
4,140,461 字节的逐文件哈希不变，生成 schema 已清理。

这是一个历史 Session/Chat 的隔离副本演练；startup 为 schema readiness，
restore_traffic 为本地 share 访问，rollback 为两个 scope 的一致 backup/export
与当前树 legacy reader 续写。固定旧镜像未验证，rollback_ready=false。
单进程 high-water RSS 最大 197,525,504 字节；锁等待、p95/p99、NAS bytes/files
仍为 null。逐阶段值和首轮包装器 schema 配置失败记录见
[本地模拟报告](../reports/2026-11-01-conversation-local-simulation.md)。
本地 ledger 缺口已补，目标环境连续停机/入口/全 writer 与规模预算 gate 保留。

本轮测试提交 `aa846b947` 的三个修改集成文件完整复跑：74 passed、
0 failures/errors/skips，1376.02 秒；XML
`/private/tmp/conversation-cutover-final-three-files.xml`，包装器退出 0、
生成 control schema 清理。该次包含全部新增故障参数及三文件原有用例，
与基线全目录 274 passed/1 skipped 分开记账。
