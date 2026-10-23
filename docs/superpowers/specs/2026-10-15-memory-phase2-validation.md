# 第二阶段本地验收记录

日期：2026-10-15。代码已同步到主工作区，尚未提交；未执行生产数据迁移或部署。

## 功能与故障验证

项目虚拟环境运行的联合专项回归 **458 passed（17.06s，主工作区）**。范围包括checkpoint journal/record/attachment/compaction/archive、request memory lease、session transaction/cancellation/ledger、Stop与恢复、answer-turn、分享、cron、备份及维护CLI。测试环境使用`PYTHONPATH=src:tests/fakes`，其中trace_sdk为仓库已有测试替身；不宣称全仓测试通过。

新增冷存储故障测试覆盖：根发布前写失败、根已发布收尾失败、取消恢复期间继续持锁、损坏根禁止覆盖、未知schema拒写、短写/完整dict替换保留generation/revision、缺正文/缺reference/latest、损坏tombstone校验、损坏intent拒绝删除旧文件、UTF-8预算及修改记录保护、只读预览不恢复pending。

实际子进程`os._exit(91)`覆盖intent落盘后、冷文件写完而根未发布、根已发布而intent未清理三个边界；重启按根只读到完整旧/新快照。两个独立session写进程各提交8次，共16次，计数及revision连续。checkpoint另有两个独立进程flock并发16条连续序号，以及条件重试hash、分段/水位、尾部恢复、持久隔离、迁移严格校验和原子导出测试。

answer-turn和分享创建测试将全量cold materialize设置为直接报错，验证其只点查所选ID；完整历史与分享选项维持全量语义。旧archive消息扫描仍按原分页流程执行。

## 静态与范围检查

本次任务文件的pre-commit全通过：AST、YAML/JSON、编码、密钥检测、空白、trailing-comma、mypy、Black、flake8、pylint。两个备份脚本`bash -n`通过；OpenSpec strict validate通过。既有取消/线程隔离测试已接到真实流式写入入口，保留原有行为断言。

GitNexus detect_changes报告HIGH，主要涉及answer-turn及其scope/Chat查询链；此前_build_chat_history impact已提示4个直接调用及恢复/分享路径。源代码逐项核对、联合回归覆盖这些消费者；新增未索引文件另行人工复核，未将UNKNOWN解释为零风险。主工作区原有其他文档变更不属于本任务，没有纳入任务文件清单。

## 基准与启用门禁

可重复脚本与1万/10万条样本结果见[基准报告](2026-10-15-memory-phase2-benchmark.md)。本地10万条冷turn点查Python新增分配峰值约96KiB，读取约28KiB；checkpoint追加不读取旧sealed段。无旧实现对照，不报告收益百分比；无生产NAS或进程RSS结论。

两个新格式开关默认空，已有对象不自动迁移；完整历史、单条大记录、全部未结算turn及元数据仍可能增长。启用前必须验证生产StorageClass的跨Pod flock、锁持有Pod退出、rename/fsync及导出硬链接能力，完成备份恢复和含新写入的旧格式导出演练。详见[设计与操作说明](2026-10-15-memory-phase2-design.md)。


独立质量复审最后发现并修复恢复拒绝触发全量rollback的问题：新增专用无写入拒绝异常，恢复任务只回滚已实际修改的租户。两场景回归覆盖首租户拒绝与前序租户已恢复；复审重跑确认目标目录未删除、原文件inode不变，无修改时任务FAILED。备份及CLI专项47 passed，独立复审未再发现明确重大问题。
