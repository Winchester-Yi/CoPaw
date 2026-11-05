# 简化读取与 SQL 批量化实施计划

> 按 superpowers:executing-plans 在现有 worktree 顺序执行；不委派子代理。

**目标：** 实施已确认的简化读取、集合查询批量化和成员批写，保留写入/GC正确性。

**架构：** 展示移除全量Message重复复核，Chat保持窄列分批普通读。固定查询模板复用连接packet预算；publication锁在原事务复用，PreparedInsert用于成员和Message多行写。

**技术栈：** Python、pytest、现有MySQL facade/PreparedInsert。

## S1 读取与查询

- [x] 新增 `test_simplified_batching.py`，真实调用History/Chat方法，断言无Message复核、Chat批次有界、后批缺失拒绝。
- [x] 运行新增测试确认旧实现失败。
- [x] `history.py` 移除展示Message复核，保留NAS前后Chat复核；`reference_queries.py` 提供窄列Chat查询和逐批消费；二进制key固定查询另放 `collection_queries.py`。
- [x] 运行History、Chat、reference相关单测，更新仅依赖旧SQL形状的fixture，保留领域断言。

## S2 publication锁复用

- [x] 新测试覆盖257/4097依赖、missing/foreign/changed、current完整plan检查；观察旧逐条实现失败。
- [x] `source_messages.py` 批锁依赖、current最后完整锁；`retention.py` 增加仅在当前事务内部消费已锁定行的依赖安装步骤，独立入口继续锁验。
- [x] 运行source/projection/publication/retention回归，验证没有跨事务授权缓存。

## S3 retention批写

- [x] 测试多行预算、单行简单路径、数据库时间、released/额外成员、首中末批失败整体回滚。
- [x] `retention.py` 固定PreparedInsert；reader/backup按expected+1一次检查，dependency大集合keyset分页；`backup.py`复用成员读取规则且保留snapshot_ref检查。
- [x] 运行retention/backup单测与真实MySQL目标模块（无专用库则如实记录skip）。

## S4 Message批写

- [x] 测试101消息、混合两类顺序、重复source、最后批坏行回滚；先确认逐行实现失败。
- [x] `message_installs.py` 保持原行生成/顺序/校验，改多行PreparedInsert，不获取或推算自增ID；所有批次和high/revision仍在外层事务。
- [x] 运行Message/publication/archive/projection回归。

## S5 剩余集合SQL

- [x] 测试binary key按字节切批、固定delete候选、share proof精确成员、prune后批整体回滚。
- [x] `chat_repository.py`/`shares.py`/`archive.py`复用固定模板；保留父行锁阶段和单事务，不改旧无proof语义。
- [x] 全部数据库/会话存储单测、Tracing回归、相关真实库模块；仅任务文件pre-commit。

## 交付

- [x] 审查diff与设计逐项覆盖，记录未处理的旧无proof/历史locator点查。
- [x] 更新实施报告与排查索引。根据用户后续指令提交本包代码、测试和文档；不推送。

统一运行入口：`../../venv/bin/python -m pytest`；不读取dev.json，不升级共享venv。

执行结果：1172 passed、287 skipped；任务文件 pre-commit 通过。跳过项为真实数据库未配置，验收仍未完成；见[实现记录](../reports/2026-11-05-conversation-simplified-batching-implementation.md)。
