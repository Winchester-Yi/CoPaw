# Conversation storage verification evidence

本报告属于 Task 14。它记录验证范围与复现方法，不构成上线批准。2026-11-01 本次执行
未部署任何集群资源，未修改生产库或生产会话目录。当前 kubectl context 为本地
`orbstack`，不能代替目标 Kubernetes/NAS。目标 namespace、专用 PVC、包含当前脚本的
固定镜像 digest、固定旧镜像和全量数据副本仍需操作者提供。

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
| owner 崩溃、迟到 writer、Stop/终态竞争、reader/GC/backup fence | 必须运行同 adapter 故障矩阵 | test_publications_mysql、test_execution_mysql、test_retention_mysql、test_gc_mysql，以及备份集成证据 |
| §10 全量 API 查询矩阵 | 未验证 | 在同一 MySQL adapter 上运行契约；旧 NAS 单测不能替代 |
| Console 兼容 | 交由总验收记录 | Task14 规定四组前端测试实际输出 |
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
