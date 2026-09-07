---
date: 2026-08-21
topic: wplus-stage-incremental-artifacts
status: draft-for-review
decisions: confirmed-by-author-2026-08-21
supersedes: null
related:
  - 0013-wplus-sop-uses-persisted-session-and-structured-envelope
  - 2026-07-17-wplus-sop-workspace-design
  - 2026-08-21-wplus-stage-incremental-artifacts-requirements
---

# W+ SOP 环节级增量产物 — 技术方案

## 1. 背景与目标

需求文档（`docs/brainstorms/2026-08-21-wplus-stage-incremental-artifacts-requirements.md`）要求把 W+ SOP 的产物生成从"全部环节结束后一次性组装"改为"环节级增量"：

- 每个环节预跑成功后立即生成该环节的 JSON / Markdown / HTML 三格式报告（R1–R3）；
- 用户围绕最新报告反馈、澄清、重跑，每次成功重跑形成新报告版本，历史版本只读保留（R4–R5）；
- 用户确认环节后锁定接受版本并纳入累计 SOP，立即刷新累计三格式预览（R6–R9）；
- 最后一个环节确认后，基于最新累计生成带整体摘要与导航的最终三格式结果（R10–R12）。

本方案沿用现有架构的**持久化会话 + 结构化事件信封 + 专用工作台**（ADR-0013）作为唯一事实来源，产物全部由**已确认环节快照确定性组装**，不引入第二套状态源。

## 2. 术语

在 CONTEXT.md 中新增以下术语（与现有 `W+ SOP Result Bundle` 明确区分）：

| 术语 | 定义 | 与现有概念的关系 |
|---|---|---|
| 环节报告（Stage Report） | 某一环节在某次成功预跑后生成的三格式（JSON/MD/HTML）产物，携带内容版本标识与校验证据 | 是 `W+ SOP Result Bundle` 的环节级前置形态 |
| 报告版本（Stage Report Version） | 该环节在当前会话 revision 下的成功预跑序号；修订（revision）后重新计数 | 与 Answer Revision 的 revision 是两个维度，完整标识为 `(revision, report_no)` |
| 累计 SOP（Cumulative SOP） | 截至当前已确认环节（按环节顺序）的三格式组装结果，不含未确认或历史版本 | 最终结果的内容基础；可预览、可下载 |
| 整体结果（Final Result） | 最后一个环节确认后，基于最新累计生成的整体三格式 + 摘要/目录/顺序 | 即现有 `FinalSopResult`，内容基础由"全量重生成"改为"累计组装" |

## 3. 总体设计原则（先定决策，再谈实现）

### D1. 内容版本锚定：环节锁定时刻 = 内容版本

单一事实来源是会话投影中的**已确认环节快照**（stage_id + report_version + sha256）。环节报告、累计 SOP、最终结果都只是快照的确定性渲染结果：

- 渲染是**纯函数**：输入 = 已确认快照集合 + 壳参数（标题/目录/摘要），输出 = 三格式文本。
- 任何重跑、失败恢复、重复生成都不会改变已锁定快照的 sha256，因此不会产生内容漂移（回应需求文档 Q1）。
- 环节报告文本**落盘即锁定语义**：同一 (stage_id, report_version) 的内容一经生成不可变；新版本 = 新文件，不覆盖旧文件。

### D2. 确认与累计刷新是同一事务边界

`STAGE_CONFIRMED` 事件提交时，在同一个 store commit 内完成"锁定接受版本 + 重算累计 SOP"：

- 累计刷新成功 → 会话进入下一环节（或最后环节时进入最终组装）。
- 累计刷新失败 → 该 commit 不成立，会话停留在可恢复失败态，**不进入下一环节**（回应 Q3；R7 顺序边界不被破坏）。
- 累计重算是确定性的，因此 Retry 天然幂等，可直接复用现有 `retry_current_turn` 语义。

### D3. 报告版本号与 Answer Revision 联动

- 报告版本标识 = `(revision, report_no)`：`report_no` 为该环节在当前 revision 下的成功预跑序号（1 起）。
- `REVISION_APPLIED` 只允许修改当前尚未确认环节的回答；已确认环节及其锁定报告永久只读。当前环节修订后 `revision` 递增、旧报告保留为历史，新 revision 的 `report_no` 从 1 重新计数。
- 前端展示用 `report_no`（"v2"），审计与存储用完整标识 `(revision, report_no)`（回应需求 Q1 的溯源需求）。

### D4. 环节级 vs 整体级字段边界（回应 Q2）

环节报告只包含**环节局部内容**（R2 八类：目标、适用范围、已确认输入、执行步骤、能力/操作、结果结构、异常与限制、本轮预跑证据）。以下内容**只能在整体级生成**，禁止下放到环节级，从源头杜绝最终组装时的静默改写：

- 整体标题、封面、目录、跨环节摘要、完整执行顺序（R11）；
- 跨环节的汇总统计（如总步骤数、累计事实去重视图）。

跨环节摘要的生成规则：**只引用已锁定环节内容中已存在的事实**，摘要字段采用白名单映射（每个环节报告的既定字段 → 摘要模板插槽），不允许自由改写。

### 3.1 已确认决策（2026-08-21）

| # | 决策 | 结论 |
|---|---|---|
| A1 | 渲染/校验脚本宿主 | 运行时位置：`{workspace}/skills/wplus-sop-miner/scripts/`（`render_md.py` / `render_sop.py` / `validate_sop.py`，依赖同目录 `privacy.py`）；与 `resolve_effective_skill_dir(workspace_dir, "wplus-sop-miner")` 的解析一致；环节级/累计级渲染为同一批脚本的参数化复用，不新建脚本族。本机 `Desktop/wplus-sop-suite` 仅为开发备份源，非运行时位置。**产物一律输出到 `{workspace}/.wplus/` 产物目录，技能目录（`{workspace}/skills/`）保持只读，不落任何产物**；`Desktop/wplus-sop-suite/skills/wplus-sop-miner/`（SKILL.md / references / scripts）作为技能内容**实现参考蓝本**，运行时仍以 `{workspace}/skills/wplus-sop-miner/` 为准 |
| A2 | 累计 JSON 与最终结构 | **同构**：累计 `sop_spec` 与最终 `sop_spec.json` 同一结构（`schema_version/title/request_summary/stages[]` 等），已确认环节填充完整执行内容，未确认环节不出现；最终结果 = 累计 + 整体壳层 |
| A3 | 环节级 `example_result_html` | 不生成；`example_result_html` 仅整体级保留（`FinalSopResult` 四件套语义不变） |
| A4 | 新状态 UI | `GeneratingStageReport` / `RefreshingCumulative` 的呈现参考既有 `GeneratingTrial` / `ExecutingTrial` / `FinalizingOutputs` 的处理：生命周期进度行 + 状态标签，不新增独立复杂视图 |
| A5 | 校验器策略 | 新建 `validate_stage_sop.py` / `validate_cumulative_sop.py`（允许部分确认、未完成状态），不直接复用 `validate_sop.py`——其强制 `2 <= len(stages) <= 4` 且 `status=complete` 时所有 stage 必须 `complete` + `user_confirmed`，与环节级/累计级产物冲突；最终整体仍走完整校验 |
| A6 | 环节级子集字段基线 | 以 `validate_sop.py` 的 stage 必填字段（`id/name/status/verification_mode/entry_point/data_scope/decision_logic/output/next_action/trial_notes/execution`）为基线映射 R2 八类；字段映射清单在 M3 前产出 |

## 4. 内容模型（models.py 扩展）

### 4.1 新增领域模型

```python
class StageReportArtifact(StrictModel):
    artifact_id: Literal["stage_sop_json", "stage_sop_md", "stage_sop_html"]
    name: Literal["stage_sop.json", "stage_sop.md", "stage_sop.html"]
    static_file_name: str          # basename，无路径分隔符
    static_url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    copied_by: Literal["copy_file_to_static"]

class StageReportValidationEvidence(StrictModel):
    schema_validator: Literal["scripts/validate_stage_sop.py"]
    schema_exit_code: Literal[0]
    renderers: tuple[Literal["scripts/render_stage_md.py"], Literal["scripts/render_stage_sop.py"]]

class StageReport(StrictModel):
    stage_id: str
    report_no: int = Field(ge=1)          # 当前 revision 内序号
    revision: int = Field(ge=0)           # 所属会话 revision
    created_at: datetime
    superseded_by: int | None = None      # 新版本 report_no；None 表示最新
    artifacts: list[StageReportArtifact]  # 三格式齐全且校验通过
    validation: StageReportValidationEvidence

class ConfirmedStageSnapshot(StrictModel):
    stage_id: str
    report_no: int
    revision: int
    artifact_sha256: str                  # 锁定内容指纹（以 stage_sop.json 为准）
    confirmed_at: datetime

class CumulativePreview(StrictModel):
    preview_version: int                  # 每次确认 +1
    stage_order: list[str]                # 已确认环节 id 的有序列表
    snapshots: list[ConfirmedStageSnapshot]
    artifacts: list[StageReportArtifact]  # cumulative.json / cumulative.md / cumulative.html
    rendered_sha256: dict[str, str]       # 三格式各自指纹
```

### 4.2 会话投影新增字段（SessionRecord.projection）

- `stage_reports: list[StageReport]` — 全部环节报告历史（审计只读）。
- `confirmed_snapshots: list[ConfirmedStageSnapshot]` — 按环节顺序排列的已确认快照。
- `cumulative_preview: CumulativePreview | None` — 最近一次成功刷新的累计预览。

### 4.3 既有模型不动

- `FinalSopResult` 保持四件套（sop_spec / readable_sop / html / example_result_html），但其组装输入从"Agent 自行汇总"改为 `confirmed_snapshots + cumulative_preview` 的确定性组装；`example_result_html` 仅整体级生成（决策 A3）。
- `StageStatus` 不变：`CONFIRMED / INVALIDATED` 语义已覆盖需求（R6 锁定后不可重开 = 已 CONFIRMED 的环节不再提供任何写操作）。

## 5. 状态机扩展

### 5.1 新增会话状态

```python
class SessionState(str, Enum):
    ...
    GENERATING_STAGE_REPORT = "GeneratingStageReport"   # 预跑成功 → 生成环节三格式
    REFRESHING_CUMULATIVE = "RefreshingCumulative"      # 确认锁定 → 重算累计 SOP
```

### 5.2 主路径变化

```text
… → ExecutingTrial → TRIAL_EXECUTION_COMPLETED
  → GeneratingStageReport          ← 新增：生成三格式环节报告
  → (报告校验失败 → RecoverableFailure，保留上一稳定版本，可 Retry)
  → AwaitingStageConfirmation      ← 三格式均 validated 才允许确认（R3）
      ├─ 补充澄清 → GeneratingQuestions
      ├─ 按反馈重新预跑 → GeneratingTrial → 生成新报告版本
      └─ 用户 confirm_stage
  → RefreshingCumulative           ← 新增：锁定 + 重算累计（同一事务）
  → (刷新失败 → RecoverableFailure，不进入下一环节)
  → GeneratingQuestions（下一环节）或 FinalizingOutputs（最后环节）
```

- 最后环节确认且累计刷新成功后，直接进入现有 `FinalizingOutputs → OutputReview`，但最终结果由累计组装（R10）。
- `confirm_stage` 启动的所属 Chat Agent 回合只负责累计刷新，事件序列固定为 `cumulative_refreshed`。该事件持久化后，服务端拒绝旧 run 提交下一环节或最终结果事件；只有旧 run 完整结束，完成回调才原子结算它并认领一个新的 Agent run：非最后环节的新 run 进入 `GeneratingQuestions`，最后环节的新 run 进入 `FinalizingOutputs`。进程在结算与认领后、实际启动前中断时，新 run 仍以 `CLAIMED` 持久化并可由孤儿恢复处理。
- `OutputReview` 确认（`confirm_outputs`）后仍走现有 `MemoryReview`（R12：记忆授权顺序不变）。

### 5.3 状态机约束

- 只有 `stage_reports` 中当前环节存在最新可验收版本（三格式 sha256 齐全）时，才允许 `confirm_stage` 命令（R3 的强制点）。
- `confirm_stage` 的 payload 必须携带 `expected_report_no`，与前端所见版本一致，避免确认到被取代的旧版本（配合现有 `expected_state_version` 乐观并发）。

## 6. 事件协议扩展（EventKind + emit_wplus_sop_event）

新增事件类型（注册进现有协议白名单，`event_key` 保持幂等稳定）：

| 事件 | 触发 | event_key 规则 | payload 要点 |
|---|---|---|---|
| `stage_report_generated` | 环节预跑成功、三格式生成并校验通过 | `stage:{stage_id}:report:{revision}:{report_no}` | stage_id、report_no、revision、三格式静态文件引用 + sha256、validation 证据 |
| `stage_report_generation_failed` | 生成或校验失败 | `stage:{stage_id}:attempt:{run_id}` | 失败码、最后稳定版本引用（若有） |
| `cumulative_refreshed` | 环节确认且累计刷新成功 | `cumulative:{preview_version}` | stage_order、snapshots 摘要、三格式引用 + 指纹 |
| `stage_confirmed`（既有） | 不变 | 不变 | payload 增加 `confirmed_report_no` |

`STAGE_REPORT_GENERATED` 的 Agent 侧行为：Miner 在预跑成功后调用渲染脚本生成三格式 → 写入静态目录 → `emit_wplus_sop_event` 提交。渲染脚本是**确定性、无 LLM** 的（沿用现有 `scripts/render_md.py / render_sop.py / validate_sop.py` 同族脚本的参数化版本；脚本所在位置以 `FinalValidationEvidence` 现有引用为准，需在实现时确认其宿主目录——当前工作区 `scripts/` 与 `skills/wplus-sop-miner/` 均未找到，列为实现前必查项）。

## 7. 存储与文件布局

**产物目录与技能目录严格隔离**：渲染/校验脚本从 `{workspace}/skills/wplus-sop-miner/scripts/` 只读执行；全部环节报告、累计 SOP、最终产物一律写入 `{workspace}/.wplus/` 产物目录，技能目录不落任何产物（决策 A1）。

沿用现有 JSON 文件 store（`WPlusSopStore`、`commit_event`、乐观 `state_version` 并发），新增文件命名空间：

```text
<workspace>/.wplus/sessions/<session_id>/
  stage-reports/<stage_id>/
    r<revision>-v<report_no>/stage_sop.{json,md,html}
  cumulative/
    v<preview_version>/cumulative.{json,md,html}
```

- 旧版本文件**只增不删**（R5 审计）。
- `.wplus` 只是产物 staging/cache，不是 Session 恢复源。每次新澄清使用平台生成的全新 `sop_session_id` 目录；运行时和 Miner 不得扫描其他 Session、读取跨 Session 的固定 `latest` 文件或从旧产物重建新会话。
- 通过现有 `copy_file_to_static` 机制暴露 `static_url` 供前端预览/下载；sha256 随工件记录。
- store 的 `commit_event` 在 `STAGE_CONFIRMED` 时校验：确认的 report_no 必须存在且为最新；随后在**同一文件写事务**内写入 `confirmed_snapshots` 与 `cumulative_preview`（D2 事务边界）。

## 8. 渲染与校验

| 产物 | 输入 | 渲染方式 | 校验 |
|---|---|---|---|
| 环节报告 | 该环节锁定输入 + 本轮预跑结果/证据 | 复用 `{workspace}/skills/wplus-sop-miner/scripts/render_md.py / render_sop.py`（环节级子集 spec + 本轮预跑证据） | `validate_stage_sop.py`（新建，允许部分确认；schema 校验 + 三格式一致性） |
| 累计 SOP | confirmed_snapshots 有序集合 | 确定性拼接 + 壳层（整体标题/目录/顺序） | 校验仅含已确认环节、顺序一致 |
| 最终结果 | 最新 cumulative_preview + 整体摘要 | 现有 render 流程，内容基础 = 累计 | `validate_cumulative_sop.py`（新建）+ 快照一致性校验（最终与累计逐环节 sha256 比对）；整体交付前跑完整 `validate_sop.py` |

一致性校验规则（回应 R10/R12）：最终三格式与累计逐环节比对 `artifact_sha256`，任何差异即失败，不静默修复。

**校验器策略（决策 A5）**：`validate_sop.py` 强制 `2 <= len(stages) <= 4`，且 `status=complete` 时所有 stage 必须 `complete` + `verification_mode=user_confirmed`——这对"累计只含 1 个已确认环节"和"环节报告处于未完成状态"均不成立，因此新建：

- `validate_stage_sop.py`：校验单个环节报告（允许部分确认、`status` 可为进行中；三格式内容语义一致）；
- `validate_cumulative_sop.py`：校验累计（仅含已确认环节、顺序一致、引用版本 sha256 存在）；
- 最终整体交付仍执行完整 `validate_sop.py`；若人工确认环节数超过 4（ADR-0013 允许），需评估放宽 `validate_sop.py` 上限或拆分为独立问题。

## 9. 后端落点（按文件）

| 文件 | 改动 |
|---|---|
| `src/swe/app/wplus_sop/models.py` | 新增 §4.1 模型、§5.1 状态、§6 事件类型与 payload |
| `src/swe/app/wplus_sop/store.py` | projection 新增字段；`commit_event` 增加 STAGE_CONFIRMED 事务逻辑与 stage_report 相关校验 |
| `src/swe/app/wplus_sop/service.py` | 新增 `generate_stage_report` / `refresh_cumulative` / `assemble_final_from_cumulative`；`confirm_stage` 分支扩展（D2）；`serialize_session` 输出 `stage_reports`、`cumulative_preview` |
| `src/swe/app/wplus_sop/runtime.py` | Agent 回合内新事件的提交路径与状态推进 |
| `src/swe/agents/tools/emit_wplus_sop_event.py` | 注册新 kind 与 payload 校验 |
| 渲染/校验脚本 | 新建 `validate_stage_sop.py` / `validate_cumulative_sop.py`（允许部分确认）；渲染复用 `{workspace}/skills/wplus-sop-miner/scripts/render_md.py / render_sop.py`（决策 A5） |
| wplus-sop-miner 技能契约 | `SKILL.md` 与 `references/`（stage-workflow.md、output-contract.md、sop-schema.json）同步扩展环节报告协议（见 §9.1）；内容以 `Desktop/wplus-sop-suite` 为参考蓝本（决策 A1） |

### 9.1 技能契约落点（wplus-sop-miner）

Miner 是 Agent 驱动，技能契约必须与新事件协议同步（参考蓝本：`Desktop/wplus-sop-suite/skills/wplus-sop-miner/`）：

- `SKILL.md`：在"强制状态机"章节新增 `trial_running → stage_report_generated → awaiting_stage_confirmation` 转换；声明每次预跑成功后生成三格式环节报告并提交 `stage_report_generated` 事件（R1–R3）；确认环节后不自行生成最终产物——最终由平台按累计组装（R10）。
- `references/stage-workflow.md`：状态转换表补充"预跑成功 → 生成环节报告 → 反馈或确认"；反馈重跑时报告版本递增规则（D3）。
- `references/output-contract.md`：新增"环节级输出"章节（三格式、产物写入 `{workspace}/.wplus/`、校验用 `validate_stage_sop.py`）；整体级四件套约定保留，但内容基础 = 累计（A2）。
- `references/sop-schema.json` / `state-schema.json`：补充环节报告与累计预览的 JSON 结构（与 sop-spec 同构，A2）。

## 10. 前端落点

### 10.1 `console/src/api/types/wplusSop.ts`

- `WPlusSopState` 增加 `GeneratingStageReport` / `RefreshingCumulative`。
- 新增：
  - `WPlusSopStageReport`（report_no、superseded_by、artifacts、preview）
  - `WPlusSopCumulativePreview`（preview_version、stage_order、artifacts、sha256）
- `WPlusSopSession` 增加 `stage_reports`、`cumulative_preview`；`artifacts` 语义不变（仍表示最终四件套）。
- `WPlusSopCommandType` 不变（`confirm_stage` 已存在），payload 增加 `expected_report_no`。

### 10.2 `console/src/api/modules/wplusSop.ts`

- 跟随 session 投影扩展；阶段报告按 `stage_id + revision + report_no + artifact_id`、累计预览按 `preview_version + artifact_id` 读取，不能只使用会在不同版本间重复的 `artifact_id`。
- 阶段与累计产物的预览和下载都走 Session ownership 校验的 HTTP 路由，由请求携带现有认证头直接读取文件；前端不直接读取持久公开的 static URL。

### 10.3 工作台页面（`console/src/pages/WPlusSopWorkspace/index.tsx`，路由 `/wplus-sop/:sessionId`）

落点文件：`console/src/pages/WPlusSopWorkspace/index.tsx`（主组件，含报告版本切换与累计预览面板；路由已在 `console/src/layouts/MainLayout/index.tsx` 注册）、`console/src/api/types/wplusSop.ts`（类型）、`console/src/api/modules/wplusSop.ts`（API 调用）。

- **环节报告面板**：只在 `AwaitingStageConfirmation` 展示，默认加载最新版本；版本切换器只读加载历史版本（R5）。最新版本提供“补充澄清”“按反馈重新预跑”“确认并锁定”三路操作；确认后不再开放反馈或修订。
- **累计 SOP 预览面板**：仅在 `AwaitingStageConfirmation` 与当前阶段报告一起展示，作为此前已确认环节的辅助上下文；`GeneratingQuestions`、`AwaitingAnswer`、预跑、报告生成和累计刷新期间均不渲染累计正文。
- **确认按钮门控**：仅当当前环节最新报告三格式全部 `validated` 且无失败时可用（R3 的前端强制）。
- **最终结果视图**：复用现有 OutputReview，新增"与累计逐环节一致"的状态提示（R10 的可见化）。
- **状态呈现**：`GeneratingStageReport` / `RefreshingCumulative` 参考 `GeneratingTrial` / `ExecutingTrial` / `FinalizingOutputs` 的既有呈现（生命周期进度行 + 状态标签），不新增独立复杂视图（决策 A4）。
- **退出入口**：正常工作台顶栏不展示“保存并退出”；返回所属 Chat 只执行路由跳转，不自动暂停或修改 Session。

## 11. 失败恢复与一致性

- **报告生成失败**（预跑成功但渲染/校验失败）：不产生可确认版本；复用 `RecoverableFailure`：保留该环节最后一个稳定报告版本为只读兜底，提供 `retry_current_turn`（幂等，不追加重复审计记录）。
- **累计刷新失败**：会话处于"已锁定待刷新"恢复态（`RecoverableFailure` 变体，附失败码 `cumulative_refresh_failed`），不得进入下一环节；Retry 重算（确定性幂等）。
- **Agent 提前结束**：任何生成态（包括 `GeneratingStageReport` 和 `RefreshingCumulative`）在所属 Agent 回合结束时仍未到达所需结构化边界，都必须把 run 标记为失败并进入 `RecoverableFailure`，保留原生成态作为 `resume_state`；不得把 run 标记为完成后让会话永久停留在生成态。
- **事件幂等**：`event_key` 稳定（§6），重发不产生重复版本。
- **状态机合法性**：新增状态加入 `_validate_agent_event_state` 与前端状态机映射，防止非法迁移。

## 12. 兼容与迁移

- 旧会话（`final_result` 已存在、无 `stage_reports`）按原逻辑渲染，新字段为可选；序列化时缺省为 `[]` / `null`。
- `StageStatus` 语义不变，存量 `CONFIRMED` 环节自动视为"已锁定快照"（`report_no=1, revision=0` 的合成快照），保证旧会话可继续确认后续环节并进入新流程。
- 新增字段全部走 `StrictModel extra="forbid"` 的既有协议，前端类型同步升级，避免静默不兼容。

## 13. 测试计划

- **store 层**：`commit_event` 的 STAGE_CONFIRMED 事务（成功/累计失败回滚/并发 state_version 冲突）；报告版本只增不改。
- **service 层**：confirm_stage 门控（无有效版本拒绝）；当前未确认环节 revision 重计数；已确认环节修订拒绝；最后环节 → 最终组装；累计与最终 sha256 一致性。
- **渲染层**：环节/累计/最终三格式同一内容语义（回应 R1）；摘要白名单映射不越界。
- **前端**：类型契约测试（现有 `wplusSop.test.ts` 模式）、版本切换 UI、确认门控状态。
- **产物读取**：覆盖阶段历史版本与最新版本、累计版本、跨 Session ownership 拒绝、未知版本 404，以及页面内 JSON / Markdown / HTML 加载与失败状态；测试必须实际点击阶段预览和下载，不能只断言按钮存在。
- **端到端**：对照 AE1–AE5 五条验收示例逐条验证（新增一条：确认后累计刷新失败不得进入下一环节）。

## 14. 里程碑

| 里程碑 | 内容 | 出口标准 |
|---|---|---|
| M1 数据模型 | models/store 扩展 + 迁移 | store 单测通过，旧会话可读 | ✅ 2026-08-21 |
| M2 状态机与事件 | service/runtime/emit tool 扩展 | 状态机单测通过，AE1–AE3 通过 | ✅ 2026-08-21 |
| M3 渲染脚本 | 参数化复用 `{workspace}/skills/wplus-sop-miner/scripts/`（render_md / render_sop / validate_sop）实现环节级/累计级确定性渲染与校验 | 三格式一致性校验通过 | ✅ 2026-08-21 |
| M4 前端工作台 | 报告版本、累计预览、确认门控 | 类型测试 + UI 走查 | ✅ 2026-08-21 |
| M5 端到端验证 | AE1–AE5 + 刷新失败场景 | 全量回归通过 | ✅ 2026-08-21 |

实施备注：store 层经实现确认无需改动（`commit_event` 的 `projection_changes` 通用机制天然支持新字段，旧会话经 Pydantic 默认值向后兼容）；"确认与累计刷新"以两阶段提交落地（`STAGE_CONFIRMED` 锁定 → `CUMULATIVE_REFRESHED` 写入），刷新失败停留在可恢复态、不进入下一环节。

## 15. 决策记录与剩余事项

### 已确认决策（2026-08-21）

- A1 渲染/校验脚本宿主：运行时 `{workspace}/skills/wplus-sop-miner/scripts/`（`render_md.py` / `render_sop.py` / `validate_sop.py` + `privacy.py`），与 `resolve_effective_skill_dir` 解析一致；环节级/累计级为参数化复用；**产物一律输出到 `{workspace}/.wplus/`，技能目录保持只读**。本机 `Desktop/wplus-sop-suite` 仅为开发备份源。
- A2 累计 JSON 与最终 `sop_spec.json` 同构：累计 = 已确认环节的完整填充，最终 = 累计 + 整体壳层（标题/目录/跨环节摘要/顺序）。
- A3 环节级不生成 `example_result_html`，仅整体级保留。
- A4 新状态 UI 参考既有 `GeneratingTrial` / `ExecutingTrial` / `FinalizingOutputs` 呈现。
- A5 校验器策略：新建 `validate_stage_sop.py` / `validate_cumulative_sop.py`（允许部分确认、未完成状态），不直接复用 `validate_sop.py`（其强制 2–4 个 stage 且 complete 时全 stage complete + user_confirmed，与环节/累计产物冲突）；最终整体仍走完整校验。
- A6 环节级子集字段基线：`validate_sop.py` 的 stage 必填字段（id/name/status/verification_mode/entry_point/data_scope/decision_logic/output/next_action/trial_notes/execution）映射 R2 八类；映射清单在 M3 前产出。
- 已确认：`copy_file_to_static` 沿用现有机制（原核对项 B4）；技能内容以 `Desktop/wplus-sop-suite/skills/wplus-sop-miner/` 为参考蓝本（原核对项 B1 的内容侧）。

### 范围外（外部责任）

- 运行时 `{workspace}/skills/wplus-sop-miner/` 的技能部署/上传由用户负责（不在本方案实现范围）；本方案以运行时该目录已包含完整技能（SKILL.md / references / scripts）为前提，内容参考蓝本为 `Desktop/wplus-sop-suite/skills/wplus-sop-miner/`。
