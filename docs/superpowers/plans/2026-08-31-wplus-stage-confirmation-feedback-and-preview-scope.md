# W+ SOP 阶段确认反馈与预览显隐计划

## 目标

补全阶段 SOP 报告生成后的反馈闭环，并把中间 SOP 正文限制在阶段确认页面展示。已确认环节继续保持锁定，只在最终 `OutputReview` 展示整体结果。

## 行为边界

- `AwaitingTrialFeedback` 继续处理预跑结果反馈。
- `AwaitingStageConfirmation` 展示当前阶段 SOP、历史版本、反馈输入和三路操作：补充澄清、按反馈重新预跑、确认锁定。
- 补充澄清返回 `GeneratingQuestions`；重新预跑返回 `GeneratingTrial`，成功后生成递增报告版本。
- `CONFIRMED` 阶段不能通过历史答案修订重新打开。
- 累计产物继续在后端生成和持久化，但正文只在 `AwaitingStageConfirmation` 展示。
- 工作台顶栏移除“保存并退出”，普通返回 Chat 不改变 Session 状态。
- `.wplus` 旧产物按 `sop_session_id` 隔离，不参与新 Session 初始化；本次不实施历史产物清理。

## TDD 步骤

1. 在 `tests/unit/app/wplus_sop/test_service.py` 增加阶段确认页反馈分流和已确认阶段修订拒绝测试，先验证失败。
2. 在 `console/src/pages/WPlusSopWorkspace/index.test.tsx` 增加阶段 SOP 反馈按钮、澄清/重跑命令，以及非确认状态隐藏累计正文测试，先验证失败。
3. 最小修改 `src/swe/app/wplus_sop/service.py`：为阶段报告反馈增加 clarify/rerun 分流，并限制 `revise_answer` 只能操作当前未确认阶段。
4. 最小修改 `console/src/pages/WPlusSopWorkspace/index.tsx`：在确认页加入反馈操作，把累计面板移回确认分支。
5. 更新相关旧测试，运行聚焦 pytest、Vitest、TypeScript、ESLint、Prettier 和 `git diff --check`。

## 验收

- 用户在阶段 SOP 确认页可以补充澄清或重新预跑，并得到新报告版本。
- 用户确认阶段后不能再通过历史回答修订重开该阶段。
- 澄清、预跑、生成报告和刷新累计期间不展示阶段或累计 SOP 正文。
- 阶段确认页与最终结果页仍可正常预览 JSON、Markdown 和 HTML。
