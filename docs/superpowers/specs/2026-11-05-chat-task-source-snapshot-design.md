# Console 会话分页与任务来源解耦设计

日期：2026-11-05，Asia/Shanghai。状态：本工作包已实施，尚待真实浏览器与部署验收。

基线：`a1f7bae75`、`16c532d7d`，工作区 `conversation-mysql-nas`。
本设计承接[整体性能设计](2026-11-04-conversation-storage-performance-redesign.md)及[摘要查询工作包](../plans/2026-11-04-conversation-summary-query-plan.md)。

## 1. 已确认的需求与交付边界

- schema、索引和持久化格式保持不变，非必要不访问 NAS。
- 用户确认：其他标签页或其他实例修改任务后，返回聊天页或手动刷新时感知即可。本页操作结束后立即更新或重新确认对应来源。
- 会话首屏、历史翻页、标题刷新、临时 ID 解析不应为了判断任务是否存在，重复加载全部任务。
- 保留 legacy / scheduler 两套来源、同 ID 不同来源、任务展示和动作协议；任务查询失败不能等于任务不存在。
- 会话 cursor、详情 metadata、本地草稿、正在生成的消息、目标身份和权限隔离保持原语义。
- 不承诺实时感知外部定义变化，也不引入列表共享 MySQL 快照、Redis 缓存或跨标签页同步总线。
- 本工作包先消除分页的任务查询依赖、共享重复请求。既有任务执行状态刷新先保留；降低该刷新本身的全量读取成本属于后续工作包，不能把它算成本包收益。

## 2. 当前入口与实际成本

| 入口 | 当前行为 | 要保留的需求 |
| --- | --- | --- |
| `sessionApi.getSessionList` | 摘要页与 `listTaskSources` 并行，等待两者后过滤任务会话 | 历史翻页、旧页合并、临时 ID 解析、任务会话过滤兼容 |
| `sessionApi.loadMoreSessions` | 每一页再次完整查询两个任务来源 | cursor 连续性、重试、在途去重 |
| `useChatTaskSources` | 聊天页挂载独立加载两个来源 | 任务区域、当前任务和 Task ID 深链使用完整任务数据 |
| `Chat/index.tsx` | visibility/taskCreated/动作后刷新；三组轮询常量目前均为 30 秒 | 未读、执行中、首个结果等运行状态更新 |
| `ChatSidebar` | visibility 又刷新 SessionApi；普通历史最终排除 `session_kind=task` | 恢复页面时更新会话与任务，各自独立 |
| `ChatSessionDrawer` | 使用共享会话列表，可能展示任务会话 | 任务存在性变化应更新视图，不能仅处理普通历史面板 |
| legacy `/cron/jobs` | `list_jobs` 加载完整文件，逐项补历史绑定、计算 next_run_at、生成任务视图 | 本包不改旧接口和补绑定行为 |
| scheduler `listAll` | limit=200 遍历所有 cursor 页后得到完整列表 | 身份验证、全页成功后才确认集合完整 |
| Chat 动作与已读 | optimistic 更新、服务端命令、`refreshJobs`；新协议保留 operation/request_id | 不因缓存省掉版本校验、命令对账或失败重试 |
| 管理页与 CaseDetailDrawer | 管理页独立列表/动作；CaseDetailDrawer 发 `taskCreated` | 返回聊天页重读；旧事件继续支持 |

证据文件：`console/src/pages/Chat/sessionApi/index.ts`、`useChatTaskSources.ts`、`index.tsx`、`components/ChatSidebar/index.tsx`、`components/ChatSessionDrawer/index.tsx`；`console/src/api/modules/{cronTaskSources,schedulerCron,cronjob}.ts`；`src/swe/app/crons/{api.py,repo/base.py,repo/json_repo.py}`。

`JsonJobRepository.get_job` 有签名快照，但 `list_jobs -> load` 仍会读取并解析整个文件。不能把单条详情的缓存当成完整列表已无 NAS 成本。

设 legacy 一次完整列表为 L，scheduler 全部 cursor 页为 S，会话页为 Q。当前一次会话页需要 Q+L+S，聊天页还独立请求 L+S；首屏和可见性恢复有重复，N 次翻页额外产生 N×(L+S)。SQL 摘要页零 NAS 不代表侧栏整条链路零 NAS。

## 3. 三种方案比较

| 方案 | 收益 | 局限 | 结论 |
| --- | --- | --- | --- |
| 给 `listTaskSources` 加短 TTL | 改动少，命中时少发请求 | 冷启动仍阻塞会话；任意 TTL 不能表达失败、来源完整性和动作失效；展示状态与存在性仍混用 | 不选 |
| 一个当前身份的任务快照，分页只读其存在性投影 | 分页零任务请求；消除同轮重复；可明确失效、失败与迟到响应 | 需要统一任务读取所有者和会话视图重算 | 推荐本包实施 |
| 新增按页 task ID 批量存在性 / 状态接口 | 响应更小，可避免完整列表 | legacy 权威仍是文件；scheduler 是独立协议，需确认可用服务接口；任务区域仍需完整展示数据 | 后续按测量决定 |

不直接移除所有任务会话过滤：普通历史虽然排除任务，但共享会话状态、会话抽屉、初始选择和深链仍消费这些记录，取消过滤会改变兼容行为。

## 4. 目标数据流与职责

```text
会话首屏 / 翻页 / 标题刷新
  -> ChatSummary cursor 页
  -> SessionApi 已加载会话缓存（含草稿、详情、任务会话）
  -> 使用当前任务存在性投影生成会话视图

聊天页任务生命周期
  -> 当前身份任务快照协调器
     -> legacy 完整读取
     -> scheduler 完整遍历
  -> 任务区域与当前任务展示
  -> 发布存在性变化，内存重算已加载会话视图
```

- **loader**：复用 `cronTaskSources` 的来源校验、tagging 和全页读取，不增加永久缓存。
- **共享快照**：建议新增一个 `chatTaskSourceStore` 模块；只维护当前身份的一份数据，负责来源结果、在途合并、失效和通知。可使用现有 Zustand，不新增请求框架。
- **`useChatTaskSources`**：成为快照订阅和聊天页生命周期适配器，保留现有 jobs/error/refreshJobs 的调用习惯；不再维护另一份权威清单。
- **SessionApi**：会话读取只调用 summary；同步读取当前快照的存在性，不启动、等待或重试任务请求。
- **共享会话状态桥接**：任务存在性变化时只重算已加载会话，不能调用 `getSessionList` 导致重新发 summary 请求。
- **管理页**：本包不把带 includeDeleted/跨目标等条件的管理列表写入聊天快照；返回聊天页触发重读即可。

loader 需要按来源暴露独立读取，协调器才能在一个来源悬挂时发布另一来源。旧 `listTaskSources` 的 Promise.allSettled 包装继续保留给兼容调用方，不能让协调器等待该聚合 Promise 后才分别发布。

这是一个具体的任务读取模块，不设计通用查询缓存、任意 TTL/LRU 或多工作区历史缓存。

## 5. 身份与状态契约

### 5.1 当前身份与请求版本

沿用当前 `cronAccessKey` 的 actor/tenant/source/agent/manager/admin 维度，并维护递增 epoch。身份或角色改变、退出或上下文清空时，立即清空展示和存在性、使旧请求失效，尝试中止旧读取。

即使 A→B→A 的字符串 key 再次相同，旧 A 请求也必须因 epoch 不同被丢弃。认证凭据由现有请求层实时构建，不放进快照键、日志或本地持久化；普通 token 续期不等于身份变化。

legacy 完整读取需增加可选的捕获 context 和 signal，仍保留无参接口兼容。任务读使用捕获的 `cronContextHeaders`，不能在 await 之后按新身份重新定位旧请求。scheduler 所有页继续传同一个 context/signal；身份失效后不得启动下一页。

### 5.2 每个来源分别记录

| 状态 | 用途 |
| --- | --- |
| items | 最后一次完整成功的展示数据；失败时可保留 |
| membership：Set 或 null | 完整、未失效且成功的任务 ID 集合；null 表示不能断言不存在 |
| revision | 来源失效/变更版本，用于拒绝变更前的响应 |
| pending / error | 在途与错误展示；不得把失败写成空集合 |

不单独缓存第三份合并 tasks；legacy 在前、scheduler 在后，以现有 helper 投影。只有合法空响应得到空 Set；scheduler 中途失败、取消、重复 cursor、身份不合法均不能发布前几页为完整集合。

展示可保留旧数据，存在性在失败或失效后必须变为 unknown。仅凭 `items.length===0` 无法区分未加载、合法空和失败，Task ID 深链也必须检查来源完成状态，不能据此误报不存在或无限等待。

现有 `setJobs` 乐观修改须迁为按完整任务身份作用于展示的临时 override，不能覆盖来源成功快照或 membership。动作后确认成功时以服务端结果替换；动作失败或确认失败时撤销 override，恢复最后成功展示并提示错误。允许相应调整少量 Chat 动作调用点，不能为了原封不动保留 setter 而混淆数据权威。

存在性按 owner 判断；来源不明时，任一来源包含即可保留，任一来源 unknown 也保留，仅两边均确认不存在才隐藏。缺失 task_job_id 的损坏记录沿用现有 helper 的过滤规则，不与来源失败混为一谈。

## 6. 刷新策略与在途规则

| 触发 | 行为 |
| --- | --- |
| 进入/返回聊天路由、页面重新可见 | 失效旧存在性，刷新两来源；任务旧展示可保留，会话 summary 独立刷新 |
| 用户明确刷新任务/页面 | 刷新两来源；不改变 cursor 已加载页和当前消息 |
| 普通会话首屏、翻页、标题事件、临时 ID 解析 | 不发任务请求，只使用内存快照；无快照时按 unknown 处理 |
| 本页 legacy 动作或已读 | 仅失效和确认 legacy；既有命令语义不变 |
| 本页 scheduler 动作或已读 | 仅失效和确认 scheduler；保留 operation/execution/request_id |
| `taskCreated` 旧事件 | 兼容刷新 legacy，不再额外触发会话翻页读取任务 |
| 既有 30 秒运行状态 tick | 可见聊天页保持当前运行状态更新能力，交由同一协调器合并；本包不增加新的全量定时器 |
| 页面隐藏 | 暂停新的周期查询；中止或失效未完成读取，重新可见时重读，不承诺隐藏期间实时展示 |
| 无聊天页订阅者 | 不启动后台新查询；离开后保留与否不影响下次进入必须重读的规则 |

用户允许外部定义变化等到返回/手动刷新，本包不额外建立跨实例监听。保留的执行状态轮询可能更早看到变化，但不提供实时保证。不能把这一确认解释成可以停止任务未读/运行结果更新。

同一来源、同一 epoch/revision 的并发读取共享一个在途 Promise。visibility、任务初始挂载、显式刷新同时发生，最多启动一轮；消费者取消订阅不能误取消其他仍在使用的同轮读取。visibility 和轮询只由任务生命周期所有者注册一次，SessionApi/Sidebar/抽屉订阅者不各自注册任务刷新。

普通 refresh 调用不递增失效 revision：已有同版本读取则复用；没有在途读取才开始新一轮。动作、身份改变和隐藏/返回生命周期才使旧版本失效，同一生命周期事件只处理一次。正常可见期间的周期刷新可在结果到达前保留未失效 membership，不能每次 tick 都把已确认缺失的任务临时恢复；读取失败后改为 unknown。手动刷新先将 membership 改为 unknown，但可复用当前同版本读取，不要求精确捕获点击瞬间的远端状态；返回页面则因生命周期已失效而启动新版本读取。

动作开始时使对应来源的存在性失效，乐观修改只影响展示。动作结束（成功、失败或结果不确定）后，在仍为当前身份的前提下安排一次服务端确认；失败不把本地乐观删除发布为真实删除。新协议“已受理”不能直接当“已逻辑删除”。

若动作期间已有读取在途，旧 revision 响应不能发布；只合并一次待重读标记，旧读取中止/结束后启动确认。多个动作可合并确认，但不能把动作前的在途 Promise 当成动作后的刷新完成。无任务操作时普通同轮刷新可以复用现有读取。

来源结果各自发布，一边慢或失败不阻塞另一边。保留已有失败提示与后续刷新重试；不增加分页重试、每个订阅者自己的轮询或自动无限重试。

成本边界：一次任务完整确认仍为 L+S；一次会话页只有 Q；任务 membership 通知引起的会话视图重算为零网络请求。运行状态 tick 的 L+S 是保留成本，本包不宣称已经消除。内存复用两来源已有完整任务数据与已加载会话，不另存所有工作区或跨标签页副本。

## 7. 会话缓存与展示投影

**任务存在性只能影响视图，不能破坏已加载会话缓存。**

现在 SessionApi 会先过滤再把结果写回 `sessionList`，不能直接换成缓存的任务集合后继续沿用。否则失败转 unknown、删除回滚或任务重新出现时，之前剔除的记录无法恢复，只能重新读后端。

建议保留一个已加载的原始 Session 缓存，在公开返回列表和共享 UI 投影时应用现有 `filterStaleTaskSessions`。不复制第二份正文缓存：草稿、完整 metadata、messages、realId/sessionId 和生成状态仍在原对象中维护；输出只是投影数组。

- `getSessionList/loadMoreSessions`、返回列表的本地修改和无 IO 的 `getLoadedSessionView` 使用同一投影入口，防止其他返回路径绕过过滤。
- membership 变化由 Chat 根部桥接到共享 sessions：先复用现有规则合并正在生成的消息/本地会话，再最终应用过滤。不能只过滤 incoming 后调用 `mergeConcurrentSessions`，因为它会把 currentOnly 的任务重新加回来。
- hidden→unknown 或重新存在时可从已加载缓存恢复；已有的显式 Chat 删除仍真正移除缓存，并通过 excluded identity 防止旧回包复活。
- 普通历史仍排除 task，数量仍为已加载的普通历史数；抽屉使用同一过滤后的共享视图。
- presence 不作为正文访问授权或 404 依据。当前打开会话与深链仍以真实详情/动作服务端校验为准；任务查询失败不能清空当前消息或擅自跳转。
- cursor、has_more 来自 summary 响应，不按投影后的条数推算。全页都是任务或被隐藏时仍可继续下一页；不自动遍历全部页面凑满一页。
- 保留上一工作包的完整 metadata 合并、迟到 summary 防覆盖、身份 generation fence 和本地 ID 解析。

## 8. 首期修改范围与后续边界

首期预计修改：

1. 新增 `console/src/stores/chatTaskSourceStore.ts`（具体文件名以实现评审为准），一个当前身份快照及来源协调。
2. `api/modules/cronTaskSources.ts`、`api/modules/cronjob.ts`：可独立读取来源；legacy 捕获 context/signal；保留旧 `listTaskSources`、`mergeTaskSources` 的兼容导出。
3. `pages/Chat/useChatTaskSources.ts`、`index.tsx`、`useManagedTaskActions.ts`、`useTaskReadOnOpen.ts`：共享读、来源明确的动作失效、现有 visibility/轮询的单一所有者。
4. `pages/Chat/sessionApi/index.ts`、`taskSessions.ts`：去除任务 IO，缓存/视图分离和无 IO 投影；纯过滤 helper 尽量复用。
5. Chat 根部/Sidebar 与会话抽屉：用同一桥接结果更新共享列表，保护 currentOnly、消息和生成状态。

不扩展管理页的通用缓存；管理页操作后返回聊天页必重新读，满足已确认需求。CaseDetailDrawer 的已有 taskCreated 继续支持，若后续事件携带 context，新增字段须可选，旧事件仍可工作。

本包不修改后端 Cron 生命周期、JSON file-lock、任务 schema、scheduler 服务写协议、NAS fsync、正文详情授权；不为仅判断存在而额外创建逐 ID NAS 请求。

后续单独设计任务运行状态的轻量批量查询。现有 legacy state 接口也会确认任务定义，不应直接循环调用它来“替代”完整清单；scheduler 的外部协议能力需先核实。目标是让必要的状态刷新不反复搬运任务定义，而非随意延长 TTL。

Tracing 单对象 Session→Chat 映射另起工作包，避免与任务来源状态机制混在一次实现中。

## 9. 验收矩阵

| 场景 | 必须证明 |
| --- | --- |
| 任务接口悬挂，summary 正常 | 会话首屏及下一页仍成功；不等待任何任务 Promise |
| 任务接口均设置失败哨兵，连续翻页 N 次 | 翻页产生 N 次 summary 请求、0 次任务请求；旧 cursor 行为保持 |
| 挂载、可见性恢复、多个订阅者重叠 | 同 epoch/revision 每来源最多一轮；scheduler 页数按完整遍历计，不能声称一次 HTTP |
| scheduler 中途失败/重复 cursor/非法目标身份 | 丢弃该轮部分结果；旧展示保留、membership unknown；legacy 独立更新 |
| 合法空与失败 | 空 Set 可以隐藏已确认缺失的任务；失败/null 不能隐藏合法身份的任务会话 |
| 两来源同 ID、来源未知、缺损 task_job_id | 保持现有区分与损坏记录过滤，不把同 ID 合并 |
| 乐观删除失败、操作仅受理、写前读取晚到 | 不发布伪删除；旧 revision 不覆盖；最后一次确认使用动作后版本 |
| 空→存在、隐藏→unknown、完整刷新删除 | 已加载会话无网络恢复/隐藏；不破坏草稿和正在生成消息 |
| mergeConcurrentSessions 的 currentOnly | 最终投影不能复活已确认隐藏的任务；普通本地会话仍保留 |
| user/source/agent/角色变化、A→B→A | 立即隔离，旧回包、旧动作回调和下一 cursor 页均不能写入新身份 |
| 临时 ID、Task ID/Chat ID 深链、详情、抽屉、标题刷新 | 保持选择、路由与 metadata；合法空来源能结束 Task ID 等待，失败不据旧快照自动导航 |
| 返回路由/标签页、taskCreated、本页动作、已读 | 按触发矩阵更新；无分页隐含任务请求；旧创建事件保留 |
| 任务执行状态 | 既有未读/运行中/首次结果刷新不退化；不因窗口消费者增加而多出轮询器 |
| 页面隐藏和恢复 | 隐藏期间不启动周期请求；恢复只启动一轮；隐藏前旧页不能继续遍历或发布 |

验证使用现有 Vitest、React hook/UI 测试，前后端契约回归按影响范围执行。补充请求次数和延迟响应测试，不新增只镜像内部字段的测试。

本实现没有新增 CSS；使用 React 18 订阅、AbortController 和现有浏览器事件，未依赖 Chromium 108 之后的原生 API。仍须在真实 Chromium 108/360 和当前浏览器覆盖侧栏、抽屉、身份切换、错误及 hideMenu=true。本轮未运行这些验收，不能用 build 代替。

退出条件：分页对任务源无依赖；同轮任务读取不重复；任务存在性 unknown/empty 和来源隔离有测试；原会话与任务动作契约保留；明确记录剩余每轮 L+S 的真实成本。尚无目标规模和延迟 SLO，不给出未经测量的毫秒或吞吐承诺。

## 10. 设计复核记录

- 已按当前源码核对入口；新设计没有把 JsonJobRepository 的单条缓存误当列表缓存。
- 已区分任务定义外部变更和任务执行状态，避免把“返回时更新”误用为停止运行状态刷新。
- 已处理缓存负结论失效、在途动作、A→B→A、部分来源失败、过滤后被 currentOnly 合并复活，以及无网络恢复视图的问题。
- 现已实施此工作包，保留原 HTTP 与旧前端 API 方法。运行态共享快照、分页解耦、动作来源确认和线性会话合并均有回归；未完成压测或真实浏览器验收。
- 详见[实现与验证报告](../reports/2026-11-05-chat-task-source-snapshot-implementation.md)，包含扩大测试中的已有/无关失败及剩余每轮 L+S 成本。


后续具体设计：[聊天任务运行状态轻量查询](2026-11-05-chat-task-runtime-query-design.md)，已实施并完成自动化验证，详见其实现报告；真实环境验收仍待进行。
