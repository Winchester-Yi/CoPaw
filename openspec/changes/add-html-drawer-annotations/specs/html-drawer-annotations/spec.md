## ADDED Requirements

### Requirement: HTML annotation is available only in a writable normal-chat drawer

The Console SHALL expose HTML annotation only when the normal-chat `FileManager` right-side drawer explicitly enables annotation for its session-file preview, the active file is HTML, and the current Conversation Workspace has a writable composer. The shared `FilePreviewModal` SHALL remain annotation-free by default. The Console MUST NOT expose annotation in scheduled-task previews, task-run result previews, content-only or read-only chat presentations, non-HTML previews, or other modal/workspace preview callers.

#### Scenario: Normal chat opens HTML in the right-side drawer
- **WHEN** a user opens an HTML artifact from a writable normal chat in the `FileManager` right-side session preview
- **THEN** the drawer header exposes an accessible `添加批注` action

#### Scenario: Drawer previews a non-HTML file
- **WHEN** the right-side drawer previews a PDF, image, Office document, Markdown document, or another non-HTML file
- **THEN** no HTML annotation action or annotation state is created

#### Scenario: Scheduled-task or task-run HTML uses a modal
- **WHEN** an HTML result is opened from a scheduled task or a task-run result whose preview presentation is `modal`
- **THEN** no annotation action, overlay, draft, or annotation request field is exposed

#### Scenario: Another feature opens the shared modal preview
- **WHEN** any ReportView, read-only replay, or other caller opens HTML through `FilePreviewModal`
- **THEN** the existing preview remains unchanged and annotation-free

### Requirement: Annotation mode selects DOM elements without activating the document

While annotation mode is active, the drawer SHALL provide hover highlighting, element selection, numbered target markers, comment entry, keyboard focus, and cancellation without requiring screenshot input. A selection gesture consumed by annotation mode MUST NOT activate links, buttons, form submission, or other page actions. Leaving annotation mode SHALL restore the document's existing interaction behavior.

#### Scenario: User hovers and selects an element
- **WHEN** annotation mode is active and the user points to an eligible DOM element in the HTML preview
- **THEN** the Console displays a non-destructive outline aligned to that element
- **AND** selecting it opens a comment editor associated with a numbered annotation

#### Scenario: Selection lands on a deeply nested presentation node
- **WHEN** the selected node is a presentational child such as an icon or text span whose nearest meaningful ancestor represents the visible control or content block
- **THEN** the Console resolves the annotation to the nearest meaningful target and shows that resolved target before saving the comment

#### Scenario: Annotation mode intercepts a link click
- **WHEN** the user selects an anchor or button while annotation mode is active
- **THEN** the Console saves or begins a target annotation without navigating, submitting, or invoking the document action

#### Scenario: User exits annotation mode
- **WHEN** the user presses Escape or chooses the annotation-mode close action
- **THEN** temporary hover and editor UI is removed
- **AND** existing saved draft annotations remain available until explicitly discarded or sent
- **AND** ordinary HTML interactions work again

### Requirement: Each annotation carries resilient text-model target evidence

For each saved annotation, the Console SHALL capture a unique annotation ID, the user's comment, stable target attributes when available, a DOM selector/path, a bounded text quote with context, a bounded rendered HTML excerpt, and only the computed-style fields relevant to layout and presentation. Screen coordinates MAY be retained for local overlay placement but MUST NOT be used as the sole model-facing target identity.

#### Scenario: Target has a stable document identifier
- **WHEN** the selected element has a unique `id`, stable `data-*` attribute, form name, or accessible label
- **THEN** the annotation bundle records that stable identity ahead of structural selector fallbacks

#### Scenario: Target lacks a stable identifier
- **WHEN** the selected element has no stable identifier
- **THEN** the annotation bundle records a structural path, sibling context, text quote, ancestor evidence, and rendered HTML excerpt sufficient for bounded fallback resolution

#### Scenario: Target exists only after JavaScript execution
- **WHEN** the selected element does not exist in the pre-execution source and was created or replaced at runtime
- **THEN** the annotation is marked as runtime-generated
- **AND** its runtime locator and rendered evidence are retained without treating the live DOM serialization as the source document

#### Scenario: Target cannot be represented safely
- **WHEN** the selected content is inside a cross-origin nested iframe, inaccessible shadow tree, canvas pixels, or another surface without an addressable DOM target
- **THEN** the Console explains that the location cannot be annotated and does not create a misleading coordinate-only annotation

### Requirement: Revision input retains the canonical interactive HTML source

The Console SHALL retain the complete HTML source that was supplied to the preview before browser execution, including scripts, styles, resource references, doctype, and document structure. The live mutated DOM SHALL be used only as target evidence and MUST NOT replace the canonical source used for revision.

#### Scenario: Dynamic HTML is rendered from an API response
- **WHEN** the drawer renders a complete HTML string obtained or assembled from backend data
- **THEN** that pre-execution HTML string becomes the canonical source for the annotation bundle without requiring `template_id` or `result_id`

#### Scenario: JavaScript mutates the document after load
- **WHEN** the preview script adds, removes, or replaces DOM nodes after the iframe loads
- **THEN** the Console retains the original executable HTML source for revision
- **AND** records the changed live DOM only in the selected target evidence

#### Scenario: Preview source changes before annotations are sent
- **WHEN** polling, template switching, nested navigation, or another reload replaces the canonical HTML source
- **THEN** annotations remain bound to their original source digest
- **AND** the Console requires the user to discard or explicitly re-create annotations for the new source rather than silently applying stale targets

### Requirement: Completed annotations transfer to the chat composer as one draft bundle

Finishing annotation SHALL add one source-scoped annotation bundle to the writable chat composer, display the source file name and annotation count, allow optional additional user text, and preserve the bundle until successful submission or explicit removal. The bundle SHALL belong to the current logical chat and source digest and MUST NOT leak across chat switches.

#### Scenario: User finishes multiple comments
- **WHEN** the user completes three annotations in the drawer
- **THEN** the composer displays one attachment-like summary showing the HTML file and `3 条批注`
- **AND** the user can add an optional visible instruction before sending

#### Scenario: User closes the drawer before sending
- **WHEN** a completed bundle is present in the composer and the user closes the preview drawer
- **THEN** the composer retains the bundle for the current chat

#### Scenario: User changes chats
- **WHEN** the active logical chat changes while an annotation bundle is pending
- **THEN** the pending bundle is not submitted with the new chat
- **AND** returning to the original chat restores or safely discards it according to the chat-scoped draft policy

#### Scenario: Submission fails
- **WHEN** upload or chat submission fails before the backend accepts the turn
- **THEN** the annotation bundle remains available for retry
- **AND** failure handling follows the existing chat behavior for visible text and ordinary attachments
- **AND** a delayed failure MUST NOT overwrite newer composer input

#### Scenario: Submission succeeds
- **WHEN** the backend accepts the annotated turn
- **THEN** the Console clears that submitted bundle from the composer and keeps the visible historical message free of hidden annotation directives
