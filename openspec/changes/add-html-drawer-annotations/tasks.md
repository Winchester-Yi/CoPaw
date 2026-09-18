## 1. Preflight and contracts

- [x] 1.1 Run GitNexus upstream impact analysis for every existing frontend and backend symbol selected for modification, record direct callers/processes, and stop for user review if any result is HIGH or CRITICAL.
- [x] 1.2 Add shared TypeScript annotation/source/output contract types and Python request models for `document_annotations` schema version 1 with bounded field constants.
- [x] 1.3 Add contract fixtures covering a source-resolvable target, a JavaScript runtime-generated target, multiple comments, and an unresolved target report.

## 2. Backend request validation and model context

- [x] 2.1 Write failing pytest cases for valid same-turn HTML annotation requests, absent optional fields, unsupported schema versions, oversize bundles, and malformed target evidence.
- [x] 2.2 Write failing pytest cases for attachment mismatch, non-HTML input, digest conflict, missing file, path traversal, and cross-workspace/cross-tenant source references.
- [x] 2.3 Implement `document_annotations` extraction in the Console chat request path without changing ordinary requests that omit the field.
- [x] 2.4 Implement same-turn file-part matching, workspace-media containment, server-side SHA-256 verification, extension checks, and bounded payload validation.
- [x] 2.5 Write failing tests for hidden annotation directive escaping, document-data trust language, resolved source paths, output requirements, and display-history redaction.
- [x] 2.6 Implement a typed document-annotation directive and append it through the existing trusted hidden-context lifecycle.
- [ ] 2.7 Run focused Console router and runner pytest suites and resolve only failures introduced by this change.

## 3. Revised HTML publication contract

- [x] 3.1 Write failing tool tests for valid new HTML publication, source overwrite rejection, workspace escape rejection, empty/non-HTML output, and leftover `data-copaw-*` annotation instrumentation.
- [x] 3.2 Implement a narrow `publish_annotated_html` tool or validation wrapper that verifies the source/output boundary and delegates successful publication to the existing scoped static-file logic.
- [x] 3.3 Add applied/unresolved annotation ID reporting and structural script/resource preservation checks to the publication result without attempting visual-semantic judgment.
- [x] 3.4 Register the publication tool for the main Agent only where annotated revision context is valid, and add configuration/tool-render compatibility tests.
- [x] 3.5 Add model-facing instruction tests requiring canonical-source edits, minimal unrelated changes, preserved JavaScript behavior, runtime-override fallback, a distinct output file, and no false success when publication is absent.

## 4. Frontend annotation core

- [x] 4.1 Write unit tests for source digest binding, stable-attribute preference, structural selector construction, text-quote bounds, rendered excerpt bounds, computed-style whitelisting, and runtime-generated target classification.
- [x] 4.2 Implement canonical HTML source capture for both dynamic `srcDoc` and ordinary HTML file previews without using the post-execution DOM as the revision baseline.
- [ ] 4.3 Implement an explicitly enabled iframe annotation controller with hover resolution, capture-phase selection, meaningful-ancestor choice, keyboard cancellation, reload cleanup, and explicit unsupported-target handling.
- [x] 4.4 Render hover outlines, numbered markers, comment editor, edit/delete/reselect controls, and `完成批注` outside the iframe using Conversation Workspace tokens and accessible focus behavior.
- [x] 4.5 Ensure annotation-mode gestures suppress document navigation/actions and preview click analytics while ordinary interactions and tracking resume after exit.
- [ ] 4.6 Detect canonical source digest changes caused by reload, polling, template switching, or nested navigation and prevent stale annotations from being silently reused.

## 5. Drawer-only eligibility and regression boundaries

- [x] 5.1 Write component tests proving `添加批注` appears for explicitly enabled HTML right-side previews with a writable composer.
- [ ] 5.2 Write regression tests proving the right-side FileManager session preview opts in, while default `FilePreviewModal`, scheduled-task results, TaskRunGroupCard results/steps, ReportView/read-only previews, content-only chat, ordinary file-browser previews, and non-HTML content remain annotation-free.
- [x] 5.3 Wire the annotation capability through the normal-chat `FileManager` session-preview boundary and keep shared/modal callers opted out by default.
- [ ] 5.4 Verify eligible nested HTML right-side preview navigation receives a fresh source-bound draft and does not inherit stale targets from its parent source.

## 6. Chat draft and submission integration

- [ ] 6.1 Write provider/reducer tests for chat/source-scoped draft creation, editing, explicit discard, drawer close retention, chat-switch isolation, source conflict, successful consumption, and retry preservation.
- [x] 6.2 Implement `HtmlAnnotationProvider` around the normal Conversation Workspace and expose one pending MVP bundle per logical chat/source.
- [x] 6.3 Add the shared annotation summary row to both welcome and standard composer surfaces with file name, comment count, remove action, long-name handling, and keyboard accessibility.
- [x] 6.4 Implement programmatic canonical-HTML upload through `/console/upload`, attach the returned file through the ordinary file content path, and leave the manual attachment accept policy unchanged.
- [x] 6.5 Serialize `document_annotations` at the top level of `/console/chat`, provide concise default visible text when the composer is otherwise empty, and keep hidden target details out of displayed text.
- [x] 6.6 Preserve annotation drafts across upload/fetch/interrupt failures, follow existing chat behavior for visible text and ordinary attachments, avoid overwriting newer composer input, and clear only the accepted revision token after successful request submission.
- [ ] 6.7 Add request-builder and Chat submission tests covering annotated turns, ordinary turns, active-run interruption, welcome/standard composer parity, and no cross-chat leakage.
- [x] 6.8 Persistently exit Plan Mode before an annotated upload, force the current request to normal mode, preserve the draft on persistence failure, and cover the mode helper plus Chat regressions.

## 7. Interactive HTML preservation verification

- [x] 7.1 Record the approved scope decision that inline-script, handler, markup, style, and behavioral equivalence are model responsibilities rather than publication acceptance gates.
- [x] 7.2 Keep existing narrow structural/external-resource checks without adding inline-script hashing, event-handler manifests, runtime-change declarations, or behavioral diff enforcement.
- [x] 7.3 Add an unresolved fixture for an inaccessible or ambiguous target and verify the Agent/publication flow reports it without flattening the live DOM or overwriting the source.
- [x] 7.4 Remove mandatory browser-runtime smoke validation from this feature's acceptance boundary and avoid claiming runtime equivalence in publication results.

## 8. Quality review and delivery

- [ ] 8.1 Update `console/DESIGN.md` only if implementation establishes a reusable Conversation Workspace annotation rule; otherwise keep visual decisions local to the Drawer module.
- [x] 8.2 Run the relevant frontend unit tests, type checking, linting, and production build using the repository's configured package commands.
- [ ] 8.3 Run focused backend pytest suites with `venv/bin/python -m pytest`, then run the broader affected router/runner/tool test groups.
- [ ] 8.4 Use `copaw-f2e-review` to review project conventions, state contracts, complexity, accessibility, error states, and the Drawer/Modal exclusion boundary.
- [ ] 8.5 Use `browser-qa` to verify normal HTML Drawer annotation, non-HTML Drawer exclusion, scheduled-task and task-run Modal exclusion, narrow/full-width Drawer behavior, failure recovery, and the returned revised HTML preview.
- [ ] 8.6 Run an end-to-end turn with a text-only model and confirm the assistant returns a distinct revised HTML artifact; observe runtime behavior without treating model-output equivalence as a publication gate.
- [ ] 8.7 Run GitNexus `detect_changes` against `main`, inspect every affected execution flow, and confirm the final diff is limited to the approved annotation feature and required design documentation.
