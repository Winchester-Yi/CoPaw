# Token 诊断实现校验记录

日期：2026-10-11。工作区：`codex/token-usage-diagnostics`，基线 `99a2d861446a55415a9e54fb560f7895c1172a95`。未提交、未推送、未执行业务数据库 DDL。

## 审查方式

按仓库 AGENTS.md 的 Task 映射，在主任务内顺序执行四个审查视角；不是四位独立审查者。以实际源码、失败后修复的测试和真实 Console 路由的浏览器验证为证据。

| 视角 | 检查内容 | 本轮修复 |
| --- | --- | --- |
| 记录与隐私 | 未知 / 明确 0、缓存子项、指纹、媒体、来源引用 | 缓存写入但无读取时的规范化；AgentScope metadata 子项；嵌套媒体不能伪造完整估算；只保留数字 / HMAC / 来源引用 |
| 流与运行时 | 重试、取消、关闭、Task 归属、子进程、辅助调用 | 不在 yield 期间绑定 ContextVar；跨 Task 关闭；未消费首个 chunk 的显式关闭；合并分段 usage 和最后结束原因；Worker 继承启用快照；外部标题用量未知 |
| 存储与分析 | 自有连接、修订重放、排队限额、租约、清理、范围查询 | 写入取消时保留未完成快照；关闭超时仍关闭池；活跃与写入中数量预算；并发分析新数据保持 dirty；证据不足不解决旧问题；保留活跃 / 近期子分支；小连接池初始化锁 |
| UI 与接口 | 当前租户、请求竞态、总量 / 估算、操作、长 ID、嵌入布局 | 旧响应不覆盖新范围；Trace 关联入口；分类锚定只适用于完整估算；未知字段不显示为 0；问题关联调用；固定可见详情操作 |

## 已完成的验证

- Linux 项目 venv：最终相关回归 **197 passed**。包括 `tests/unit/token_diagnostics/`，模型工厂、工具输出、压缩、旧用量包装器、标题、Runner 边界 / 重试及启动 schema 测试。新增工厂集成测试实际启用诊断并关闭 TraceManager，验证既有 Retry / Token 包装链的提前关闭。
- Windows 的独立模块回归此前为 **52 passed, 1 skipped**；唯一平台 skip 是依赖既有 `fcntl` 的工厂集成。其后新增 usage 分段测试已包含上述最终 Linux 回归。
- 前端 / 导航：**9 passed**；TypeScript、改变范围的 ESLint / Prettier 与生产构建通过。构建保留大 chunk 与静态 / 动态混合导入提示，未为本特性重构全局打包。
- Black（79 列）、改变范围的 Flake8、独立模块 compileall、`git diff --check` 通过。
- 真实 Console 路由配数字 fixture：1280×720、1440×900、1920×1080；hideMenu 模式没有 Header / Sidebar；任务选择、问题关联调用与刷新通过，无 pageerror；截图位于本地 `C:/Users/lenovo/.codex/visualizations/2026/10/11/token-diagnostics/`。
- GitNexus：编辑前模型工厂为 CRITICAL，6 个直接入口；已向用户报告。其它主要接入符号为 LOW。最后 detect-changes 对已索引修改识别 22 个文件、36 个符号、2 个应用 lifespan 流，整体 medium；新模块通过源码和测试独立核查，不能把旧索引输出当作全部新模块验证。

## 基线失败与未验证部分

扩大回归曾得到 **200 passed, 1 failed**：`test_get_in_memory_memory_uses_instance_tenant_id` 在 `_clone_unbound_chat_memory` 给字符串赋 `add` 时报错。用 HEAD 的独立临时源码归档运行同一测试，重现完全相同错误，未修改该既有逻辑。最终绿色回归未包含这个旧测试文件。

SubAgent 全目录旧测试多项依赖当前已为空的 builtin_definition_provider（如 plan-researcher），因此不把全目录声明为通过；没有恢复用户未要求的内置定义。

未进行真实 MySQL / 多进程 / Provider 账单联调。SQL、环境、网关身份核验、连接总预算、30 天清理和有界分析窗口的上线要求见 [部署说明](../../deploy/token-diagnostics.md)。没有原文内容的技能 / Hook 细分类、内部 SDK 重试及未透传字段不能可靠恢复。
