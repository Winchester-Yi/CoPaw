# 13 项旧 CLI 契约失败根因分析

日期：2026-11-11。目标工作树 `.worktrees/conversation-mysql-nas`，HEAD `9b72c860bc5b73135b8102c5ee6190663dffbf32`。本文前半保留初次诊断证据；随后已按用户要求实施修复，见末尾处理记录。源码行号对应修复前审计版本。

## 结论

不是 13 个独立数据库/迁移错误，也不能简单归为旧测试过时。直接根因是**两种不同清单结构共用 version=3，CLI 仅按该字段分派**。

1. `tests/unit/cli/test_conversation_storage_cmd.py:32` fixture 调用 `MigrationManifest.create`，生成旧 OfflineJob 结构：workspaces、assignments、batches，以及 offline.nas_roots，没有 services。
2. 提交 `7144571c3`（scope bundles by workspace）将 `migration/manifest.py` 的 create 版本号和 _validate 允许版本同时从 2 改成 3（当前第81、165行），用于旧清单自身格式演进。
3. `cli/conversation_storage_cmd.py:343-347` 却把所有 version=3 清单都送入 `run_v3_command`，跳过旧 OfflineJob 路径。
4. 支持的命令在 `migration/v3_manifest.py:22` 读取 services 失败；install/export 则更早在 `conversation_storage_v3_cmd.py:362-372` 被拒绝。

真实生产 v3 应通过 discover/freeze 生成，包含 services、shares、fingerprints 等发现证据，不是这个旧工厂生成的结构。正常 v3 路径不能凭本组失败判定不可用；旧清单 API/CLI 组合确有回归。

## 精确失败分类

| 测试实例 | 数量 | 实际错误 | 未到达的预期行为 |
| --- | --- | --- | --- |
| preview[inventory/plan/import/inspect] | 4 | Error: 'services' | 只读预览成功、文件不变、不连接DB |
| preview[install/export] | 2 | unsupported v3 command | 旧命令预览 |
| untrusted_targets[staging_schema/nas_roots] | 2 | Error: 'services' | 旧暂存库命名/NAS存在性校验 |
| writable_source_rejected | 1 | Error: 'services' | 旧模式的 read-only 权限校验 |
| unpinned_old_image_rejected | 1 | unsupported v3 command | SourceBoundary 的不可变镜像校验 |
| export_nonempty_destination | 1 | unsupported v3 command | 旧导出目标空目录校验 |
| legacy_apply_denied[import] | 1 | Error: 'services' | 明确拒绝 legacy v2 写入 |
| legacy_apply_denied[install] | 1 | unsupported v3 command | 明确拒绝 legacy v2 写入 |

合计 13 项：8 项缺 services、5 项不支持命令。4 项通过的测试未依赖上述正常 CLI 版本路由（只读包装、缺边界、recover、直接 OfflineJob.export）。

这些失败中的负向用例大多仍“非零退出”，但因更早报错，目标安全检查实际没有被测到。不能将其解释为权限或路径校验已被绕过，也不能据此宣称这些校验已经验证通过。

## 验证与排除

初始假设：版本错分派、旧命令语义不兼容、安全校验自身回归。先复现完整原测试文件，再做单变量路由探针。

- 原测试重跑：13 failed、4 passed，1.85秒；未连接数据库。
- git blame/show 将版本变化定位到 `7144571c3820d5023281ea65e492a396f62f47af`，不是当前用户未提交的 SQL 变更。
- 临时探针在进程内将旧工厂输出标记为2，同时让旧加载器在验证时接受该标签；保持清单其他内容、CLI代码和原测试断言不变。新 V3Manifest 不做修改。
- 相同17个原测试经探针运行：**17 passed，1.52秒**。说明只恢复旧路径可消除全部13项失败；本组未显示独立的安全校验实现故障。
- 探针位于 `/private/tmp/conversation-cli-probe/test_cli_version_probe.py`，没有修改仓库代码。测试使用 tests/fakes，不代表真实发布依赖及完整CLI可用。

不能将探针当正式补丁：此次版本升级同时引入 scope-aware publication assignment 和 bundle 定位格式；直接把产品代码版本改回2，可能把新格式错误伪装成旧格式，破坏持久清单兼容性。

## 修复建议与验证要求

优先明确清单的两个独立维度：工作流类型（旧 OfflineJob / discovery direct import）与该类型的格式版本。采用无歧义 discriminator，或明确识别并拒绝不兼容旧格式，禁止仅靠 version=3 推断工作流。已持久化清单的兼容/拒绝策略也需写清楚。

- 当前正式迁移测试应使用 discover/freeze 生成的真实 v3 fixture，并覆盖 inventory/plan/import/inspect、finalize 等实际命令。
- 若保留旧预览/导出路径，须以明确的旧工作流标识测试它；旧 import/install 的拒绝要发生在连接DB之前，并校验明确错误。
- 若决定整体退役旧路径，应同步移除/拒绝相应 API/命令并改为明确的退役契约，不能仅删失败测试。
- 补一项跨工厂与 CLI 的回归：每个公开清单工厂产物要么能被正确路由，要么得到明确不兼容错误，不能返回裸 services KeyError。
- 新 v3 的输入安全门禁按当前契约重新测试；旧 read-only/staging/export 断言不能机械搬到 v3（v3 无 staging，且新 discovery 不做旧的全源 mode 检查）。

## 已实施处理

- 保持持久格式 version=3，新增独立 workflow 标识：旧工厂生成 offline，discover/freeze 生成 direct。CLI 不再按 version 单独分派。
- 兼容已有无 workflow 的清单：具有 services 或 discovery_version 结构证据时走 direct，否则由旧加载器验证。未知 workflow、offline 与 discovery 证据冲突时明确拒绝。
- 两种加载器验证工作流匹配，刷新清单时保持工作流身份不变；direct 缺 services 改为明确 ValueError，不再显示裸 KeyError。
- 保留旧 import/install --apply 禁用门禁，不恢复旧写入。原13项失败测试保留且已通过，不删除/放宽原断言。
- 新增真实工厂→CLI回归，覆盖有/无标识的旧清单和discover/freeze新清单，以及未知/矛盾/缺字段清单在连接DB前拒绝。
- 扩围测试发现 NAS 演练脚本硬编码“第3次目录fsync失败”，在目录层级变化后会于最终链接创建前注入故障，却仍尝试confirm_durable。已改为观察最终bundle路径存在后注入目录fsync故障，增加文件失败无bundle、目录响应失败保留可读bundle的回归。

验证结果在手册第17节及独立代码核对报告中更新。完整发布CLI的trace_sdk依赖和真实数据库/NAS验收不由这些单元测试证明。

