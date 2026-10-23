# 本地存储验证与迁移回退演练

日期：2026-10-16。范围：已批准的第二阶段存储方案验收，不修改业务存储实现，不启用格式开关，不迁移已有用户会话。

## 结论与环境

本地双进程验证、合成数据迁移/回退及优化前代码的兼容读写已通过。**真实两 Pod/NAS 验收未执行**：用户确认暂时没有 Kubernetes Pod，本机唯一 context 为 orbstack，API 连接被拒绝。因此 OpenSpec 6.3 仍未通过，不能据本报告开放生产新格式。

实际运行目录为 `/Users/shixiangyi/.swe/.storage-verification/20261016-3x1nwph6`，由 `mkdtemp` 新建，所有测试数据均为合成数据。未读取或迁移该目录外的用户会话。该目录和代码工作区的本机 filesystem device 均为 16777233；这不是 NAS 挂载证明。

验收代码基于 `c71ff6c3b`，另包含本次 verifier、Job label 及测试修改。旧版本读取使用独立 checkout **`ddb683634`**（第二阶段存储优化提交的父版本），同一项目虚拟环境；并非旧容器镜像的完整启动验收。

## 已执行结果

| 项目 | 本地结果 | 证据及边界 |
| --- | --- | --- |
| 独立进程 flock 争用 | 通过 | worker 0 的子进程持锁时，worker 1 非阻塞取锁失败 |
| SIGKILL 后接管 | 通过 | 只终止验证器自己创建的持锁子进程，worker 1 随后取得同一个锁 |
| 原子替换、file/directory fsync | 通过 | 1,000 次连续发布；写者逐次回读；并发读者 19 次有效样本，读取中途水位并核对最终 revision=1000；另运行 all 模式 1,000 次 |
| 硬链接独占发布 | 通过 | 已存在目标拒绝覆盖，删除暂存名后目标 JSON 保留；fsync 调用无报错 |
| checkpoint 异常退出 | 通过 | 子进程在根发布前/发布后直接退出：分别可见 0/1 条事实；新实例条件重试两次后均只保留 1 条 |
| session 异常退出 | 通过 | pending intent、冷文件已写、根已发布三个边界恢复；只保留根已提交的删除 |
| 实际 writer 并发 | 通过 | 两进程 checkpoint 序号连续；两进程 v3 session 计数/revision 不丢更新 |
| 专项回归 | 通过 | **76 passed in 8.73s**，覆盖新增检查、ledger、checkpoint、CLI、备份/恢复限制 |

同步到当前工作区（含 legacy 导出修复）后，同范围回归为 **78 passed in 8.96s**；目标文件 pre-commit 全部通过。

`fsync` 成功和进程退出恢复不等于模拟了物理掉电或 NAS 服务端故障；跨节点锁、客户端缓存可见性、服务端持久化仍需真实 StorageClass 验收。

## 迁移及回退数据核对

两套演练均执行真实 Click 命令：`preview`、默认只读 `migrate`、`migrate --apply`、`validate`、`export --destination`。预览和导出前后比较源文件树摘要，预览不迁移、导出不修改源数据。

Session：初始 86 个 turn，包含 UTF-8 正文、终态及未结算状态、两个 chat 和附加消息字段。迁移后修改冷 turn `t0` 的状态与回复，删除冷 turn `t2`，新增 `t86`。导出后逐字段核对 86 个 turn 及 session 附加字段，只规范化提交 revision；验证所有 v3 元数据已去除。当前代码按旧格式继续追加后为 87 个 turn。

Journal：迁移前 60 条事件、36 条归档消息；迁移后继续写入、归档并 reset，最终导出包含 3 个 epoch、88 条事件、2 个 batch 和 80 条消息。逐字段比较事件 ID/时间/facts/source refs、消息、完整 manifest、完整 epoch history、80 条 evidence 映射及当前 checkpoint。迁移前后的活动 checkpoint 在 reset 前单独完整比较，防止新 epoch 掩盖迁移丢失。当前代码按旧格式再追加后为 89 条事件。

额外负向实验：在导出后分别注入空 evidence 映射和空 epoch history，两个演练均失败，证明相关断言能检出这两类丢失。

优化前版本 `ddb683634` 读取上述回退产物的独立副本，再次核对完整 session、checkpoint 和 80 条归档消息并继续写入：session 从 87 增至 88 个 turn，journal 从 89 增至 90 条事件；旧写入前的全部记录逐字段保持一致。没有让旧 writer 访问 v2/v3 原始目录。

规范化 SHA-256、计数和旧源码路径见[机器可读证据](2026-10-16-memory-storage-verification-evidence.json)。原始日志、fixture、摘要和旧版本演练脚本保留在上述独立运行目录，`summary.json` 为总入口。随机消息/事件 ID 会使后续重跑的部分 hash 不同；每次运行比较的是该次预期与实际值。

## 复跑

始终选择全新测试目录，不能把已有用户 sessions 目录传给 pytest `--basetemp`，pytest 会清空该目录。

```bash
# 在当前仓库根运行；只创建独立合成 fixture。
VERIFY_ROOT=$(mktemp -d "$PWD/.memory-storage-verification.XXXXXX")
PYTHONPATH=src:tests/fakes venv/bin/python -m pytest \
  tests/unit/scripts/test_verify_session_nas_lock.py \
  tests/integrated/test_checkpoint_process_recovery.py \
  tests/integrated/test_memory_storage_rehearsal.py \
  tests/unit/app/test_session_turn_ledger.py \
  tests/unit/agents/test_checkpoint_journal_v2.py \
  tests/unit/cli/test_memory_storage_cmd.py \
  tests/unit/app/backup/test_memory_storage_backup.py \
  --basetemp="$VERIFY_ROOT/pytest" -q

# 同一新建目录中的两个独立进程；两个命令都必须退出 0。
venv/bin/python scripts/verify_session_nas_lock.py "$VERIFY_ROOT/primitives" \
  --mode indexed --worker-index 0 --revisions 1000 > "$VERIFY_ROOT/worker-0.json" &
VERIFY_PID_0=$!
venv/bin/python scripts/verify_session_nas_lock.py "$VERIFY_ROOT/primitives" \
  --mode indexed --worker-index 1 --revisions 1000 > "$VERIFY_ROOT/worker-1.json" &
VERIFY_PID_1=$!
wait "$VERIFY_PID_0"
wait "$VERIFY_PID_1"
```

本次旧版本复跑脚本保留为运行目录下 `verify_legacy_exports.py`，三个参数依次是 session 导出文件、journal 导出目录、全新输出目录。只复制和处理传入的演练导出：

```bash
git worktree add --detach .worktrees/memory-rollback-reader ddb683634
PYTHONPATH="$PWD/.worktrees/memory-rollback-reader/src:$PWD/tests/fakes" \
  venv/bin/python \
  /Users/shixiangyi/.swe/.storage-verification/20261016-3x1nwph6/verify_legacy_exports.py \
  "$VERIFY_ROOT/pytest/test_session_cli_migration_and0/rollback/s.json" \
  "$VERIFY_ROOT/pytest/test_journal_cli_migration_and0/legacy/16374f34-c02e-4bb9-87bf-eb5e64348cef" \
  "$VERIFY_ROOT/old-version"
```

## 后续环境门禁

取得两 Pod 和生产等价 RWX PVC 后：使用包含本次 verifier 的不可变镜像；为单次运行选择唯一共享测试子目录；确认两个 Pod 位于不同节点并挂载同一个 PVC/StorageClass。修复后的 Job Pod labels 与 required anti-affinity 已匹配，原模板缺少 Pod labels 会使反亲和选择器失效。模板的镜像、PVC、路径仍须按环境替换。

收集两份 JSON、Pod UID/节点名、imageID、PVC/PV/StorageClass、挂载参数与退出码；核对 contender 的正数 read_samples 和正确 final_revision，再把同套恢复/回退用例放到该挂载执行。实际旧 writer 全部退出、跨 Pod 验证与部署授权仍独立于本地结果；不得把本报告中的 same-local-host 标为跨 Pod 通过。
