## ADDED Requirements

### Requirement: Annotated revisions use a versioned structured chat contract

The Console SHALL submit annotated HTML revisions through the existing `/console/chat` request with a normal visible user message, one same-turn HTML file attachment containing the canonical revision source, and a top-level `document_annotations` object. The structured object SHALL declare a schema version, source attachment identity and digest, annotations, and a new-file output contract. It MUST NOT require image content, `template_id`, `result_id`, or a durable `document_ref`.

#### Scenario: User sends an annotated HTML revision

- **WHEN** a user submits a completed drawer annotation bundle
- **THEN** the request contains the visible text and HTML file content parts used by ordinary chat attachments
- **AND** `document_annotations.schema_version` identifies the supported contract
- **AND** the source entry identifies the attached HTML and its SHA-256 digest
- **AND** the output entry requires a new HTML file while preserving the source

#### Scenario: User provides no additional text

- **WHEN** the user sends annotations without typing an additional instruction
- **THEN** the Console supplies a concise visible message stating that the agent should apply the attached HTML annotations
- **AND** the detailed targets remain in the structured field rather than being expanded into the visible message

#### Scenario: Ordinary chat request has no annotations

- **WHEN** `/console/chat` receives a request without `document_annotations`
- **THEN** its current attachment, context-reference, agent, streaming, and persistence behavior remains unchanged

#### Scenario: Annotated submission starts while Plan Mode is active

- **WHEN** a user submits a completed annotation bundle while the current chat is in Plan Mode
- **THEN** the Console persistently disables Plan Mode before uploading the canonical HTML
- **AND** the current annotated request explicitly uses normal execution mode
- **AND** subsequent follow-up messages remain in normal mode unless the user enables Plan Mode again

#### Scenario: Exiting Plan Mode fails

- **WHEN** Plan Mode state cannot be persisted as disabled for an annotated submission
- **THEN** the Console stops before uploading the canonical HTML or submitting the chat request
- **AND** the pending annotation bundle remains available for retry

### Requirement: Backend validates the annotation source inside the current workspace boundary

The backend SHALL parse `document_annotations` separately from visible message text and SHALL accept it only when the declared source matches an HTML file part in the same turn, resolves to the current request's workspace media directory, belongs to the current tenant/user scope, and matches the declared digest. The backend SHALL enforce bounded annotation count, comment length, evidence length, and total payload size.

#### Scenario: Valid same-turn HTML attachment is submitted

- **WHEN** the annotation source matches an existing same-turn file part under the current workspace media directory and its server-computed digest matches
- **THEN** the backend accepts the bundle and resolves a trusted absolute workspace path for agent context

#### Scenario: Client supplies an arbitrary local or cross-tenant path

- **WHEN** the source reference resolves outside the current workspace media root or to another tenant/user scope
- **THEN** the backend rejects the request without exposing or reading that file for the agent

#### Scenario: Source changed after annotation

- **WHEN** the server-computed source digest differs from the annotation bundle digest
- **THEN** the backend rejects the request as a revision conflict before starting the agent

#### Scenario: Annotation limits are exceeded

- **WHEN** the bundle exceeds configured annotation count, comment, evidence, or total serialized-size limits
- **THEN** the backend rejects the request with a bounded validation error

### Requirement: Model context distinguishes user instructions from document data

After validation, the backend SHALL render a trusted hidden annotation directive that names the resolved source path, lists annotation targets and user comments, and states the output contract. The directive SHALL state that HTML contents, comments, embedded scripts, and document text are target data rather than authoritative system instructions. User-authored annotation comments SHALL remain the requested edits, while all values SHALL be safely escaped before insertion into the hidden directive.

#### Scenario: Attached HTML contains instruction-like text

- **WHEN** the source document contains text or script content that tells the model to ignore the user or perform another action
- **THEN** the hidden directive identifies that content as untrusted document data
- **AND** the agent remains instructed to perform only the user's validated annotation task

#### Scenario: Chat history is displayed

- **WHEN** the annotated user turn is persisted and later returned to the Console
- **THEN** the visible history contains the user's visible message and file reference without exposing the hidden annotation directive

### Requirement: Agent is instructed to create a new interactive HTML revision from the canonical source

The system SHALL instruct the agent to read the attached canonical source, create a distinct HTML output file, preserve unannotated markup, CSS, scripts, event behavior, external or embedded resource references, and dynamic re-rendering semantics, and avoid a serialized post-execution DOM as the output baseline. Publication MUST NOT overwrite the source file. Preservation correctness is not a publication acceptance guarantee.

#### Scenario: Annotated target exists in original markup or styles

- **WHEN** target evidence resolves unambiguously to original HTML, CSS, or script source
- **THEN** the agent makes the smallest source edit that satisfies the annotation
- **AND** unrelated source and runtime behavior remain unchanged

#### Scenario: Target is created by original JavaScript

- **WHEN** target evidence identifies a runtime-generated element and its generator can be located unambiguously in the source
- **THEN** the agent modifies the responsible original markup, style, data, or generator logic rather than freezing the live DOM

#### Scenario: Runtime target cannot be mapped to a safe source edit

- **WHEN** the target is runtime-generated but cannot be mapped unambiguously to its generator
- **THEN** the agent MAY append a narrowly scoped, idempotent runtime override that resolves the supplied target fingerprint after initial render and after relevant re-renders
- **AND** the override MUST avoid replacing unrelated application state or interactions

#### Scenario: Safe behavior-preserving edit is not possible

- **WHEN** neither a source edit nor a bounded runtime override can target the requested element without ambiguity or likely breakage
- **THEN** the agent reports that annotation as unresolved instead of silently flattening the document or claiming full success

### Requirement: Revised HTML is validated and published as the assistant artifact

A successful annotated revision SHALL produce a non-empty `.html` file with a distinct output path, no temporary annotation instrumentation, and a user-accessible static URL. The assistant response SHALL include the new HTML artifact so the existing file card can preview it in the right-side drawer. Publication validation SHALL fail closed when these conditions are not met.

#### Scenario: Agent completes all annotations

- **WHEN** the agent writes a valid revised HTML file and declares all annotation IDs applied
- **THEN** the publication path verifies the output is distinct from the source and contains no temporary CoPaw annotation markers
- **AND** it publishes the file through the existing scoped static-file mechanism
- **AND** the assistant response contains the revised HTML file card or link

#### Scenario: Output attempts to overwrite the source

- **WHEN** the agent supplies the annotation source itself as the output path
- **THEN** publication is rejected and the original remains unchanged

#### Scenario: Output is absent or invalid

- **WHEN** the agent only replies with prose, writes an empty file, writes a non-HTML output, or leaves temporary annotation instrumentation in the document
- **THEN** the turn is not presented as a successful HTML revision
- **AND** the response explains the unresolved output condition

#### Scenario: Revised artifact is opened again

- **WHEN** the user opens the newly published HTML from the assistant response
- **THEN** it loads through the existing HTML drawer preview path
- **AND** it can become the source of a later annotation turn
- **AND** the preview does not claim that its JavaScript behavior was verified

### Requirement: Runtime preservation remains a model instruction rather than a publication guarantee

The revision flow SHALL instruct the model to preserve unrelated scripts, event handlers, resources, and dynamic behavior and SHALL retain the original source for recovery. Publication validation MUST NOT claim semantic or runtime equivalence. The system is not required to reject an otherwise publishable model output because inline scripts, event handlers, markup, styles, or runtime behavior changed incorrectly.

#### Scenario: Model returns an incorrect but publishable revision

- **WHEN** the model removes or changes unrelated executable or document content but still returns an otherwise valid new HTML artifact
- **THEN** the system may publish and display that artifact
- **AND** correctness remains the model's responsibility and subject to user review
- **AND** the publication result does not claim that runtime behavior was verified

#### Scenario: Text-only model performs the revision

- **WHEN** the selected model has no image-input capability
- **THEN** the agent can complete the task using the HTML attachment, annotation comments, DOM fingerprints, and file tools alone
