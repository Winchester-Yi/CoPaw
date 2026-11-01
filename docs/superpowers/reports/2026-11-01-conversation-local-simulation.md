# 会话迁移本地模拟验收

2026-11-01，Asia/Shanghai。用户确认当前没有可用 Kubernetes/NAS，授权以
`/Users/shixiangyi/.swe` 的现有数据进行本地模拟。继续复用已有业务 workspace；
本次只读复制其历史样本到隔离临时目录，不初始化或切换原业务 workspace。

## 样本与源数据边界

- 原始只读入口：`/Users/shixiangyi/.swe/default_RMASSIST/workspaces/default`。
- 隔离演练目录：`/private/tmp/conversation-local-rehearsal-fd98e5ebf7ad4fcbbffef63436cb578f`。
- scope 是本次明确指定的 `default / RMASSIST / default`，不能据此声称覆盖其他
  workspace 或生产完整 scope 清单。
- 原 workspace 全扫描：205 文件、4,140,461 字节、61 Chat、46 个关联 Session。
  inventory 正确阻断：6 个缺少源文件的 Session、107 个未提供可信身份映射的
  Session 文件。未删除、补造或猜测这些历史数据。全扫描 `ready=false`。
- 后续往返仅选取可完整解析的 40 Session、55 Chat、44 文件、1,283,360 字节
  子集，子集 inventory `ready=true`；该子集不是全量验收。
  冻结副本权限设为只读。原目录复制前后逐文件 SHA-256 一致；往返结束再复核。
- 没有固定旧生产镜像。临时 source boundary 使用明确标记
  `local-simulation-unavailable-image` 的占位 digest，仅满足本地组件数据结构，
  不构成镜像来源证明、生产迁移 manifest 或回退就绪证据。

完整 inventory/source hash 包含本机历史内容，只保存在上述私有临时目录；
不提交源正文、凭证、个人聊天内容或原始身份清单。

## 本地文件系统故障与竞争

`/private/tmp/run_conversation_local_nas.py` 调用正式 `BundleStore` 与现有 rehearsal
原语，在 macOS 本地文件系统完成以下检查：

- 文件与父目录 fsync、独占发布；
- 注入文件 fsync/目录 fsync 失败；
- 截断、丢失及注入不可读正文；
- 两个独立进程同时发布同 operation key，20 轮均恰有一个发布成功，另一个
  独占冲突；两进程均读到相同完整胜者正文，所有进程退出码为 0。

机器证据：`/private/tmp/conversation-local-rehearsal-ecf7d8a767e248deb4f530ef7c7e11fb/local-nas-fault-report.json`，
耗时 0.115 秒。该故障演练目录与下述成功历史往返目录不同。
`kubernetes_tested=false`、`two_nodes_tested=false`、`network_nas_tested=false`；
结果只证明本机文件系统行为，不能关闭真实 NAS 跨节点和 Pod death 门槛。

## 本地验收结果

- 本地契约回归：`../../venv/bin/python -m pytest
  tests/unit/scripts/test_rehearse_conversation_storage.py
  tests/unit/app/conversation_storage/test_factory.py
  tests/unit/cli/test_conversation_storage_cmd.py -q
  --junitxml=/private/tmp/conversation-local-simulation-contracts.xml`，
  68 passed，0.67 秒。第一次命令的 factory 路径遗漏 `app/`，退出 4、
  没有执行测试；修正后的结果才计入通过数。
- 新鲜 manifest 的 40 Session / 55 Chat 子集导入 513.26 秒，同 manifest 幂等重入
  241.36 秒；计数没有变化。逐字段 verify 检查 139 项，无 blocker，用时 374.75 秒。
  机器结果在 `import-result.json`、`verify-result.json` 与 `local-roundtrip-summary.json`。
- 一致备份并在空隔离 schema 完整恢复通过，用时 285.14 秒；反向导出通过，用时
  0.45 秒。当前代码树 `SafeJSONSession` 读回全部选定 Session，继续写并重开验证通过。
  该次历史往返尚无增量写入，反向导出的 `rollback_ready=false`，固定旧生产镜像不可用。
- 本地批次基准结果：`/private/tmp/conversation-semantic-benchmark.json`。small/default/large
  参数下 publication 数分别为 18/5/2，保留 bundle 逻辑字节分别为
  2,516,204 / 919,109 / 394,267。DB lock wait 为 null，指标基于一次顺序 state-only
  本地实验，不据此调生产默认值。
- T14.3 隔离 app 验收：真实 FastAPI lifespan、Auth/Tenant middleware 和随机 SQL 子 schema；
  非 loopback 坏 token 返回 401，合法 token 对既有 workspace 返回 SQL 中预置的 chat。
  1 passed，9.35 秒，XML 为 `/private/tmp/conversation-app-bootstrap-report.xml`。
  workspace 与 secret 只读复制到 tmp；NAS 使用本地临时目录，目标 NAS、Kubernetes
  和外部 tracing 不在该证据内。
- Primary memory/task/error 层按 runtime timestamp 稳定排序后导入，verify 使用同一顺序；
  anchor 仍保留既有的单独顺序规则。新增交错错误与 answer-turn MySQL 用例 13 passed，
  单测 15 passed；XML 分别为 `/private/tmp/conversation-history-display-order-mysql.xml`
  和 `/private/tmp/conversation-migration-import-final.xml`。

上述单 workspace 子集已通过，不代表全量 scope。完整扫描仍因缺失数据和身份映射
`ready=false`，未跳过 blocker 或猜测身份。

本次本地往返报告 `passed=true`、`original_unchanged=true`，数据库隔离 schema 已清理。
原 workspace 的 regular file 清单为 205 个文件、4,140,461 字节；前后 SHA-256 相同。
结果以最终机器报告和对应测试输出为准。未测量的数据保持缺失/null，
不将本地模拟写成生产连续停机、全量历史、目标 NAS 或固定旧镜像验收。

## 本地演练发现与修复记录

首次历史 verify 位于
`/private/tmp/conversation-local-rehearsal-ecf7d8a767e248deb4f530ef7c7e11fb/verify-result.json`，
`passed=false`，有两项 `runtime answer Turn source/fragment semantics differ` blocker。
根因是无 turn_states 的旧 Session 按 memory/error 层顺序导入，而 runtime 按
timestamp 稳定显示。修复 `0be85c33f` 后使用新 manifest 完整重跑，成功证据是
前述 `fd98e5...` 目录的 139 项 verify；没有覆盖或删除首次失败证据。

真实历史增量首轮在 terminal commit 后因无标签断言失败；诊断重跑目录
`/private/tmp/conversation-local-increment-bb6e5b43fc35471aa01e15479caaf906` 显示，
fresh Session/current history 同时含问题与回答，但 answer_turn 只有问题，
Turn 状态已经是 completed。两次失败运行均清理生成 schema，原 workspace
205 个文件、4,140,461 字节的逐文件 SHA-256 保持一致。

根因是 admission anchor 之后，同一问题的 recovery memory occurrence 被 SQL
当成下一条 user 边界，回答被区间裁掉。最小修复仅在选中 question occurrence
关联到持久 Turn 时按 scoped/visible Turn 取消息；legacy 未关联路径保留原区间，
Chat 所属检查、archive-anchor suppression 和读取后重验保持原逻辑。

回归经历保留如下：初版简化 Msg 字典在旧实现上复现缺答复，首次 green
尝试因夹具缺少 `name` 报错，不能计为通过；改为真实 AgentScope `Msg.to_dict()`
后撤回修复重新取得有效 RED（1 failed，16.47 秒），再应用修复取得 GREEN
（1 passed，14.96 秒）。随后增加同 Chat/另一 Chat 的后续 completed Turn
参数，最终三个参数均通过（3 passed，65.55 秒），严格断言原切片只含
question/answer。RED/XML 为 `/private/tmp/conversation-answer-turn-red.xml`，
最终三参数 GREEN/XML 为 `/private/tmp/conversation-answer-turn-green.xml`。
初次夹具报错的 green XML 路径随后被新运行复用；该错误仅按运行记录描述，
不将现有 green XML 说成仍含旧错误。

回答相关单元契约 34 passed，3.02 秒，XML 为
`/private/tmp/conversation-answer-turn-unit.xml`。限定 history.py 与 execution
集成测试的显式 pre-commit 全通过；首次 Black 自动排版后，第二次检查通过。
独立审查读取最终两文件实际 diff 和三参数 XML，未发现阻断项。
当前修复源码的 execution、history、session projection、API 四模块真实 MySQL
回归 78 passed、0 failed、0 skipped，766.13 秒；XML 为
`/private/tmp/conversation-typed-turn-regression.xml`，生成 control schema 已清理。
该结果不是整个 MySQL 集成目录的再次运行。
修复已提交为 `74057c17b`，实际提交钩子全部通过；提交前独立新鲜复跑三参数
3 passed（62.38 秒）与回答单元 34 passed（2.93 秒），XML 分别为
`/private/tmp/conversation-answer-turn-green-fresh.xml` 和
`/private/tmp/conversation-answer-turn-unit-fresh.xml`。

## 修复后的真实历史增量往返

最终运行目录：
`/private/tmp/conversation-local-increment-89634288187d4759a5f7b6c7c9d6c224`。
命令为 `PYTHONPATH=src ../../venv/bin/python /private/tmp/run_conversation_local_increment.py`，
进程退出 0，`increment-report.json` 的 `passed=true`、
`generated_schemas_removed=true`、`cleanup_errors=[]`。

1. 将前述历史子集的一致备份恢复到空隔离 schema，237.16 秒。
2. 选择恢复后的现有 idle console Session/Chat，正式 reserve admission、
   publish admission、start、finish，分别用时 0.83/4.63/0.52/5.94 秒。
   增量写入含独立 question/answer marker 和未知顶层 recovery 模块。
3. fresh Session、current history、answer_turn 同时读到新问答；未知模块保留，
   SQL Turn 为 succeeded/completed。首轮 reader 校验 4.35 秒。
4. 捕获增量一致备份并恢复第二个空隔离 schema，274.38 秒；第二次 fresh readers
   再次验证新问答、未知模块及终态，`increment_backup_ready_local=true`。
5. 反向导出后，当前树 `SafeJSONSession` 读到新问答/终态/未知模块，继续写并
   重开通过。固定旧镜像仍不可用，`image_verified=false`、`rollback_ready=false`。

原业务 workspace regular files 仍是 205 个、4,140,461 字节，前后逐文件
SHA-256 无新增、缺失或变化。两个生成 schema 均删除，未初始化业务 workspace、
未写业务 schema。源完整性证据仅覆盖选定原 workspace 的 regular file 路径和正文，
不包含其他 workspace、symlink 元数据或生产全量身份清单。

## 本地连续阶段演练

证据目录：`/private/tmp/conversation-local-stages-20261101-r2`；临时 driver 为
`/private/tmp/conversation_local_stages.py`。driver 从上述冻结可解析子集中选取
一个真实 Session/Chat，只复制到临时 working 目录；调用正式
`scripts/rehearse_conversation_storage.py rehearse --apply` 执行十阶段。
数据库只创建随机隔离应用 schema 和对应 staging schema，NAS 使用本机临时目录。

`ledger/report.json` 的 `scope=isolated-copy`、`status=passed`，十阶段退出码
均为 0，总计 98.1515 秒。每阶段另有 `*-audit.json`，记录 operator、UTC
起止时间、argv 和退出码。阶段实测值如下，不代表生产停机预算：

| 阶段 | 秒数 | 实际操作 |
| --- | --- | --- |
| drain | 2.575 | 独立旧 writer 在真实 `SafeJSONSession.execution` 中写 running Turn 并持锁；等待 0.15 秒超时，随后发停止信号、提交 stopped、等待进程退出 0；安装未开始 |
| stop_writers | 1.572 | 重开临时旧 Session，核实 stopped 和 writer 退出证据 |
| backup | 1.562 | 冻结只读副本并逐文件比较哈希，生成明确标记本地未验证镜像的 manifest |
| import | 33.251 | 正式 `OfflineJob` 导入 |
| verify | 9.533 | 正式逐字段核验通过 |
| install | 17.325 | 正式隔离七表安装，保留旧永久 token/count=17 和无关业务表 id=91 |
| startup | 3.114 | 原 workspace adapter 的 schema readiness 和既有 Chat 读取；未启动完整 FastAPI 进程 |
| smoke | 18.937 | 正式 admission/start/finish、新问答、未知模块、解绑和新分享 |
| restore_traffic | 2.598 | 本地读取新 share，SQL 访问计数变为 2；未操作生产入口 |
| rollback | 7.683 | 历史及新增两个 scope 分别一致备份/反向导出，当前树 legacy reader 读取并继续写；不执行逆向 RENAME |

历史 scope 为 `default/RMASSIST/default`，新写 helper scope 为
`tenant/source/agent`，两个 scope 均有独立 backup/export 结果。
`rollback-result.json` 的 `passed=true`，但 `image_verified=false`、
`fixed_old_image_verified=false`、`rollback_ready=false`；当前树 legacy reader
不能替代固定旧生产镜像。峰值 RSS 197,525,504 字节是 wait4 的单进程
high-water 值，不是并发进程树内存之和。DB lock wait、p95/p99、NAS bytes/files
均为 null，未用 0 补齐。

`source-before.json`/`source-after.json` 确认原业务目录仍为 205 个 regular files、
4,140,461 字节，逐文件 SHA-256 不变；`cleanup.json` 的
`generated_schemas_removed=true`，该标记在两条 DROP 成功返回后写出，未保存独立
清理后查询。drain 的超时由 `Popen.wait(0.15)` 观察，随后由 driver 发 stop 信号；
该阶段最终 exit 0 继续执行，不是生产 drain API 或失败阶段中止 ledger。
本地独立 writer 超时/停止与完整 operator ledger 已补证；目标入口、队列和
全部 Pod writer 的真实排空仍不在本次证据内。

首次目录 `/private/tmp/conversation-local-stages-20261101` 的 import 被
OfflineJob schema 一致性保护拒绝：包装器尚未把生成 schema 同步到进程环境。
该次已清理生成库；r2 显式同步生成 schema 后完整重跑通过。首次失败记录保留，
不算生产实现的有效 RED。

本轮故障测试已提交 `aa846b947`；修改的 install/rollback/legacy-roundtrip
三个集成文件完整复跑为 74 passed、0 failures/errors/skips，1376.02 秒，
XML `/private/tmp/conversation-cutover-final-three-files.xml`，包装器退出 0、
生成 control schema 清理。当前已提交基线全目录另为 274 passed/1 skipped，
收集早于新增参数，二者不累计；精确范围见配套测试报告。
