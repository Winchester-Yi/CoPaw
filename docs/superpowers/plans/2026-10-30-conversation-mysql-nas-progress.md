# 会话迁移实施记录

更新：2026-11-01，Asia/Shanghai。实施工作树：

`.worktrees/conversation-mysql-nas`，分支
`feature/conversation-mysql-nas-migration`。

## 已验证增量

| 范围 | 提交 | 本次证据 |
| --- | --- | --- |
| 最终设计与十五任务实施计划 | dc3332d2e | 文档差异检查与提交钩子通过 |
| T1 基础 fixture 与真实旧 Session/archive reader 契约 | fd880c6b8 | 旧 API 契约基线 79 passed；新增旧 reader 5 passed |
| T2 精确 UTF-8 身份与 immutable bundle | 3bf159b8b | 独占发布、fsync 故障、路径/symlink、范围与流式读取；本地测试通过；生产 NAS 原语未验收 |
| T3 专用隔离 schema、正式 DDL/索引/分享扩展 | 556a6cde5 | 真实 MySQL 8.0.46，page size 16384，strict mode，UTC；8 passed，无 skip；151 列和分享扩展均验证 |
| T4 publication 基础与 unknown 对账 | e35ff1925 | 真实 MySQL 6 passed；预留/最终 COMMIT 响应丢失、安装回滚、unique-key 竞争、writer/abort 竞争 |
| T5 reader/dependency pin 基础 | 11a0861fa | 17 单元通过；真实 MySQL 4 passed；取消时排空 NAS worker、released key 不复用、guard 形状验证 |
| T6 SQL Chat 与原子完整 metadata | b20722f84 | 独立复审通过；真实 MySQL 14 passed；最后删除读取竞态新增回归；综合单元与旧分页/专家契约 155 passed；提交钩子全通过 |
| T7 索引历史与归档查询增量 | 209de073a | 已提交；完整入口验收仍按 T10/T14 记录 |
| T11 稳定清单与多格式导入核验 | 32044ca56、eab7f222f | 已提交 inventory/import/verify；后续离线零写入修复仍待提交 |
| T8/T9/T10 执行、归档及分享发布基础 | fe7d0b1db | 已提交；生产运行时装配尚待独立提交与全入口验收 |
| NAS 根缺失不冒充已回收 | 33cc6e2f6 | 失败回归确认后修复；bundle 专项与提交钩子通过 |

上述 T4/T5 仅是基础增量，完整 Message/Turn/archive 发布和历史授权 snapshot 等仍待后续接入；不把基础组件通过当作任务全部验收。

## 本次收敛与证据

运行时装配复用原 workspace 与共享 NAS；已安装映射必须匹配可信 tenant/source/agent。
正常新 provision 的空 workspace 可以按已解析身份接入，遗漏的旧会话数据必须先迁移；
不从路径反推身份。单用户默认入口仅复用清单明确记录的 default 身份。

独立检视发现并修复：终帧早于 terminal commit、preflight 异常无终态、外层协调器覆盖
已提交终态、SQL 审批拒绝清理调用文件私有接口、分享误用编码 runtime tenant、scope
跨租户错绑、静态清单阻止正常新建、只按列数接受错误 schema。readiness 现在检查
精确字段/可空性/默认值/自增、索引及 InnoDB base table；过程不执行 DDL。

2026-11-01 本次实际结果（不相加去重）：

- 新组件与旧 API、Session/AnswerTurn 专项：564 passed，0 skipped，4.151 秒；
  `/private/tmp/conversation-runtime-regression.xml`。
- 增量、CLI、备份边界与协调器专项：462 passed，0 skipped，1.434 秒；
  `/private/tmp/conversation-increment-regression.xml`。
- 真实 MySQL readiness/已关闭 execution 的未知终态确认：3 passed，0 skipped，17.955 秒；
  `/private/tmp/conversation-runtime-mysql-report.xml`；生成隔离库已清理。
- T12 既有真实回退集：7 passed（366.919 秒），另 prepared 收敛/恢复验证 2 passed
  （60.532 秒）。XML 已核对；随后发现 GC 后备份不兼容，修复及回归尚在进行。
- T15 既有真实 GC：26 passed，后补并发专项 10 passed；最终收集 29 cases，不能把
  两次数量直接相加。独立审查确认默认 report-only，并发现 GC→backup 交叉缺口。

T12/T13/T15 未提交代码的专项通过不等于生产门槛通过。最终功能报告与测试报告仍待
全目标完成后输出；本文件只是可续接的实施记录。

## 环境与边界

- 数据库连接读取原工作区 `src/swe/config/envs/dev.json`，仅用于生成的一次性隔离 schema；
  不输出凭证、不提交用户配置、不修改业务表。每次测试生成的控制库/子库在结束清理。
- 用户明确复用原有 workspace/NAS 挂载；无需初始化新 workspace。新 bundle 使用受控新前缀。
- 运行时 SQL authority 为显式 opt-in，默认仍是 legacy；未修改生产配置、执行生产停机、
  业务七表安装或部署。
- 真实 NAS、跨节点双 Pod、全量生产规模迁移、完整恢复/回退和停机预算仍未验收。

## 当前开放项

- T10 生产装配最终检视与小步提交；T14 全入口与故障矩阵，尤其 unknown/prepared
  操作的完整运维结算及进程恢复，不能靠重跑模型或工具解除。
- T12/T15 有效引用语义统一：合法回收隐藏/删除 locator 后仍须能备份/恢复与反导；
  新的端到端回归正在实施。默认不启用物理 GC。
- T13 离线 verify 必须证明 SQL/manifest 零写入，包括嵌套 projection 和分享 reader；
  CLI/安装器需完成独立审查、真实隔离 MySQL 重跑和小步提交。
- T14 固定旧镜像恢复继续写、目标 NAS 原语、跨节点双 Pod、全量规模及停机预算仍缺
  实际验收证据。本机 context 为 orbstack；已询问目标隔离环境参数。
- 逐项核对十五任务复选项、Console 兼容回归及最终全范围检查；不能用专项单测通过
  代替未运行的环境门槛。

本记录不替代主计划的逐项验收；最终功能报告和测试报告在全部要求有证据后交付。
