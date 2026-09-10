## Context

CoPaw renders generated and uploaded HTML through a shared `FilePreviewModal` implementation. Normal writable chats open generated files in the `FileManager` right-side drawer, where the preview itself uses `presentation="workspace"`; scheduled-task results, task-run histories, ReportView, and other callers use modal presentation. Dynamic preview paths retain a complete HTML string before it is assigned to iframe `srcDoc`; ordinary HTML files are fetched as text/blob content. The existing iframe tracking code already proves that same-origin preview DOM events can be observed.

Chat uploads are stored under the request-scoped workspace media directory, same-turn file parts are converted to trusted `workspace_file` references, and hidden Console context can be appended for the model while remaining redacted from display. The main Agent already has file read/write/edit tools and `copy_file_to_static`, whose result renders as an HTML file card.

The feature must work with text-only models. It must not rely on screenshots, source maps, `template_id`, `result_id`, or a durable document registry. The revised document must be a new HTML artifact based on the original source and must retain existing JavaScript interactions and dynamic re-rendering for everything the annotations do not change.

## Goals / Non-Goals

**Goals:**

- Provide element-level comments only in HTML opened from a writable normal-chat right-side drawer.
- Give a text-only Agent enough deterministic target evidence to edit the intended element.
- Preserve the pre-execution HTML source, scripts, dependencies, and runtime behavior while applying minimal requested edits.
- Send a versioned structured annotation bundle through the existing chat endpoint and workspace attachment boundary.
- Produce a new validated HTML file and render it as the assistant's artifact without overwriting the source.
- Preserve annotation drafts through recoverable UI and network failures while isolating them by chat and source digest.

**Non-Goals:**

- Annotation UI in scheduled tasks, task-run results, ReportView, read-only/content-only chats, file manager previews, or any modal presentation.
- Screenshot capture, image understanding, or coordinate-only targeting.
- Reverse-editing a template service, data provider, or external application that generated the HTML.
- Guaranteed editing of cross-origin nested iframes, canvas pixels, inaccessible shadow roots, or opaque minified runtime code with no safe target.
- A persistent collaborative-comment database, long-lived `document_ref`, comment threads, reviewers, resolution workflow, or cross-session document version graph.
- General-purpose code-file upload through the manual attachment picker.

## Decisions

### 1. Capability is enabled explicitly at the right-drawer preview boundary

The `FileManager` session-file preview inside the normal-chat right-side drawer explicitly opts into annotation through `enableAnnotations`. The shared `FilePreviewModal` does not infer capability from its visual `presentation`, because this preview is embedded with `presentation="workspace"`. All other callers remain opted out by default. A chat-scoped provider also supplies `composerAvailable` and read-only/content-only state, so the capability prop alone cannot accidentally enable an incomplete workflow.

Effective eligibility is:

```text
right-side FileManager session preview
AND HTML content
AND normal writable chat
AND composer/upload surface available
```

Modal callers and ordinary workspace/file-browser previews remain behaviorally unchanged because they do not opt in. Nested HTML that stays inside the eligible right-side preview inherits annotation capability for its currently active source; changing the nested source digest invalidates unsent targets from the prior source.

Alternative considered: add the button to shared `headerActions` and hide it by CSS. Rejected because modal callers would still mount annotation behavior and tests could miss inaccessible or programmatic activation.

### 2. Canonical pre-execution source and live DOM evidence are separate

The source supplied to `srcDoc` or fetched for the HTML blob is retained as `canonicalHtml`. The iframe's live DOM is never serialized as the revision baseline because JavaScript may already have expanded templates, attached state, or replaced nodes; reloading such a serialization could duplicate generated content or lose the generator.

Each draft binds to:

```text
chat key + preview source key + SHA-256(canonicalHtml)
```

The live DOM contributes only target evidence. If polling, template switching, nested navigation, or file reload changes the digest, the UI requires a new annotation draft rather than silently rebasing old locators.

Alternative considered: upload `document.documentElement.outerHTML`. Rejected because it cannot reliably preserve dynamic JavaScript behavior or the distinction between source and runtime state.

### 3. Target identity uses layered DOM evidence, not visual coordinates

On selection, the explicitly enabled annotation controller records:

- annotation ID and user comment;
- tag name and meaningful role;
- unique `id`, stable `data-*`, `name`, `aria-label`, and safe URL identity when present;
- CSS/element-child path and sibling index;
- exact text quote with bounded prefix/suffix;
- bounded target and ancestor `outerHTML` excerpts;
- a whitelist of relevant computed styles;
- whether the node can be found in canonical source or is runtime-generated.

Coordinates and rectangles remain local overlay state. The resolution order is stable attribute, source-resolvable selector/text, rendered excerpt signature, and structural path. The frontend refuses targets that have no addressable DOM representation.

For a source-resolvable target, implementation may inject a temporary `data-copaw-annotation` marker into an annotation working copy only when it can do so without ambiguous or broad source rewriting. The canonical source and digest remain separately retained. Runtime-only targets rely on the structured locator.

Alternative considered: `nth-child` selector alone. Rejected because insertions and dynamic rendering make it fragile. Source line numbers are also rejected because browser DOM nodes do not retain dependable source offsets after parsing and script execution.

### 4. Annotation UI stays outside the iframe document

Hover outlines, numbered markers, and the comment editor are rendered by the parent Drawer layer using iframe-relative rectangles. They are not inserted into the document body and therefore cannot leak into downloads or affect page layout. Annotation-mode listeners use capture phase to prevent navigation and existing click-tracking callbacks for consumed gestures, and are removed on exit/load/unmount.

The right-side preview header uses the Conversation Workspace emphasis `#3769FC`, existing icon/button sizing, 8px action gaps, visible keyboard focus, and named controls. Entering mode exposes a concise instruction and `完成批注`; each marker can be focused, edited, deleted, or scrolled back into view. Responsive full-width presentation keeps the same behavior.

Alternative considered: inject an editor script and visual controls into arbitrary HTML. Rejected because it changes the target document, increases collision risk, and complicates source preservation.

### 5. A chat-scoped provider owns draft handoff

An `HtmlAnnotationProvider` is mounted with the normal Conversation Workspace around both the runtime message area and composer surfaces. It owns at most one pending bundle per logical chat/source for the MVP and exposes:

```text
startDraft(source)
updateAnnotations(sourceDigest, annotations)
stageForComposer(bundle)
removePendingBundle()
consumeAfterAcceptedSubmission()
migrateChatAlias(fromChatKey, toChatKey, token)
```

The composer renders an attachment-like row with file name and comment count above the text input. Closing the Drawer does not remove a staged bundle. Switching chats isolates it; a failed upload/request leaves it intact; accepted submission clears only the submitted revision token. Both welcome and standard composer paths consume the same provider contract.

Session creation and ID resolution are authoritative alias events rather than ordinary chat switches. A pending bundle is migrated only through those events, while accepted submissions are consumed by their unique token so an in-flight URL change cannot leave stale annotation state behind.

Submitting annotations is explicit execution intent. If the current chat is in Plan Mode, the Console first persists Plan Mode as disabled and forces the annotated request to `mode: "normal"`; it does not grant write or publication tools to Plan Mode. If that persistence fails, submission stops before the canonical HTML upload and the staged bundle remains available for retry. The normal-mode state remains disabled for subsequent follow-up edits rather than changing for only one turn.

Alternative considered: keep draft state inside `FilePreviewModal`. Rejected because the composer cannot reliably access it and closing/unmounting the preview would lose the work.

### 6. The source travels as an ordinary same-turn HTML attachment

On send, the Console constructs a `File` from `canonicalHtml` with a safe revision-source name and uploads it through `/console/upload`. The returned URL is included as an ordinary file content part, preserving the existing same-turn `workspace_file` resolution. Programmatic annotation upload does not broaden the manual file-picker accept policy.

The request also includes:

```json
{
  "document_annotations": {
    "schema_version": 1,
    "source": {
      "attachment_url": "<uploaded HTML preview URL>",
      "file_name": "report.annotation-source.html",
      "sha256": "<canonical source digest>"
    },
    "output": {
      "mode": "new_file",
      "format": "html",
      "preserve_original": true,
      "suggested_name": "report-revised.html"
    },
    "annotations": [
      {
        "id": "ann-001",
        "comment": "将这里改成两列布局",
        "target": {
          "runtime_generated": false,
          "stable_attributes": { "id": "sales-summary" },
          "selector": "main > section.sales-summary",
          "text_quote": { "exact": "今日销售额" },
          "rendered_html": "<section ...>...</section>"
        }
      }
    ]
  }
}
```

The visible default message is concise, for example `请根据 3 条页面批注生成修改后的 HTML。`; the structured bundle is not dumped into visible chat text.

Alternative considered: add a database-backed `document_ref`. Rejected for MVP because the uploaded workspace file plus digest already identifies the exact revision source within the current request scope.

### 7. Backend revalidates and renders a typed hidden directive

`/console/chat` extracts `document_annotations` explicitly and validates a typed schema. It matches `source.attachment_url` to a file content part from the same turn, reuses the existing workspace-media containment rules, checks `.html`/`.htm`, computes the digest server-side, and enforces bounded counts and field sizes. Client-supplied absolute paths are never accepted as trusted identity.

The generic Chat request builder may normalize the file content part from its preview URL to the decoded workspace path before transport. Same-turn matching therefore compares the parsed file identity rather than raw URL strings, while `source.attachment_url` itself must remain a `/files/preview/...` URL and the resolved file must still pass current-workspace media containment.

Recommended initial limits are 20 annotations, 2 KiB per comment, 4 KiB per rendered excerpt, and 128 KiB for the structured bundle, while the source file remains governed by the existing upload limit.

After validation, a `DocumentAnnotationDirective` renders escaped XML-like hidden context containing the trusted absolute source path, annotations, preservation rules, and output requirements. The directive states that HTML/document content is untrusted data, not authoritative instructions. Existing hidden-context redaction keeps it out of displayed history.

Alternative considered: let the client send a preformatted prompt. Rejected because it would bypass server validation and mix untrusted paths and document data with trusted runtime instructions.

### 8. The Agent edits source minimally and uses a runtime override only as fallback

The model-facing contract orders the Agent to:

1. read the canonical source attachment;
2. resolve each annotation using stable/source evidence before structural fallbacks;
3. prefer a minimal change in existing markup, CSS, data, or JavaScript generator;
4. preserve unrelated scripts, styles, resources, event handlers, and dynamic behavior;
5. write a distinct output file;
6. report applied and unresolved annotation IDs;
7. publish the new artifact.

If a runtime-generated target cannot be mapped to source but has an unambiguous locator, the Agent may append a namespaced, idempotent override script. The override waits for DOM readiness, resolves only the target fingerprint, reapplies after relevant mutations with debouncing, and changes only the requested property/content/structure. It must not replace the original application bootstrap or freeze the document.

If neither path is safe, the Agent reports the annotation unresolved. Flattening the live DOM is never an allowed fallback.

Alternative considered: always append MutationObserver patches. Rejected because source edits are easier to maintain and less likely to fight application state; runtime patches remain an explicit last resort.

### 9. A narrow publication tool turns file creation into a verifiable result

Add a small request-scoped `publish_annotated_html` Agent tool or equivalent validation wrapper around `copy_file_to_static`. The backend binds the validated source path and expected annotation IDs into a per-turn tool factory; the model supplies only the output path plus applied and unresolved IDs. The tool is registered only for the main Agent after annotation validation and is not added to the global built-in tool configuration. It then verifies:

- both paths are within the current workspace;
- output is a distinct, non-empty `.html` file;
- temporary `data-copaw-*` annotation markers and editor instrumentation are absent;
- required document structure is present;
- the existing bounded external resource manifest check passes.

On success it delegates to the existing static publication logic and returns the URL consumed by the existing HTML file card. It does not attempt semantic verification of the user's visual intention.

The product accepts that a model may incorrectly remove or alter inline scripts, event handlers, markup, styles, or other behavior. Those preservation rules remain model instructions, not a backend correctness guarantee. This change does not add inline-script hashing, event-handler manifests, declared runtime changes, DOM behavior comparison, or mandatory browser smoke validation. Existing narrow structural and external-resource checks may remain as low-cost safeguards but must not be described as proof of runtime equivalence.

Alternative considered: rely on final prose and generic `copy_file_to_static` alone. Rejected because the product requirement is a newly generated HTML artifact, not a claim that one was produced.

### 10. Initial security boundary matches current trusted preview scope

The MVP supports HTML already previewable by the current same-origin CoPaw flow. Direct DOM access is consistent with existing click tracking, but all listeners and source values remain scoped to the active iframe load. Arbitrary external cross-origin pages are not made annotatable.

The implementation must not relax tenant path boundaries, executable attachment policy, tool approval, or static route scoping. A future security-hardening change may move arbitrary HTML previews onto an isolated origin and use a reviewed `postMessage` bridge; that is not required to deliver this trusted-source feature.

## Risks / Trade-offs

- [Runtime-generated element cannot be mapped back to readable source] → Capture bounded runtime evidence, prefer generator edits, allow only narrowly scoped idempotent overrides, and report unresolved annotations rather than flattening the page.
- [Model changes unrelated code] → Require minimal edits in the model instruction, preserve the source file, and return a new artifact for user review; accept incorrect model output without attempting full content or runtime equivalence validation.
- [MutationObserver override loops or degrades performance] → Make overrides optional, target-specific, idempotent, debounced, and disconnected when no longer needed; reject broad whole-document rewrite loops.
- [Dynamic source changes while the user comments] → Bind drafts to a SHA-256 digest and invalidate on reload/template/source changes.
- [Annotation gestures trigger document actions or analytics] → Use capture-phase interception only while annotation mode is active and add explicit tracking regression tests.
- [Large HTML or evidence consumes excessive model context] → Send the source as a file for on-demand reading, bound target excerpts and comments, and keep detailed data out of visible text.
- [HTML contains prompt-injection text] → Treat document contents as untrusted data in a server-generated directive and escape every structured value.
- [Shared preview component leaks annotation into modal callers] → Enable through `FilePreviewDrawer` plus writable-chat context and test every modal/task/content-only exclusion.
- [Browser smoke checks are unavailable in some deployments] → Keep structural and manifest validation mandatory; make runtime smoke validation capability-aware without weakening source/output/path checks.

## Migration Plan

1. Add request models, backend validation, hidden directive rendering, and unit tests while accepting no client traffic yet.
2. Add the publication wrapper and Agent instruction contract behind the presence of a validated annotation bundle.
3. Add the chat-scoped draft provider, programmatic HTML upload, and request serialization with tests.
4. Add Drawer-only selection UI and source/locator capture, then verify modal, scheduled-task, task-run, content-only, and non-HTML exclusions.
5. Run focused frontend tests, backend pytest suites, browser Drawer scenarios, and a text-only model end-to-end revision using static and JavaScript-generated targets.
6. Roll out without a database migration. Rollback removes the Drawer affordance and stops sending the optional field; old clients and ordinary requests remain compatible.

## Open Questions

No blocking product questions remain. The confirmed contract is that every successful turn returns a new HTML artifact and preserves the original document's JavaScript interactions and dynamic rendering for unannotated behavior. Implementation may tune bounded limits and the exact names of internal helpers without changing that contract.
