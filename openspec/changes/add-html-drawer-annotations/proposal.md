## Why

Users can inspect generated HTML in the normal-chat right-side preview drawer, but they cannot attach precise, element-level revision feedback to that artifact. Text-only agents therefore lack a reliable way to associate a requested change with the correct HTML element and to return a revised, still-interactive HTML document without relying on screenshots or template/result identifiers.

## What Changes

- Add element-level annotation mode only to HTML opened in the normal-chat right-side preview drawer.
- Keep scheduled-task previews, task-run result previews, content-only/read-only chat presentations, non-HTML previews, and all modal previews annotation-free.
- Capture annotations against the live rendered DOM while retaining the exact pre-execution HTML source as the revision baseline.
- Represent each target with a stable anchor when available plus a bounded DOM fingerprint fallback for runtime-generated elements; do not use screen coordinates as model-facing identity.
- Upload the canonical source HTML as a same-turn workspace attachment and submit a versioned `document_annotations` request field alongside the visible user message.
- Validate attachment ownership, source digest, annotation limits, and target evidence on the backend, then convert the bundle into trusted hidden model context.
- Require the agent to preserve unannotated HTML, CSS, scripts, resources, and runtime behavior, create a new HTML file rather than overwrite the source, remove temporary annotation metadata, and publish the revised artifact back into chat.
- Add a bounded runtime-override fallback for elements that only exist after JavaScript execution, while preferring minimal source edits whenever the target can be mapped to original markup, styles, or scripts.
- Suppress existing HTML-preview analytics for clicks consumed by annotation selection while preserving normal preview tracking outside annotation mode.

## Capabilities

### New Capabilities

- `html-drawer-annotations`: Drawer-only annotation availability, element selection, comment editing, target anchoring, draft lifecycle, and composer handoff.
- `annotated-html-revision`: Structured request contract, backend trust boundary, model-facing edit instructions, dynamic-behavior preservation, output validation, and revised HTML publication.

### Modified Capabilities

- `html-preview-event-recording`: Annotation-selection gestures are excluded from ordinary preview click analytics without disabling normal tracking outside annotation mode.
- `chat-content-only-mode`: Content-only chat keeps file preview/download behavior but omits the write-oriented HTML annotation affordance because it has no composer or upload surface.

## Impact

- Console preview flow: the normal-chat `FileManager` right-side drawer, its capability-gated workspace `FilePreviewModal`, iframe DOM tracking, drawer styles, and tests.
- Console chat flow: a chat-scoped annotation draft provider, both normal composer surfaces, attachment upload integration, request typing/building, submission cleanup, and failure recovery.
- Backend Console flow: `/console/chat` extraction and validation of `document_annotations`, same-turn workspace-file matching, source hashing, hidden-context directive generation, and unit tests.
- Agent/runtime flow: HTML edit instructions and a narrow publish/validation path built on the existing file tools and static-file response card.
- No new database, Redis record, durable `document_ref`, multimodal input, `template_id`, or `result_id` dependency is introduced.
- No API behavior changes for requests that omit `document_annotations`; no existing modal or scheduled-task preview gains annotation UI.
