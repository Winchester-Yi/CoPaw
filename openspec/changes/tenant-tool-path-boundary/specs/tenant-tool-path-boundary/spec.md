## ADDED Requirements

### Requirement: Builtin local path tools stay within the current tenant workspace root
The system SHALL reject any builtin local path tool operation whose
resolved target path is outside the current tenant workspace root
`WORKING_DIR/<tenant_id>`.

#### Scenario: Read path escapes current tenant root through relative traversal
- **WHEN** a tenant-scoped builtin file-read operation resolves a path
  like `../other-tenant/file.txt`
- **THEN** the system SHALL reject the operation before reading the file
- **AND** the operation SHALL return a permission-denied error

#### Scenario: Absolute path targets a sibling tenant workspace
- **WHEN** a tenant-scoped builtin local path tool receives an absolute
  path under `WORKING_DIR/<other-tenant>/...`
- **THEN** the system SHALL reject the operation before filesystem
  access
- **AND** the operation SHALL NOT expose the sibling tenant file
  contents

#### Scenario: Resolved path remains within current tenant root
- **WHEN** a tenant-scoped builtin local path tool resolves a path under
  `WORKING_DIR/<tenant_id>/...`
- **THEN** the system SHALL allow the operation to proceed subject to
  the tool's normal validation rules

### Requirement: Tenant-scoped local path tools fail closed without tenant context
The system SHALL fail closed for tenant-scoped builtin local path tools
when tenant context is unavailable.

#### Scenario: Missing tenant context during local file operation
- **WHEN** a builtin tenant-scoped local path tool is invoked without a
  current tenant context
- **THEN** the system SHALL reject the operation
- **AND** the system SHALL NOT fall back to global `WORKING_DIR`

### Requirement: Search and media builtin tools use the same tenant boundary
The system SHALL apply the same tenant workspace root boundary to
builtin search-root and media-file tools as it does to file read/write
tools.

#### Scenario: Search root points outside current tenant root
- **WHEN** a tenant-scoped `grep_search` or `glob_search` operation
  resolves its search root outside `WORKING_DIR/<tenant_id>`
- **THEN** the system SHALL reject the search before enumeration begins

#### Scenario: Media view path points outside current tenant root
- **WHEN** a tenant-scoped `view_image` or `view_video` operation
  resolves its media file path outside `WORKING_DIR/<tenant_id>`
- **THEN** the system SHALL reject the operation before loading the file
  into model context

### Requirement: Shell command execution rejects explicit cross-tenant path access
The system SHALL reject builtin shell execution when the effective
working directory or any explicitly parsed path token resolves outside
the current tenant workspace root.

#### Scenario: Shell cwd escapes current tenant root
- **WHEN** a tenant-scoped shell execution request provides a `cwd` that
  resolves outside `WORKING_DIR/<tenant_id>`
- **THEN** the system SHALL reject the command before starting the shell

#### Scenario: Shell command contains explicit sibling tenant path
- **WHEN** a tenant-scoped shell command contains an explicitly parsed
  path token that resolves outside `WORKING_DIR/<tenant_id>`
- **THEN** the system SHALL reject the command before execution

#### Scenario: Shell command uses explicit path within current tenant root
- **WHEN** a tenant-scoped shell command uses an explicit path token
  that resolves within `WORKING_DIR/<tenant_id>`
- **THEN** the system SHALL allow the command to proceed subject to
  other existing shell safety checks

### Requirement: Tenant path authorization uses resolved paths
The system SHALL authorize tenant-scoped builtin local path access using
resolved absolute filesystem paths so lexical traversal and symlink
escape cannot bypass the tenant root boundary.

#### Scenario: Symlink under tenant root points outside tenant root
- **WHEN** a tenant-scoped builtin local path tool targets a path inside
  the tenant workspace that resolves through a symlink to a location
  outside `WORKING_DIR/<tenant_id>`
- **THEN** the system SHALL reject the operation
