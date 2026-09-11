# Worker history: per-model disclosure and five-row pages

- Keep the existing Worker summary panel and API/time-range semantics.
- Each provider/model row is a keyboard-accessible, initially collapsed disclosure. Render only that model's chart on expansion; remove the chart's visible heading, explanatory paragraph and legend, retaining axes, tooltip and zoom.
- Keep the combined interval event list below the model rows, showing five events per page. Preserve event detail expansion and reset to page one when the dataset changes.
- Scope: `CronBatchDispatch/index.tsx`, `WorkerHistoryChart.tsx`, `index.module.less`, page tests and this plan. Leave unrelated notification-worker edits untouched.

## Verification

1. Update tests first for five-row pages, last-page remainder, dataset reset, collapsed chart mounting and provider/model isolation.
2. Run GitNexus impact before editing the existing components, then implement using existing styles/components.
3. Run targeted Vitest, TypeScript, ESLint and Prettier checks. Inspect the actual UI with isolated fixture data at narrow and desktop widths; exercise keyboard expansion and pagination.
4. No commit, packaging, dependency additions or backend changes in this task.

Superpowers tools are unavailable; repository instructions require sequential main-thread work. This task uses a local plan, regression tests and self-review, not independent subagent reviews.

## Verified

- Three changed-behavior tests failed before implementation as expected; the complete targeted suite now passes: 23 tests across the page, worker-history helper and failure panel.
- TypeScript, targeted ESLint and Prettier checks passed. Existing jsdom computed-style and Ant Design deprecation warnings remain visible.
- Real components with isolated 129-event/two-model fixtures verified in the in-app browser at 1440x900 and 1280x720: default collapse, single-model chart, no visible chart heading/description/legend, keyboard toggle, and page two showing records 6-10.
- Removed duplicate single-record navigation; only the five-record pagination remains. Model row keys use source/provider/model, so current-capacity record ID changes do not reset disclosure state.
- Temporary preview files and server were removed/stopped. No backend changes, commits or package regeneration.
