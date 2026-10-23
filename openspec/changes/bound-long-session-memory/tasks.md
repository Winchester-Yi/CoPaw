## 1. 实施准备与基线

- [x] 1.1 评审完整设计，确认2A/2B分开实施与NAS前提，建立writer/reader/备份/删除工具清单。
- [x] 1.2 对拟改符号运行GitNexus impact并核对UNKNOWN调用方，记录风险；固定基线和专项回归节点。
- [x] 1.3 建立10k/100k events与terminal turns的可重复本地基准，记录Python分配、应用读写量和耗时；生产NAS和RSS尚未测量，不设RSS降幅承诺。

## 2. 2A 提交根与分段追加

- [x] 2.1 先写frame编码、大小/hash/序号、尾部和中间损坏的失败测试，再实现有界frame解析及跨Pod持久化quarantine拒写。
- [x] 2.2 实现state-v2根与不可变元数据引用的持久发布，覆盖checkpoint/manifest/epoch中途进程死亡。
- [x] 2.3 实现同一Chat锁内自动序号、条件追加receipt、short write、未提交尾部恢复和segment封段。
- [x] 2.4 实现旧epoch最终根冻结与提交链，验证reset/append/tombstone并发。

## 3. 2A 消费、迁移与验收

- [x] 3.1 实现head、序号范围迭代与分页；按sidecar定位水位，不加载所有文件名或旧events。
- [x] 3.2 迁移memory.add、projection、candidate和checkpoint前缀验证，确保不丢未应用事实。
- [x] 3.3 归档batch按行读取所需页并保持UUID/行号/cursor；记录manifest/evidence仍存在的增长边界。
- [x] 3.4 实现旧events的dry-run、严格迁移、遗留大事件保留和完整旧格式导出。
- [x] 3.5 通过本地两进程flock并发、故障注入、10k/100k追加基准及相关checkpoint/archive回归；实际两Pod验证归入6.3。

## 4. 2B 冷turn与事务

- [x] 4.1 定义热/冷统一facade及admission顺序，先写状态/正文/Chat绑定/重复msgid的兼容测试。
- [x] 4.2 实现generation、确定hash路径及不可变turn/latest版本与删除tombstone，点查不遍历全部冷正文，latest权威校验及回溯覆盖跨Chat重入。
- [x] 4.3 实现pending intent、先写冷版本再发布session根及取消等待；子进程实际退出覆盖intent、冷文件写完、根已发布三个提交边界，另覆盖失败、取消和损坏intent。
- [x] 4.4 实现此前revision已结算记录的有界移出，保留受保护状态，不按TTL删除、不截断消息。

## 5. 2B 消费与持久化适配

- [x] 5.1 接入admission/terminal/Stop/孤儿恢复，保留直接dict相等语义及最新turn顺序。
- [x] 5.2 接入answer-turn及分享权威终态查询，覆盖failed/stopped/旧cancelled/多个Chat Record。
- [x] 5.3 适配完整ChatHistory合并、归档去重、稳定ID、隐藏上下文脱敏和审批标注，明确完整响应的成本。
- [x] 5.4 使用iterencode原子写session，并复用同请求历史快照；验证AgentScope可变输入边界，不引入跨请求共享memory。
- [x] 5.5 适配备份/恢复/session删除/cron清理/维护脚本，防止缺失ledger或旧generation复活。
- [x] 5.6 实现旧session只读预览、维护进程单并发迁移、完整导出回退，验证启用后新写入也在导出中。

## 6. 审查与灰度门禁

- [x] 6.1 独立审查2A/2B恢复协议和兼容性，全部专项pytest与必要范围pre-commit通过，记录原有失败。
- [x] 6.2 运行detect_changes核对提交范围；更新设计实现状态、Playbook和基准证据。
- [ ] 6.3 确认全部旧writer退出、NAS实际StorageClass校验、回退导出演练后，另经部署授权按租户灰度。

2026-10-16：本地双进程、独立合成数据迁移/回退及优化前`ddb683634`代码读写演练已通过，76项专项测试通过；[验收报告](../../../docs/superpowers/specs/2026-10-16-memory-storage-verification-report.md)记录完整摘要。用户确认当前没有Pod，因此真实两Pod/NAS、旧writer退出与部署灰度门禁仍未完成，6.3继续保持未勾选。

真实历史副本补充：17个session与4份events-only日志共21个样本完成脱敏迁移/导出及旧源码读回追加，380个原文件hash未变；[报告](../../../docs/superpowers/specs/2026-10-16-memory-history-rehearsal-report.md)记录本地耗时、内存和文件数量。样本未触发冷turn或多分段，不能据此认定长会话收益，真实NAS与灰度门禁仍未完成。

本文件记录实际实施与验收状态；不自动部署。生产门禁单列于6.3。


本地验收证据见[基准报告](../../../docs/superpowers/specs/2026-10-15-memory-phase2-benchmark.md)与[设计操作说明](../../../docs/superpowers/specs/2026-10-15-memory-phase2-design.md)。首版完整历史、分享选项、旧archive消息查找和导出仍按输出量增长；只对冷ledger点查询保证不全量hydration。不自动回收持久版本/孤儿generation。
