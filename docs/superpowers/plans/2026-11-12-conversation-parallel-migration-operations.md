# 会话迁移并行参数与恢复操作补充

状态：2026-11-12，代码已实现，完整万级四档验收尚未通过。首次迁移的停写、冻结、DDL、分享收紧、上线凭证和应用启动顺序，仍以 [全流程手册](2026-11-02-conversation-migration-and-launch-runbook.md) 为准。本文补充 direct v3 的并行运行策略，不构成生产放行证明。

## 执行目录与资源

使用审核后的固定发布版本和对应虚拟环境。`AUDIT_DIR` 是持久化审计目录，保存 discovery、manifest、最终 JSON/日志及完成凭证；`WORK_DIR` 是解析 spool 与临时 SQLite 索引使用的可写本地目录。两者都应私有且空间充足，`WORK_DIR` 不得与任何冻结源、snapshot、原运行根或新正文根重叠，也不得通过软链接绕过。临时文件含正文，不能放共享公开目录。

```bash
export AUDIT_DIR=/audit/conversation-20261112
export WORK_DIR=/migration-tmp/conversation-20261112
mkdir -p "$AUDIT_DIR" "$WORK_DIR"
chmod 700 "$AUDIT_DIR" "$WORK_DIR"
```

不要复制示例路径到服务清单作为真实生产路径；两目录都必须是迁移进程可访问的绝对路径。APP_SCHEMA 仍是目标原应用库的实际名称，需与迁移进程加载的 SWE_DB_NAME 和 manifest 一致；worker 参数不改变库或服务归属。

| 参数 | 默认及范围 | 使用方式 |
| --- | --- | --- |
| `--workers` | 4，1–32 | workspace 或校验任务窗口；1 为顺序对照 |
| `--io-workers` | 4，1–32 | 每个活动 IO 池的提交线程上限；import 的 workspace/分享阶段共用同一池 |
| `--memory-budget-mib` | 512，范围 128–65536 | 保守准入预算；不是进程 RSS 硬限制 |
| `--work-dir` | 未指定时私有临时目录 | 建议显式选本地有足够剩余空间的目录 |

以上资源参数可用于 discover/freeze/inventory/plan/inspect/preflight/import/resume/verify/finalize 的 direct v3 路径；旧 offline 工作流不能借此开启并行。单 workspace/Session/正文证据超过 reader 或单 worker 预算会明确拒绝；不允许跳过该项后 ready。worker 更多会缩小每项可用预算，并不总是更快。

容量预检和 `inventoried_source_bytes` 只统计会话输入：`chats.json`、`sessions/`、`dialog/`，以及审核过的 `session_paths` 指定 Session 和对应 generation ledger。附件、代码、技能等业务目录不递归扫描或计入预算；分享正文按分享记录的明确路径读取。文件清单、只读及路径安全检查采用相同包含范围，配置证据检查明确文件及父目录本身。会话目录仍完整枚举，未知 Session、ledger 或归档缺少可信映射时阻断；迁移仍须全程停写和冻结。

旧 manifest 若带非空 `fingerprints`，仍按原全源哈希契约检查，可能读取非会话文件。不得手工清空旧指纹；需要新的枚举边界时，在审核后重新 discovery/freeze 创建新的迁移清单。

## 同一作业的命令

沿用主手册先准备 services、boundary 和 DBA 表结构，执行 discover/freeze。下面假设已有 `$AUDIT_DIR/manifest.json`；不要用并行参数省略停写和冻结。

```bash
python -m swe conversation-storage preflight \
  --manifest "$AUDIT_DIR/manifest.json" \
  --workers 4 --io-workers 4 --memory-budget-mib 512 --work-dir "$WORK_DIR" \
  > "$AUDIT_DIR/preflight.json" 2> "$AUDIT_DIR/preflight.log"

python -m swe conversation-storage import \
  --manifest "$AUDIT_DIR/manifest.json" --apply \
  --workers 4 --io-workers 4 --memory-budget-mib 512 --work-dir "$WORK_DIR" \
  > "$AUDIT_DIR/import.json" 2> "$AUDIT_DIR/import.log"

python -m swe conversation-storage verify \
  --manifest "$AUDIT_DIR/manifest.json" \
  --workers 4 --io-workers 4 --memory-budget-mib 512 --work-dir "$WORK_DIR" \
  > "$AUDIT_DIR/verify.json" 2> "$AUDIT_DIR/verify.log"
```

上述 `python` 必须是发布环境的项目虚拟环境解释器。检查每条退出码为 0 和相应 JSON 校验通过后再继续；Shell 重定向本身不判断业务通过。stdout 保持最终 JSON，stderr 输出周期进度；`completed` 单位是 task，各 pipeline 重新计数，不能当成已完成 workspace 总数。SQL/IO 累计秒数包含等待和重叠，不能简单相加成墙钟耗时。周期进度 JSON 与 failure 报告不记录正文或 SQL 参数；其他依赖库日志仍按发布环境规则保护。

主手册的 DBA 分享 finalize 门禁全部满足并执行后，再运行：

```bash
python -m swe conversation-storage finalize \
  --manifest "$AUDIT_DIR/manifest.json" --apply \
  --output-dir "$AUDIT_DIR/completion" \
  --workers 4 --io-workers 4 --memory-budget-mib 512 --work-dir "$WORK_DIR" \
  > "$AUDIT_DIR/finalize.json" 2> "$AUDIT_DIR/finalize.log"
```

finalize 会重新完整校验，全服务通过才置 ready。按主手册检查每服务凭证、启动及跨实例读写后才能开放入口。

## 中断与续跑

优先 Ctrl+C 一次并等待进程真正退出；IO 取消会等待已提交的真实线程完成，不把 task.cancel 当作 NAS 写入终止。禁止旧进程尚存或 IO 未排空就启动第二个迁移进程。命名锁不能充当跨进程 NAS fence。

失败时保留 manifest、数据库控制表、正文和日志。CLI 会尝试原子写出邻接文件 `manifest.json.failure.json`，其中包含异常类型、阶段和 IO 排空状态，并尽力记录首个失败位置；无法定位时 failure_position 可能为空；`io_drained=unknown` 不能当安全退出。报告无法落盘时以退出码和日志为准，不能因此忽略失败。

若为数据库断链或 `UnknownCommit`，先确认旧进程退出与 IO 排空、库/NAS可达、源冻结仍有效；不能手工把 prepared 改 committed，也不能删票据、换 operation key 或重建 manifest。使用同一作业：

```bash
python -m swe conversation-storage resume \
  --manifest "$AUDIT_DIR/manifest.json" --apply \
  --workers 1 --io-workers 1 --memory-budget-mib 1024 --work-dir "$WORK_DIR" \
  > "$AUDIT_DIR/resume-01.json" 2> "$AUDIT_DIR/resume-01.log"
```

该例以顺序方式诊断，预算/worker 可调整，不属于 source/target 绑定。后续再次 verify、分享 DDL 门禁和 finalize；ready 后禁止再 import/resume。源已变更时停止，不能通过调并发或修改 blocker 绕过。

## 性能与证据边界

本轮用户指定本地目录验收，不能据此声称真实 NAS 的吞吐、fsync 或故障行为已验证。当前完整万级测试曾因数据库超时失败；1/4/8/16 的最新代码完整结果及可控 IO 延迟对照仍待收齐。默认 4 是保守策略，不是实测最优值。

原有 SQL 由唯一持锁 task 串行执行。远端数据库的网络往返可能主导耗时，增加 worker 不会消除这部分。上线前根据源规模、SQL 延迟、NAS 和临时盘测量选择参数；详见 [实现与验证记录](../reports/2026-11-12-conversation-parallel-implementation-validation.md) 及其列出的失败记录。
