# Checkpoint read contracts and readable plans Implementation Plan

> Use superpowers:subagent-driven-development for the independent plan validation task and final spec/quality reviews; integrate checkpoint work in the current worktree. User has authorized implementation of the previously approved design. Do not commit unrelated files or read/modify dev.json.

**Goal:** 消除 checkpoint 固定窗口截断，明确 record/prefix/page/full，并在持久化前拒绝读不回的 plan。
**Architecture:** 保持 schema/format，ArchiveReader 的 checkpoint 读取拆入独立模块，以固定 epoch/revision/active publication/high 的游标逐页读取。每页使用 ReaderPins、短 SQL 事务选取并 pin，事务外 NAS，IO 排空后释放；现有 checkpoint/read_checkpoint_state 完整成功或明确失败。Migration/restore 必须完整消费，不能以 prefix 验收。
**Tech Stack:** Python、pytest、现有 MySQL/BundleStore/RetentionStore。

## 任务 1：通用 plan 最终字节准入

Files: commit_plan.py、tests/unit/app/conversation_storage/test_commit_plan_capacity.py（新增），必要时 publications.py（仅在已证明调用顺序不安全时）。

- [x] 先写失败测试：实际 1 MiB−1/等于/+1，实际 JSON/转义及 Unicode 字节，合法条数但过大 plan；失败必须来自缺少最终容量校验。
- [x] serialize 在 json.dumps 后执行与 parse 相同的 UTF-8 最终字节检查，超限 ValueError；parse 保留 IntegrityError，不在正常写入额外重复 parse。
- [x] 通过真实 PublicationStore.reserve 验证过大 plan 在 transaction/reserve/NAS 之前拒绝。检查投影有效 plan 的序列化入口，记录是否仍有后续独立容量问题，不扩展 compact 设计。
- [x] 专项及相关 publication/projection 测试，自审与独立复核；不提交代码。

## 任务 2：checkpoint 核心读取

Files: 新增 checkpoint_reader.py；修改 archive_reader.py；新增 tests/unit/app/conversation_storage/test_checkpoint_reader.py。

- [x] 真实 4095/4096/4097/8193/16385、独立/共享 active、publication 内多事件边界先写失败测试。保持 MAX_PIN_BATCH=4096 不变。
- [x] record 只读取 active 正文与 SQL metadata；pin active，验证 record watermark/revision/epoch/身份，绝不查询事件。
- [x] prefix 返回明确 has_more；page 游标固定 Chat/Session/epoch/revision/active/high，按 event_last,id 利用现有索引推进；完整验证读取的 event extent，页内返回数量有界。
- [x] full 用分页机制覆盖 captured high；显式事件/字节累计预算，超预算明确失败；旧 checkpoint 默认变为完整读取，不再静默 prefix。
- [x] 捕获 high 后 append 不进入结果；reset/active 替换/删除/缺页/坏尾部明确失败。SQL 选择和 pin 同事务，失败及重复取消后排空 worker 才释放引用。

## 任务 3：运行时及完整验证调用方

Files: archive_runtime.py、archive_binding.py、migration/verify.py、backup.py；相关 unit/integrated tests。

- [x] facade record 调用改为 record-only；保留 read_checkpoint_state 完整契约；新增 page/完整遍历入口。
- [x] 摘要投影完整消费但仅保留最后 5 个事件；degraded 只需 record；compact 仅取已知源消息数量的 prefix，避免无意义全历史读取，固定元数据一致性仍检查。
- [x] migration 逐页与源尾部比较并验证数量；restore 必须消费到完整正常结束。离线只读适配器仍仅用于停止 writer/GC 的明确边界。
- [x] 可选真实 MySQL 测试包含实际边界、并发及 GC；无专用库时明确 skipped，不读取开发配置来寻找数据库。

## 任务 4：验证与交付

- [x] pytest 专项及 conversation_storage/相关 agent 回归；明确观察 red→green。
- [x] 新增 MySQL 集成验收及环境可用性记录；无专用库不能宣称 MySQL 通过。
- [x] 对改动文件运行 pre-commit，独立 spec/质量 review；修正实际问题后复验。
- [x] 更新设计/实现报告及入口索引，说明预算、成本、兼容和环境未验收项；本轮用户仅要求处理，不自动提交。

实现完成。full 数组默认 65536 事件/256 MiB extent 读取字节安全阈值，显式可调；流式遍历按页不累计数组，不受该累计事件上限。扩大回归 865 passed、16 skipped，MySQL 因无专用库未验收。

- [ ] 真实 MySQL/NAS 上执行新增集成矩阵、检查 EXPLAIN、锁和 GC（未配置环境，不能记为已通过）。

详见[实现与验证报告](../reports/2026-11-05-checkpoint-read-contracts-implementation.md)。
