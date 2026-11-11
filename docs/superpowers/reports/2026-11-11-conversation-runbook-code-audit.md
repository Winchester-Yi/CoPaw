# 会话迁移手册独立代码核对

日期：2026-11-11。工作树：`.worktrees/conversation-mysql-nas`。复核 HEAD：`9b72c860bc5b73135b8102c5ee6190663dffbf32`，包含已有未提交 SQL 变更。

## 审计结论

三个子代理分别审计 CLI/源数据、SQL/完整性、运行时/恢复，主代理复核证据、修正文档并重跑测试；SQL 与运行时代理再次复核修订。仅修改操作手册和文档索引，未修改迁移实现或现场数据。

首次迁移主链路与当前代码契约一致；原手册确实存在错误及遗漏，已修正。**不能确认当前交付已构成可直接执行的完整生产迁移与应急恢复方案**：完整 CLI 加载、真实 MySQL/NAS、HA/恢复发布切换仍有待关闭项；旧契约回归已在随后处理中修复。正确说明限制不等于已经解决限制。

手册：[会话元数据迁移及上线全流程操作方案](../plans/2026-11-02-conversation-migration-and-launch-runbook.md)。

## 发现与处置

| 发现 | 代码证据（相对仓库根） | 处置 |
| --- | --- | --- |
| 新 discovery 不产生全源哈希，不能声称 CLI 自动拒绝有写权限位的输入 | `migration/discovery.py:543`、`migration/inventory_v3.py:91` | 第0/7节改为运维强冻结门禁 |
| mtime 截止起点在首次正式 import/resume，不能证明 discovery 起内容不变 | `migration/inventory_v3.py:260`、`migration/freshness.py:13` | 明确检测保证范围与禁用保留 mtime 改写 |
| 未提供 override 完整结构和登记 workspace 限制 | `migration/discovery.py:277`、`:460` | 第8节补 JSON 示例与归属/Session 限制 |
| 不复制源 NAS 仍有解析临时副本 | `migration/legacy.py:46` | 第2节补 TMPDIR/ephemeral-storage 实测容量 |
| 分享收紧 SQL 包含 DELETE | `scripts/sql/conversation_share_service_unit_finalize.sql:22` | 保留 all-null=0、invalid_shares=0 和全量 verify 前置；不允许删除绕过 |
| 预检要求精确结构，不只是建表成功 | `migration/preflight.py:87`、`:118`、`:165` | 第5节明确列/索引/引擎/行格式限制 |
| 命名锁逻辑名字不同于实际服务器锁 key | `database/connection.py:194` | 第0节明确连接层哈希 |
| localhost 在应用启动路径禁用 DB | `app/_app.py:313` | 第4节补实际服务 DNS/地址要求 |
| 恢复 API 只接受生成的隔离库，不能原库 HA 重绑定 | `conversation_storage/backup.py:64` | 第12/13/16节区分能力，列为放行门禁 |
| 平台 dump/快照与应用备份格式不是同一输入 | `conversation_storage/backup.py:905`、`:1285` | 第16.2节拆两条路线 |
| 空隔离库没有现成分享 CREATE 脚本 | `scripts/sql/conversation_share_publication_extension.sql` | 明确须额外取得审核后的分享建表结构 |
| 恢复需要证据、空七表、新NAS、scope映射并检查返回值 | `conversation_storage/backup.py:1203`、`:1283`、`:1309`、`:1326`、`:1410` | 第16.2节补前置/通过判据 |
| runtime_manifests 只返回字典，没有自动发布 | `conversation_storage/backup.py:1463` | 明确恢复驱动须安全落盘并部署 |

表中 `migration/` 均为 `src/swe/app/conversation_storage/migration/`；`conversation_storage/` 为 `src/swe/app/conversation_storage/`；`database/` 为 `src/swe/database/`。

## 验证证据与尚未关闭项

- 本轮七个迁移相关测试文件重跑：**81 passed、13 failed，0.59 秒**；13项失败集中在旧 `test_conversation_storage_cmd.py`。后续根因已定位为旧清单格式升级与 direct-v3 路由的 version=3 冲突，不能仅归为测试过时；见[专项诊断](2026-11-11-conversation-cli-contract-failure-analysis.md)。不能仅删除旧测试来宣称通过。
- 独立六组 v3/discovery 契约：**77 passed，0.53 秒**。
- 子代理三组定向回归：**42 passed**，与上项重叠，不相加。
- 手册 **25 个 Bash 块**通过 `bash -n`，只证明语法，不证明平台参数和外部执行成功。
- `git diff --check` 通过。
- 完整 CLI 本地曾因 `trace_sdk` 缺失加载失败；pytest 使用 `tests/fakes`，不能替代真实发布镜像的 CLI 冒烟。
- 未连接真实 MySQL，未运行生产数据导入、集群NAS故障验证或Kubernetes发布。
- 当前没有生产原库原地 server_uuid 重绑定入口，也没有覆盖应用备份→隔离恢复→凭证落盘→部署切换的通用运维 CLI。现场驱动/平台操作单和演练结果仍为上线前必需交付物。

## 后续修复与最终回归

用户要求处理失败后，已修复version=3工作流冲突并增加显式workflow及无标识旧清单兼容。原13项用例未删除、未放宽断言，全部通过。扩围另发现并修复演练脚本目录fsync注入早于bundle发布的问题。

最终测试：**1087 passed、0 failed，53.58秒**，命令：

```bash
PYTHONPATH=src ../../venv/bin/python -m pytest \
  tests/unit/app/conversation_storage \
  tests/unit/cli/test_conversation_storage_cmd.py \
  tests/unit/cli/test_conversation_storage_discovery_cmd.py \
  tests/unit/scripts/test_rehearse_conversation_storage.py \
  tests/unit/scripts/test_benchmark_conversation_semantic.py -q --tb=short
```

修复后另由CLI子代理只读复查兼容路由、工作流冲突拒绝、旧写入口禁用及fsync注入，未发现实质问题；独立四组回归74 passed（与上面范围重叠，不累加）。

Black（line-length=79）、仓库Flake8参数及git diff --check通过。未解决真实发布依赖或替代实库/集群验收。

## 初次审计内容固定

审计期间工作树存在并发变化。以最终复读的文件校验和为准，正式发布前必须重新固定代码、SQL、手册和镜像，不能仅依赖 HEAD。

```text
d878ce2105d501d2aae58ddbac01185408eb6af90fa8728c0a54992adb853724  scripts/sql/conversation_share_service_unit_finalize.sql
c90e578904a41b77956b328143f045a0f90280244bb06fcea689c19a85b60b6b  src/swe/app/conversation_storage/migration/discovery.py
e517ab49e841ab22ffa11d6c2023f190be37be15fa2fc8eaf05f1c24057da12c  src/swe/cli/conversation_storage_v3_cmd.py
```
