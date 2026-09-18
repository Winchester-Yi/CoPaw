## MODIFIED Requirements

### Requirement: Existing message interactions remain unchanged

Content-only presentation SHALL NOT change message-card visibility, permissions, state checks, event handlers, or mutation behavior. Any message-level interaction that normal `/chat/{chat.id}` would expose SHALL remain governed by the same existing logic, except that the write-oriented HTML annotation affordance SHALL be omitted because content-only presentation intentionally has no composer, upload, or submission surface. Existing file preview, download, navigation, and read-only HTML interactions SHALL remain available.

#### Scenario: Approval and feedback remain interactive

- **WHEN** normal chat rules expose approval/deny or response-feedback controls for a loaded or streamed message
- **THEN** the same controls and handlers remain available in content-only presentation

#### Scenario: Retry, suggestions, and message affordances remain interactive

- **WHEN** normal chat rules expose retry/regenerate, suggestions, copy, download, preview, disclosure, or quick-navigation controls
- **THEN** content-only presentation preserves the same visibility and behavior

#### Scenario: HTML preview omits annotation without a composer

- **WHEN** a message-level HTML file is opened while content-only presentation is active
- **THEN** the file remains previewable and downloadable
- **AND** no `添加批注` action or pending annotation draft is exposed
