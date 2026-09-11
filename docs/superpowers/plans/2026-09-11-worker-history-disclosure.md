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

## Pagination spacing follow-up

The user found the event pagination crowded. Keep five-event pages and existing navigation, but use normal-sized controls, a separate count line, and a footer with 16px list separation and wrapping control groups. Scope styling to this pagination only. Verify the normal-size regression, existing page navigation, and rendered 158-event layouts at desktop and narrow widths. Do not alter other paginations, APIs, commit history or existing transfer packages.

Verified: the new size assertion failed against the old mini pagination, then all 21 page/helper tests passed. Browser checks with 158 fixture records at 1440, 1280, 560 and 420px widths confirmed the separate count line, larger controls, wrapping jump input and next page showing records 6-10. TypeScript and scoped ESLint/Prettier checks passed. Temporary preview files and server removed; no commit or package update.

## Failure panel simplification follow-up

Remove only the user-marked static retry information banner and repaired-auth/configuration checkbox. Remove that checkbox's retry prerequisite in Scheduler as well; do not silently submit a fabricated repair confirmation. Preserve the optional legacy request field for old clients, stopping confirmation for unknown/timeout errors, source/manager checks, worker capacity, one-extra-attempt semantics and automatic auth-expiry terminal failure. Test auth/configuration retries without confirmation and retain timeout protection; verify the actual panel. No commit or package update.

Verified: auth/configuration retries failed on the old backend prerequisite before the change. After implementation, 67 backend tests passed; the 18 page tests and all 5 failure-panel cases passed, including retained timeout stopping confirmation. TypeScript, scoped ESLint/Prettier and whitespace checks passed. Real component preview confirmed both marked elements absent and auth-expiry manual retry available after type selection. The new frontend omits confirm_resolved rather than manufacturing a confirmation; the backend keeps the optional legacy field for compatibility. Temporary preview files/server removed.

## Compact Intent priority follow-up

Merge `批内顺位` and `优先依据` into one `批内顺位` column immediately after `租户 / 任务`. Show only the existing one-based rank by default. Reveal priority basis, user/branch ranks, branch identity and heat in a hover/focus tooltip. Preserve server ordering, table filters, pagination and APIs. Test column order and default/user/branch tooltip details; verify the actual table and keyboard access. Reuse existing styles, with no new component, backend work, commit or package update.

Verified: the old two-column order failed the three priority-display cases; the final page/history/failure-panel suite passes 29 tests. Hover and focus are tested independently of tooltip closing animation. TypeScript, scoped ESLint/Prettier and whitespace checks passed. Browser fixtures at 1440x900 and 1280x720 confirmed numeric-only default cells, correct column position, complete tooltip details, and Tab focus switching from user to branch priority. Temporary preview files/tab/server removed; prior uncommitted changes preserved.
