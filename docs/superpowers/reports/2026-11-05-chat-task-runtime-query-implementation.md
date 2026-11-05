# 聊天任务轻量运行状态查询：实现与验证

日期：2026-11-05，Asia/Shanghai。工作区：`conversation-mysql-nas`。代码随本工作包提交。

承接[设计](../specs/2026-11-05-chat-task-runtime-query-design.md)及[实施计划](../plans/2026-11-05-chat-task-runtime-query-plan.md)。保持 schema、索引、存储格式及旧列表/详情/state/动作接口；只新增批量只读查询，并切换 Chat 的周期刷新。

## 实现结果与成本

| 路径 | 实现与成本边界 |
| --- | --- |
| Scheduler `POST /api/scheduler/cron/jobs/-/runtime-statuses` | 每批一条显式标量 SELECT + LEFT JOIN，复用工作区和创建者权限；不读 `spec_json`、NAS、历史或外部 HTTP，不写状态 |
| Legacy `POST /api/cron/jobs/-/runtime-statuses` | repository 保留一份不可变紧凑投影；未变文件每 HTTP 批次一次 stat；变化时仍需完整读取并校验 jobs.json |
| 可见页面 30 秒周期 | 仅展示任务及当前打开任务的 ID；每来源最多一个在途读取，200 一批顺序查询，来源所有批次验证成功后原子发布 |
| 完整来源查询 | 首次进入、返回 Chat、恢复可见、手动刷新及本页动作确认时读取；保留完整 membership，状态结果不能替代它 |
| 会话列表及正文 | 普通分页仍只读 summary；状态轮询本身不查 summary/正文，有新结果时沿用既有消息刷新机制 |

请求先校验原始数组容量 1..200，再去重，按唯一请求 ID 顺序完整返回。响应只含状态/展示白名单、Scheduler 三版本和管理状态，错误最多 1024 个 Unicode 字符、预览最多 256 个，提供截断标志。旧完整接口继续提供原内容。

Legacy 快照独立于可变 spec 缓存，签名包含 device/inode/ctime/mtime/size；核对打开对象、读取前后 fstat 及路径版本。替换竞态最多重试一次，失败不发布成功签名；并发 miss 共用加载，取消等待者不取消共享线程读取。即时状态每次从当前实例复制，排程纯计算，不做绑定修复或保存。

前端身份 key/epoch/source revision、取消与动作失效继续有效。状态成功保留定义和完整存在性集合，未请求任务保持原引用；字段未变时保留记录引用。异常保留已知展示；不可见/缺绑定/定义版本变化每轮最多确认一次对应完整来源；缺失状态行直接报错。只有明确 404/405 在当前可见生命周期内降级旧查询，401/403/503、格式错误和取消不降级。

## 最终复核修正

1. 最初周期收集全部已加载任务，会使 legacy 非本人任务返回 unavailable 后重复完整查询。已改为展示任务与当前打开 Chat 的任务；完整 membership 不变，补充筛选及未请求记录引用回归。
2. Legacy 缓存按创建者预计算隐式 manual 暂停，会使管理员查看他人禁用任务时与原视图不同。已改为按本次调用者推导，实际 full task view 对照测试先复现失败，再修正通过。
3. JavaScript UTF-16 长度与 Python Unicode 字符数不同。已按 Unicode 字符数验证预览，覆盖 256 个 emoji 成功、257 个失败；合法边界测试先失败再修正通过。

## 验证证据

- 项目虚拟环境 pytest：**94 passed**。范围为 `test_cron_runtime_query.py`、`test_cron_json_repo.py`、`test_cron_task_view.py`、Scheduler 的 `test_managed_cron_runtime_query.py`、`test_managed_cron_queries.py`、`test_managed_cron_management_api.py`。
- Console：**540 passed、2 skipped**，57 文件。覆盖 Chat、runtime loader/容量/取消/覆盖性校验及共享协调器；命令及既有失败排除条件如下。
- `pnpm build` 通过，包含 `tsc -b` 类型编译；保留已有大 chunk 提示。受影响实现和新增测试 ESLint **0 errors**，Chat 根组件保留 3 条既有 hooks 警告。
- 新增后端文件、JSON repository、Scheduler router 及新增测试 Ruff 通过。包含旧 `crons/api.py` 的检查仍报 HEAD 已存在的未用导入 `resolve_scope_preferred_tenant_id`，未修改无关基线，不宣称全仓 lint 通过。
- 新增/编辑的核心前端文件经 Prettier 处理，最终 `git diff --check` 通过。临时依赖链接在检查后移除。

```bash
# 在 worktree 根目录
../../venv/bin/python -m pytest \
  tests/unit/app/test_cron_runtime_query.py \
  tests/unit/app/test_cron_json_repo.py \
  tests/unit/app/test_cron_task_view.py \
  tests/unit/scheduler/test_managed_cron_runtime_query.py \
  tests/unit/scheduler/test_managed_cron_queries.py \
  tests/unit/scheduler/test_managed_cron_management_api.py -q

# 在 console/ 目录
pnpm test:run src/pages/Chat \
  src/api/modules/cronRuntime.test.ts \
  src/api/modules/cronRuntime.validation.test.ts \
  src/stores/chatTaskSourceStore.test.ts \
  --exclude '**/runtimeMessageHeader.test.tsx' \
  --exclude '**/FileManager/index.test.tsx' \
  -t '^(?!.*(?:resolves the backend chat id and renders context usage|does not show the previous active chat usage)).*$'
pnpm build
```

前端跳过的两项 ContextUsageIndicator 断言、排除的 runtimeMessageHeader 和 FileManager 既有失败详见[上一包验证记录](2026-11-05-chat-task-source-snapshot-implementation.md#扩大回归发现的已有或无关失败)。没有把它们计为通过，也没有修改无关测试以使结果全绿。

## 尚未验证及剩余边界

- SQL 测试使用数据库替身和实际 SQLite 关系查询；尚未连接专用 MySQL，未验证真实 EXPLAIN、扫描行数、索引选择或延迟。
- 文件测试使用本地临时文件；未在真实 NAS 挂载测量属性可见性、原子替换、系统调用和字节量。Legacy 热路径仍有 stat，变更时仍读整文件，不能宣称零 NAS。
- 未进行 Chromium 108/360、当前浏览器及 `hideMenu=true` 的实际 UI 验收。代码沿用现有布局，新增逻辑只使用基线支持的集合、Promise、AbortController 和 Unicode 字符迭代；构建/jsdom 不替代实际浏览器验收。
- Legacy `_states` 仍为实例内存状态，不代表其他实例的全局状态；未调整执行和写入协议。
- Tracing 单对象映射、checkpoint/plan 容量和正文 NAS 批量 IO 属于后续工作，本包没有变更。

## 提交前联合回归

2026-11-05 提交前将本包与其依赖的共享来源工作包合并验证：Python **152 passed**；Console **559 passed、2 skipped**（61 文件，沿用上述既有失败排除条件）。生产构建包含类型编译；真实数据库、NAS 和浏览器限制不变。

提交前已对本次 46 个明确文件运行完整 `pre-commit run --files ...`：AST、编码、密钥检测、空白、TypeScript、trailing comma、mypy、Black、flake8、pylint 均通过（不适用的格式检查跳过）。本次修正新增路由的条件拆分及字典字面量，格式化后重跑相关 Python 回归。该结果只覆盖提交文件，不宣称整个仓库通过。

## 后续处理顺序

1. **正确性优先**：当前 `ArchiveReader.checkpoint(limit=4096)` 仍固定窗口和单批 pin；拆清 record/prefix/page/full，完整遍历固定 high，补齐迁移及恢复验证。`CommitPlan.serialize` 仍缺少 parse 端 1 MiB 的最终字节限制，需在 reserve/NAS 之前拒绝不可读回的 plan；compact 还需生成前容量预检、生成后最终校验和明确终态。
2. **轻查询收尾**：Tracing 的 Session→Chat 映射改成单对象查询；继续核对 Stop、Goal、SubAgent 和窄字段更新的完整对象预读，保留历史授权必要 fallback。
3. **正文及数据库批量成本**：locator/pin/dependency/复核 SQL 有界分批，同 bundle extent 分组读取与有界不可变缓存；不削弱当前授权、GC pin 和提交持久性。
4. **控制请求和后台负载**：renew/stop 连接容量预留与 deadline、NAS 有界队列/背压、备份及 GC 后台配额；guard 锁协议仅在测量确认后调整。
5. **发布验收**：真实 MySQL EXPLAIN、NAS 成本/替换一致性、多实例与满载续租、Chromium 108/360 和 hideMenu=true；先建基线，再确定生产容量目标。已有无关失败单独修复。

以上为剩余工作清单，本次没有实施这些后续改造。旧 Cron 的文件 stat 和实例内 `_states` 仍是已知边界。
