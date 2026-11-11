# Selected Cron Migration Implementation Plan

**Goal:** 旧页勾选父子组异步迁入 Scheduler，保留原身份与会话，并统一聊天页来源。

**Architecture:** 复用共库 registration journal 保存服务端迁移事实、步骤和冻结快照；SWE 仅提供只读来源及原绑定校验，Scheduler 注册新入口并发布整组。迁入标记以共库事实为准，不使用用户可编辑 meta 作为授权开关。

**Tech Stack:** FastAPI、现有 MySQL 事务层、React/Ant Design、pytest/Vitest。

**Spec:** `docs/brainstorms/2026-11-04-cron-selected-migration-requirements.md`

## Global Constraints

- 原 job_id、task_chat_id、task_session_id 不变；父子整组，旧在途不接管。
- 不写旧定义表，不改旧文件/旧平台入口；原生新任务双写不变。
- 迁入任务删除保留原会话资源；旧删除接口不修改。
- 异步、进度持久化、失败人工续办，已确认成功的注册不重复；整组就绪才投入调度。
- 非工作日默认不执行。会话中只显示成功迁入后的新版，不凭记录存在推断成功。
- 主线程顺序执行和自检，不声称独立审查；本轮不自动提交或推送。

## Tasks

### U1 迁入任务生命周期兼容

Files: `scheduler/.../managed/migration_policy.py`、`projection.py`、`controls.py`、`management.py`、SWE `managed_api.py`、`managed_resources.py`。

- [x] RED：可信迁入记录不写旧表；伪造 meta 不生效；普通新任务仍投影；迁入删除不清理共享资源；已读覆盖采集时已结束历史ID集合，而非仅按水位误收旧在途记录。
- [x] GREEN：`migration_policy` 只查询服务器 journal 中 action=migrate、完整身份与原绑定匹配的记录；投影、已读及清理复用该事实。
- [x] Verify：Scheduler生命周期测试及WSL资源测试通过。

### U2 SWE 只读迁移来源

Files: 新增 `src/swe/app/crons/selected_migration_source.py`、独立内部只读路由、来源测试。

- [x] RED：不初始化旧工作区、不改文件；父任务权限、完整父子身份、原会话绑定严格核验；原运行中状态仅保留快照。
- [x] GREEN：复用旧快照解析，以旧表关联定位选定父子工作区，不全量扫描所有用户文件；来源缺失/冲突明确失败；请求不允许任意文件路径。
- [x] Verify：真实临时jobs/chats文件与SQLite，校验读取前后字节不变及跨来源隔离。

### U3 Scheduler 迁移编排与归属查询

Files: 新增 `managed/selected_migration*.py` 和 migration 路由，注册引用/查询视图适配。

- [x] RED：批量预览/确认、持久进度、父组隔离失败、原ID导入、原绑定冲突拒绝、注册成功续办、整组发布、已删除仍能识别迁入归属。
- [x] GREEN：journal 的批/组进度和每任务 migrate 记录承载可信归属；新定义准备状态不进入执行；新timer绑定单独保存；普通与批入口遵循现有mode和日期策略。
- [x] Verify：SQLite执行真实事务，平台/SWE只在HTTP边界替身，验证旧表无INSERT/UPDATE/DELETE及新回调合同。

### U4 Console 入口与聊天兼容

Files: 新migration API模块、旧CronJobs勾选/确认/进度组件、cronTaskSources及Chat相关测试。

- [x] RED：已迁只新、未迁旧、准备/失败不隐藏旧、删除不复活、新请求失败不回旧写、身份切换回包隔离。
- [x] GREEN：统一来源选择后供列表、任务/会话定位、标读及已有动作使用；保持原页面样式，不引入新浏览器API。
- [x] Verify：Vitest、tsc、生产构建、本机Chrome模拟流程与截图；未用真实360/Chromium108运行。

### U5 验收与交付

- [x] Scheduler 1108项、WSL受影响SWE 31项、Console 120项通过；tsc及生产构建通过；修改Python函数复杂度<=15。详细静态检查限制见部署说明。
- [x] 确认旧API/旧JSON/旧表写入逻辑未被扩大修改，保留资源策略只影响已核实迁入任务；额外拒绝通过普通CRUD重试入口绕过整组发布。
- [x] 更新操作文档与预发布步骤，报告已知基线测试及未验证行内依赖；不运行生产迁移。

验证为主线程顺序自检，不声称执行了独立审查。操作与验收文档：`docs/deployment/cron_selected_migration.md`。
