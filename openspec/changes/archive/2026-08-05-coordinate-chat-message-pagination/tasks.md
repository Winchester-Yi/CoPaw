## 1. Baseline And Regression Tests

- [x] 1.1 Run GitNexus impact analysis for the MessageList, BubbleList, history preload, and chat history API symbols; record direct consumers and warn before implementation if risk is HIGH or CRITICAL.
- [x] 1.2 Add failing focused tests for an initial 10-card historical window, 10-card local reveals, immediately visible live cards, and no archive request while local cards remain hidden.
- [x] 1.3 Add failing focused tests for `limit=20`, full archive-page rendering, cursor continuation, immediate terminal state on `has_more=false`, same-cursor retry, and stale-result rejection.
- [x] 1.4 Add failing scroll tests proving that local reveal and archive insertion preserve the visible message anchor and require preload-zone exit/re-entry between actions.

## 2. Progressive History Orchestration

- [x] 2.1 Implement MessageList-owned current-history visibility state with an initial and incremental batch size of 10 while keeping BubbleList local pagination disabled.
- [x] 2.2 Route each preload trigger to exactly one action: reveal a local batch first, otherwise request the current archive cursor with `limit=20`, otherwise remain terminal.
- [x] 2.3 Render each unseen archive response in full, preserve existing deduplication and generation guards, and enter the terminal state immediately when `has_more=false`.
- [x] 2.4 Reuse visible-message anchor restoration for local reveals and archive inserts, reset pagination only on resolved chat identity changes, and preserve immediate live-message visibility.
- [x] 2.5 Preserve compaction refresh integrity by treating refreshed persisted snapshots as historical, keeping current-reader state stable, and preventing stale compaction or archive results from changing the active chat.

## 3. History Status Presentation

- [x] 3.1 Add component tests for loading, error, retry, retry-loading/disabled, terminal, keyboard focus, accessible names, and live-region semantics.
- [x] 3.2 Replace the inline history status styles with a compact Conversation Workspace status component using existing semantic theme tokens and a stable footprint.
- [x] 3.3 Implement the subtle error surface, error icon, concise “历史消息加载失败” copy, low-emphasis retry action, network-only loading state, and quiet terminal state without introducing a large card or artificial delay.
- [x] 3.4 Verify that normal and content-only chat details share the same behavior and presentation without changing content-only permissions or omitted surfaces.

## 4. Verification And Scope Review

- [x] 4.1 Run the MessageList, history preload, BubbleList, scroll-anchor, session-context, and session API focused test suites.
- [x] 4.2 Run Console lint, TypeScript/build checks, formatting validation, and `git diff --check`; fix only issues caused by this change.
- [x] 4.3 Exercise normal and content-only chat in the browser with short, long, loading, error/retry, final-page, live-stream, compaction, and rapid-session-switch scenarios; inspect representative 1280x720, 1440x900, and 1920x1080 layouts.
- [x] 4.4 Run the Impeccable detector/polish review on the changed status UI and reconcile valid findings against `console/DESIGN.md`.
- [x] 4.5 Run GitNexus `detect_changes` for the complete diff, confirm only expected chat-detail flows are affected, and report any verification that could not be completed.
