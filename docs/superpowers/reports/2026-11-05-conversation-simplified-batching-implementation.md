# 简化展示读取与 B1b/B2 批量化实现记录

日期：2026-11-05。工作分支：`feature/conversation-mysql-nas-migration`，基线 `f4d400194`。

实现依据：[设计](../specs/2026-11-05-conversation-b1b-b2-batching-design.md)、[实施计划](../plans/2026-11-05-conversation-simplified-batching-plan.md)。保持 schema、索引、NAS 格式及接口结构；展示读取采用已确认的跨批非统一时点语义。

## 实现

1. History/Tracing/归档/问答展示移除全量 Message 重复复核及其 FOR SHARE，保留 NAS 前后的窄列 Chat 检查、授权、scope、正文身份和 extent 校验。Chat 全量列表复核改为逐批窄列普通读，早退出显式关闭迭代器、释放 SQL lease。
2. 固定模板覆盖 Chat、publication、share proof、prune 的 ID 查询，以及 anchor suppression、boundary、删除候选、Session/Chat 锁、分享 Turn/occurrence 查询。每批最多 256 项，64 KiB 请求预算再按物理连接 packet 缩小；binary key 按最坏转义字节计费，全部单项预算在首条 SQL 前验证。
3. 依赖 publication 按 ID 批量 FOR UPDATE，current 最后单条完整锁验；同事务已锁定的精确集合立即复用于 dependency 安装，省去第二组 publication 查询。独立 add_dependencies 仍自行锁验；写入 Source/Projection 最终复核、authority、代次、Stop、GC/备份保护及 UnknownCommit 流程保留。
4. reader/backup/dependency 多行使用固定 PreparedInsert；多行读取一次数据库时间，默认 100 行/256 KiB，受参数和 packet 再约束。单行保留原 INSERT。reader/backup 用 expected+1 一次检查完整成员，最多 4097 行；超 4096 的 dependency 按 member_key 每页 256 检查，检测缺项、额外项、released 和错误 scope。备份释放保留 snapshot_ref 校验。
5. Message 保留原 occurrence、在线/归档独立顺序、source locator 和 typed identity 校验，懒生成多行 PreparedInsert。全部行成功后才更新 high/revision；首中末批或行生成失败仍回滚原事务，不推算自增 ID。
6. 删除先固定候选 ID，再完成全部 Session 锁、Chat 锁和分批更新；不将中途新增记录纳入删除。分享 proof 分批锁定精确 Message，保留历史无 Turn 语义；分享 occurrence 跨批恢复 MySQL 原全局排序。旧无 proof 分享分支的单 key 点查保留。
7. pin 返回已知消费者需要的窄列 publication。分享 token 身份仍使用已有完整 committed plan 校验，并与 pin 返回字段逐项比较，避免 pin 批查携带大 plan。离线迁移只读 facade 新增受限 acquire，只暴露受校验的 SELECT 和 packet 信息，不暴露写入 session。

NAS 实际 IO 仍通过 PinBatch/ReaderPins 管理；取消时先排空真实读取再释放保护。本包不增加 NAS 交互，也未实施同 bundle 分组读取、NAS 背压或缓存。

## 成本证据与验证

- 4097 个 ID 在 packet 允许时为 17 次窄列集合查询，替代逐 publication 锁查询；锁总量和外层事务范围未缩小。
- 201 个 Message 使用实际 facade/session/PreparedInsert 路径发送 100、100、1 三批。校验在第 0/100/200 项失败时，即使调用方捕获错误，session 仍禁止提交已写入前缀。
- 覆盖 4097 个 dependency 成员尾部缺失、多余、released，binary key/小 packet，513 个删除候选固定及后批回滚，prune 首中末批失败，257 个分享 proof 最后批缺失，分享完整 plan 与窄列 pin 兼容，以及只读 acquire 禁止 mutation。
- 新增真实 MySQL pin/Message 首中末批失败回滚测试；保留既有 GC 等待、reader 排空、备份、恢复及 checkpoint 规模回归。专用库未配置时跳过，不视为验收通过。

最终验证：**1172 passed、287 skipped**（59.07 秒）。跳过项为未配置专用数据库的真实库用例，不能视为真实环境验收通过；全部任务代码文件的 pre-commit 检查通过（AST、mypy、black、flake8、pylint 等）。

```bash
../../venv/bin/python -m pytest \
  tests/unit/app/conversation_storage tests/unit/database \
  tests/unit/routers/test_tracing_chat_history_parity.py \
  tests/unit/routers/test_tracing_chats_api.py \
  tests/integrated/conversation_storage -q -rs
```

pre-commit 仅检查本任务的代码、测试和文档文件；未对原有不相关改动执行全仓库格式化。

## 后续边界

- 真实 MySQL 的查询计划、锁等待、prepared wire 行为、GC/备份交错与整体回滚仍须验收。现场驱动需与 manifest 对齐；本轮未升级共享虚拟环境。
- 不将 SQL 条数减少宣称为现场延迟或吞吐达标；当前没有业务 SLO。
- 旧无 proof 分享点查、历史 projection locator 点查、同 bundle 冷读分组、NAS 背压/缓存、写入快照准备，以及 compact 容量预检/错误终态仍独立待处理。
- 按用户后续指令提交本包代码、测试与文档；不推送，保留工作区原有不相关改动。
