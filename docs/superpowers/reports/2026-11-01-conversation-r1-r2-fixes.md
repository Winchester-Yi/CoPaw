# R1 多 scope 恢复与 R2 长期 Session 引用规模修复

日期：2026-11-01。基线：`858fc4d65`。本次仅处理全局审计的 R1/P1 与 R2/P2。
沿用原业务 workspace/NAS 契约；测试使用临时文件和随机生成的隔离 MySQL schema，
未迁移、改写原业务数据，未部署、合并或推送。

代码提交：`a11132742`（R2 分批 pin、依赖安装及取消清理），`964dca0ac`（R1 多 scope
聚合恢复）。两个提交均通过实际提交钩子，未跳过检查。

## R1：同一 schema 的多 scope 完整恢复

原实现按单 scope 捕获，却要求恢复目标七表整体为空；第一份恢复后无法恢复第二份。
修复保留空目标保护，将 `restore_backup` 扩展为接受单一路径或完整路径列表。

`backup_set.load_restore_set` 在创建目标目录/写入 SQL 前加载和校验全部备份，要求
同一已识别源 schema、scope 无重复。保留全部原始 ID；同 tenant 的 legacy_share
publication、分享行及正文必须一致并去重。不同 scope 的非共享 ID 冲突、单份备份重复
ID、共享内容漂移均拒绝。共享 reader/backup_fence 在捕获间的 active→released 差异
仅对停止源 owner 的状态结算放行，静态字段仍严格比较。

合并后在同一事务中安装七表，完整比对元数据、校验 NAS 正文，再分别运行每个 scope
的 Session/Chat/Turn/分享 reader。多 scope 报告列出全部备份与 scope，计数按合并结果。
单 scope 调用和验证报告字段保持兼容。

该接口不会自行发现未提供的 scope。操作者须在全组捕获前停止全部相关 writer、重试及
GC，并提供完整清单与共同停止边界；不同时间捕获且共享业务内容不一致的备份不自动合并。

## R2：分批 pin 与完整依赖提交

已先复现：两个历史读取入口 4096 个不同 publication 可用，4097 个均抛出
`publication pins require bounded positive integer IDs`。

`ReaderPins` 对去重、排序后的引用集按 `MAX_PIN_BATCH` 分批，所有 pin 与 locator
读取仍在同一外层事务快照内。NAS IO 由批次追踪；异常、取消、后续批次 pin 失败时，
清理栈完成所有已登记批次的 drain/release。离线只读 retention 同样兼容。

独立审查继续发现提交阶段 `add_dependencies` 将目标引用与当前 publication 一起锁定，
所以 4096 个目标也会超限。回归先确认 4095 通过、4096/4097 失败；随后改为验证完整
目标集，并在同一事务中按升序分批锁定目标和 source。依赖行仍一次事务安装并校验完整
成员集合，没有提高单批上限或裁剪历史。

compaction 可同时持有归档来源 pins 和恢复引用 pins。额外回归确认：若前者清理抛出
取消或释放错误，原 `finally` 会跳过后者。已加入嵌套 `try/finally`，保证仍尝试释放全部
恢复引用批次；两种失败注入均先复现泄漏，再验证修复后 4097 个 pins 全部释放。

## 验证

- 单元回归：会话存储、迁移 CLI、切换演练脚本共 **540 passed**，28.59 秒。
  XML：`/private/tmp/conversation-r1-r2-unit-final.xml`。
- R1 真实 MySQL：**1 passed**，87.82 秒，
  `/private/tmp/conversation-r1-multi-scope-mysql.xml`。同 tenant 两 workspace + 跨 tenant
  的三个 scope 一起恢复至同一空 schema/NAS，保留未知 Session 模块、孤立分享未知字段
  和计数。先注入重复共享记录的计数漂移，确认全部七表及目标 NAS 均未写入，再恢复有效
  备份并完成整体恢复。
- R2 单元测试覆盖真实 RetentionStore 与数据库替身上的 4096/4097 边界、无请求缓存的
  重复读取、4095/4096/4097 依赖安装、第二批 pin 失败、正文失败及取消。外部 task.cancel
  测试阻塞 NAS worker，确认全部 4097 pins 在 IO 结束前保持 active，之后全部释放。
- R1 单元测试另覆盖 scope/schema/ID 冲突、共享 fence 结算、共享正文逐字节差异、
  单份重复 ID，以及第二个 scope 安装失败时整组 SQL 回滚。
- MySQL 发布/恢复/回滚回归 **16 passed**，613.61 秒，
  `/private/tmp/conversation-r1-r2-mysql-regression.xml`。包括完整 task history 发布与
  新 adapter 读取、已有 projection 场景及全部 rollback 文件。规模用例将单批上限降为
  2，以少量真实 SQL/NAS 数据跨越多批；真实 SQL 测试没有构造 4097 份正文。
  该套件开始于最终清理修复前；清理增量之后另有上述 540 单元回归及专门
  **2 passed**，`/private/tmp/conversation-r2-cleanup-green.xml`。
  最终清理代码后的真实 SQL 跨批发布/读取再次 **1 passed**，27.96 秒，
  `/private/tmp/conversation-r2-final-mysql.xml`；与前述 16 项有重叠，不相加为独立覆盖数。
- 本次 11 个代码/测试文件的 pre-commit（含 mypy、black、flake8、pylint）全部通过；
  不代表全仓静态检查通过。文档检查与 `git diff --check` 通过。

早期失败包括测试夹具重复执行非幂等分享 schema 扩展、复用全局唯一 runtime Turn key、
正文比较测试未创建临时 NAS 根，以及格式/静态检查提出的问题，均修正后重跑。
这些不作为生产缺陷的 RED 证据。R1 多路径入口和 R2 边界有修复前的独立失败记录。

R1/R2 已经分别由独立审查代理检查；R2 审查发现的依赖安装遗漏已纳入修复与回归。
全局审计的 R3 CLI 导出旧源依赖不在本次修复范围。真实 K8s/NAS、固定旧生产镜像、
全量历史映射和生产规模预算等外部验收门槛仍然存在；本报告不宣称生产验收完成。

## 提交后独立检视

按用户要求，对 `858fc4d65..ae772f02c` 再启动两个独立只读子代理检视，分别负责 R1
与 R2。两者均未发现本次改动引入的可确认缺陷，无阻断项；没有修改文件或连接业务数据库。

- R1：独立运行 `test_backup.py` 与 `test_backup_set.py`，**52 passed**，0.45 秒。
  核实共享数据/ID 冲突拒绝、七表同事务安装及逐 scope reader 核验。提交后核验失败
  保留隔离 staging 现场，不返回成功；该原有失败处理不等于部分 scope 提交。
- R2：独立运行 `test_projection_scale.py` 与 `test_retention.py`，**37 passed**，
  27.07 秒。另以临时单元数据库替身确认跨三批依赖中途插入失败整组回滚、相同集合重试
  不重复，以及离线跨三批读取零持久化 pin。以上计数与前述回归有重叠，不汇总相加。

R2 真实 MySQL 测试目前覆盖跨批成功提交和新 adapter 读取；跨批依赖中途失败、重复提交
及 GC 竞争尚无新增真实 MySQL 专项，代码检视未发现对应确定缺陷。
“释放全部批次”限定为已经返回并登记的批次；pin 事务提交应答不确定、尚未返回 PinBatch
的原有协议边界不在本次修复范围。
