## MODIFIED Requirements

### Requirement: Record events for active user preview sessions

The system SHALL record HTML preview click events and eligible list snapshot events when a user opens auto-preview HTML through normal chat, task output, Markdown file links, or tool-rendered file cards outside an explicitly read-only replay context, except for gestures consumed by HTML annotation selection. Entering annotation mode MUST NOT disable ordinary recording after annotation mode exits.

#### Scenario: Normal chat auto-preview records button clicks

- **WHEN** a user opens an auto-preview HTML file from the normal chat page and clicks a "查看方案" control inside the preview while annotation mode is inactive
- **THEN** the system records an HTML preview click event classified as a plan click

#### Scenario: Task auto-preview records task-associated events

- **WHEN** a scheduled task auto-preview HTML file is opened from the normal chat task flow
- **THEN** the system records eligible click and list snapshot events with the task tracking context preserved

#### Scenario: Markdown and tool file cards keep recording

- **WHEN** an auto-preview HTML file is opened through a Markdown file link or a tool-rendered file card outside read-only replay
- **THEN** the system records eligible HTML preview events using the existing recording API

#### Scenario: Annotation selection is not recorded as document interaction

- **WHEN** annotation mode consumes a click to select an HTML element
- **THEN** the system does not record that gesture as a preview click event
- **AND** leaving annotation mode restores the existing event-recording behavior
