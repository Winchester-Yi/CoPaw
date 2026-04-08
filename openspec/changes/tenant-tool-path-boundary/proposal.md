## Why

SWE already binds tenant identity and tenant workspace context for
requests, but the built-in local file and shell tools still rely on
path resolution patterns that can reach outside the current tenant's
workspace. In a multi-tenant deployment this leaves a direct data
exposure path where one tenant can read, search, or send files from
another tenant's directory.

## What Changes

- Add a hard tenant workspace path boundary for built-in local tools so
  any resolved target path must remain under
  `WORKING_DIR/<tenant_id>`.
- Apply the boundary to file read/write tools, file search tools,
  media-view tools, and file-send tools that accept local filesystem
  paths.
- Add shell preflight validation for `cwd` and for explicitly parsed
  path tokens in the command string, rejecting cross-tenant access
  before execution.
- Fail closed when tenant context is missing for these tenant-scoped
  local path operations instead of falling back to global
  `WORKING_DIR`.
- Keep `tool_guard` as an advisory and audit-oriented safety layer, but
  make tenant path enforcement authoritative in tool execution paths.

## Capabilities

### New Capabilities
- `tenant-tool-path-boundary`: Enforce a tenant-scoped filesystem
  boundary for built-in local path tools so resolved paths cannot escape
  the current tenant workspace root.

### Modified Capabilities
- None.

## Impact

- Affected backend tool modules:
  `src/swe/agents/tools/file_io.py`,
  `src/swe/agents/tools/file_search.py`,
  `src/swe/agents/tools/send_file.py`,
  `src/swe/agents/tools/view_media.py`,
  `src/swe/agents/tools/shell.py`
- Affected tenant context and security plumbing:
  `src/swe/config/context.py`,
  `src/swe/security/tool_guard/*`
- Adds a new shared path-boundary helper/module for tenant-scoped path
  resolution and validation
- Affects test coverage for tool behavior, path traversal, symlink
  escape, and shell explicit-path rejection
- No external dependency changes; MCP and non-builtin external tool
  isolation remain out of scope for this change
