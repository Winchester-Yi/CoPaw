# 聊天历史首次触顶不继续加载：触发状态与分页衔接

分析日期：2026-10-19。代码基线：`49de0401196c9770184a4803f6097d97f6d56ff6`。
GitNexus 绑定 `/Users/shixiangyi/code/Swe`（CoPaw），索引提交与当前 HEAD 一致。

## 基线问题结论与证据范围

当前实现存在可以通过真实 `useHistoryPreload` Hook 复现的触发停滞：进入顶部预加载区后，
`wasNearStartRef` 被置为 true；完成本地展开或归档请求只释放请求锁，不释放区域进入锁。
后续必须在一次位置检查中观察到距离顶部大于 240px，才能再次加载。

这与“到顶没有继续加载，向下再向上滚几次才加载”的现象高度一致。首次到达真正顶部前，
预加载可能已经展开了一批本地历史；不能把第一次视觉触顶等同于第一次加载回调。
当前没有用户现场的浏览器事件轨迹和请求记录，因此不能断言每次现场故障均由同一分支造成。
以下原因分析基于上述提交的旧实现；本次后续修复与验证记录见文末。

## 实际调用链

1. `ChatAnywhereSessionsContext.tsx` 加载当前会话，并给恢复消息标记 `history: true`。
2. `MessageList/index.tsx:230` 统计本地历史，初始只显示最新 10 张历史卡片；实时卡片不计入该窗口。
3. `Bubble.List` 使用 `order="desc"`、`column-reverse`，底部 `scrollTop=0`，向上为负数。
4. `useHistoryPreload.ts:16` 根据负坐标计算距离顶部，距离不超过 240px 即进入预加载区域。
5. `MessageList/index.tsx:440` 的 `advanceHistory` 优先显示下一批最多 10 张本地卡片，随后立即 return。
6. 只有下次触发且本地历史全部显示时，才调用 `loadOlderHistory`，请求
   `/chats/{chat_id}/history?limit=20[&before=cursor]`。
7. 后端 `src/swe/app/runner/api.py:get_chat_history_page` 读取归档页并返回 `has_more/next_cursor`。

主聊天界面明确传入 `pagination={false}`，也没有把 `onReachStart` 绑定到历史加载。
因此 `BubbleList.tsx` 内旧 `LoadMore/useInViewport` 分支及其首次可见跳过逻辑不是本链路的直接原因。

## 主因：区域进入锁缺少内容推进后的同步

关键位置：`console/src/components/agentscope-chat/Bubble/hooks/useHistoryPreload.ts:49`。

```text
距离顶部 > 240px：wasNearStart = false
距离顶部 <= 240px 且 wasNearStart = true：直接返回
允许触发：wasNearStart = true，requesting = true
回调完成：只清理 requesting，不清理 wasNearStart
```

所以等待请求完成、继续向上滚、在顶部附近小幅摆动，都不保证重新加载。
只有确实观察到离开 240px 区域后再进入，才会再次推进。短分页、少量剩余本地卡片、
快速滚轮/触控板滚动、拖动滚动条直接到顶，都会增加暴露这个状态问题的机会。

内容增长也可能使用户在几何上离开了旧区域，但锁仍然保留：

| 步骤 | clientHeight | scrollHeight | scrollTop | 距新顶部 | 结果 |
|---|---:|---:|---:|---:|---|
| 进入预加载区 | 400 | 1000 | -380 | 220 | 加载一次，区域锁为 true |
| 新历史插入，锚点保持 | 400 | 1600 | -380 | 820 | 若无位置检查，区域锁仍为 true |
| 下一个滚动事件直接到新顶部 | 400 | 1600 | -1200 | 0 | 被旧区域锁阻止 |
| 向下滚出区域 | 400 | 1600 | -900 | 300 | 区域锁被清除 |
| 再向上到顶 | 400 | 1600 | -1200 | 0 | 第二次加载 |

这里不要求每次滚动都会失败。如果新增内容后采集到一个区域外的滚动事件，锁会正常清除，
持续向上滚动也能加载；该对照场景已验证。因此问题依赖分页内容高度及事件时序。

## 放大因素一：本地展开与归档请求分两次触发

关键位置：`MessageList/index.tsx:440`。

以本地已有 11 张历史卡片为例：

- 首屏显示最新 10 张。
- 第一次进入预加载区只显示剩余 1 张，然后 return。
- 此时虽然本地历史已经耗尽，代码不会顺势请求归档。
- 如果用户继续上滚而区域锁没有被清除，仍然不会访问历史 API。
- 用户下滚出区域再上滚，才首次请求归档。

本地有 25 张时，至少需要两次本地展开和之后的一次归档触发。
这是逻辑上的触发次数，不一定需要三次手势：能否一次连续上滚完成，取决于每次新内容加入后是否重新观察到了区域外位置。

本地展开既不发请求，也不显示 loading；锚点恢复还会保持原有阅读位置。
因此用户可能感知不到已经发生的小批次展开，只看到真正到顶后不继续出现更早的内容。

## 放大因素二：观察的是容器尺寸，未覆盖全部内容变化

关键位置：`useHistoryPreload.ts:70` 与 `MessageList/index.tsx:466`。

- Hook 监听 `scroll`，并用 `ResizeObserver` 观察滚动容器本身。
- 该容器在 `Bubble/style/list.ts` 中是固定 `height:100%` 的滚动视口。
- 插入历史主要增加 `scrollHeight`，不一定改变被观察容器的盒子尺寸。
- 消息内容变化、可见历史条数变化、本地批次结束，没有直接要求 Hook 重新计算位置。
- 锚点恢复在 layout effect 和下一帧进行，但没有与预加载锁共享一次内容推进后的状态同步。

因此不能依靠容器 ResizeObserver 保证每一次历史插入后都重新开放加载。
锚点恢复的作用是稳定阅读位置；没有证据说明应删除锚点恢复。

## 既有规范与测试为什么没有挡住

`openspec/specs/chat-message-history-loading/spec.md` 明确规定：

- 每次离开并重新进入预加载区显示一批本地消息；
- 本地耗尽后，后续重新进入才请求归档。

这是为了防止短列表一次性拉完所有历史而采取的策略。当前问题包含交互规则与
“第一次到顶即持续取得所需历史”的期望不一致，以及规则实现对位置观察时机过于依赖两部分。

`useHistoryPreload.test.tsx` 和 `MessageList/index.contentOnly.test.tsx` 显式测试了
`-380 → -300 → -380` 的离开再进入路径。集成测试中的 `scrollHeight` 固定为 1000，
并使用模拟 Bubble.List，未模拟真实新增内容导致的滚动范围变化。

已执行：

```bash
cd console
pnpm test:run \
  src/components/agentscope-chat/Bubble/hooks/useHistoryPreload.test.tsx \
  src/components/agentscope-chat/Bubble/hooks/scrollAnchor.test.ts \
  src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/MessageList/index.contentOnly.test.tsx
```

结果：3 个测试文件，34 项通过。

另外使用临时 Vitest 用例调用真实 Hook，4 项通过，确认：

1. 触发过预加载后，到顶及在区域内小幅往返不新增调用，离开区域再回来才新增调用。
2. 内容增高后未观察区域外位置，下一个事件直接到新顶部时无法再次加载。
3. 内容增高后先观察区域外滚动事件，随后到顶可以正常再次加载。
4. 在无 ResizeObserver 的环境中，disabled 时到顶，恢复 enabled 不会主动补触发；下一次 scroll 才触发。

第 4 项只说明缺少直接的状态变化补检查。浏览器存在 ResizeObserver 时，重新 observe 的初始通知可能补偿，
不能将此项单独视为现场根因。测试使用受控 DOM 几何，不等同于已在用户浏览器完成端到端复现。
临时测试已移出源码，保留在 `/private/tmp/history-preload-diagnostic.test.tsx` 供本次会话复查。

## 现场判别方法

应在第一轮上滑就记录以下值，区分“未触发”“本地展开”“请求中”“响应后未继续”：

- `scrollTop/clientHeight/scrollHeight` 与计算出的顶部距离；
- `wasNearStartRef/requestingRef/disabled`；
- `visibleHistoryCount/historicalMessageCount/hasHiddenLocalHistory`；
- `historyLoadState/historyCursorRef/has_more`；
- `/chats/{chat_id}/history` 的首次请求时点与结果。

如果 visibleHistoryCount 已增长但没有历史 API，请先检查本地展开分支和区域锁。
如果历史 API 已发送且一直 pending，才进入网络/后端耗时排查。
如果返回 error，界面提供明确重试；当前代码在 error 状态禁用自动加载，来回滚动本身不负责重试。
如果从未触发过 Hook，还需检查当前滚动节点是否绑定正确，不能直接归因于已置位的区域锁。

## 修复方向与验收标准（基线分析阶段）

1. 保留请求并发锁，把“能否推进下一批”与内容/游标进度关联，不仅依靠离开区域的布尔锁。
2. 本地展开提交、归档插入和锚点恢复完成后重新计算位置，避免遗留旧布局的区域状态。
3. 本地耗尽且用户仍有向上阅读需求时，明确是否同一轮衔接归档；同步修订现有 OpenSpec。
4. 对短页面及无新增可见卡片的响应提供有界补取，检查游标是否推进，保留 exhausted/error 停止条件。
5. 增加真实 Bubble.List 的浏览器验证：普通滚轮、快速触控板、拖动滚动条、短最后一批、
   异步内容增高、连续分页、切换会话、加载失败与无更多历史。

验收重点：只向上滚到当前顶部即可继续获取所需历史，不要求用户人为下滚再上滚；
同时不重复请求、不自动无限拉取、阅读锚点稳定、会话隔离不受影响。
不建议仅增大 240px 阈值或简单删除所有锁，这两者都没有解决内容推进与触发状态同步问题。


## 已实施的修复与回归入口（2026-10-19）

- `useHistoryPreload` 使用当前位置、向上阅读意图和推进结果协调加载，取消 `wasNearStartRef` 区域锁。
- 一轮最多推进 3 个本地批次/归档页；完成两帧布局及 pending 锚点事务后再检查位置，任务锁覆盖全过程。
- 本地最后一批不足 10 条时可衔接归档；空可见页只有游标前进才允许继续。
- 请求失败或游标不前进转入错误状态；手动重试复用同一协调器与请求游标。
- 提供“加载更早消息”处理短列表和预算耗尽。无用户动作时挂载不预取。
- 通过实际 ref 生命周期重绑滚动节点；会话及压缩 generation 隔离旧响应和恢复回调。
- 反向列表底部判断改为 `abs(scrollTop) <= 2`，避免负坐标误判并拉回最新消息。
- 历史插入与实时新增都保持可见消息锚点；程序滚动坐标标记不随恢复事务结束立即丢弃，
  防止延迟 scroll 被误判为用户意图。真实滚动会取消旧锚点恢复。
- 对内容盒子增加单个 ResizeObserver，补偿晚于两帧的 Markdown 尺寸变化；旧阅读锚点失效后不再恢复。

回归命令：

```bash
cd console
pnpm test:run src/components/agentscope-chat/Bubble \
  src/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Chat/MessageList \
  src/pages/Chat/sessionApi
pnpm build
```

结果为 9 个文件、103 项测试通过，生产构建与修改文件定向 ESLint 通过。
真实 Bubble.List/Markdown 浏览器验证覆盖首次向上滚动、本地11/25条、空页3步上限、手动继续、
普通/content-only模式、相同消息数量重挂载、会话切换与实时更新锚点。
API为可控模拟，尚不等同部署环境端到端验收。

后续排查请记录顶部距离、当前可见窗口、游标、historyLoadState、isAdvancing 和锚点 pending 状态，
不再查找已删除的 wasNearStartRef。方案详见 `docs/superpowers/specs/2026-10-19-chat-history-preload-design.md`。
