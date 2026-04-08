## Context

SWE's HTTP stack already establishes tenant identity and binds a
tenant workspace directory for request handling. The current workspace
binding points at the tenant root under `WORKING_DIR/<tenant_id>`, and
many runtime subsystems already treat that tree as the tenant's working
area. However, the built-in tools that access local files do not use
that tenant root as an authorization boundary. Some tools accept
absolute paths directly, some allow relative paths with `..` traversal,
and shell execution can reference explicit sibling-tenant paths even
when its default working directory is tenant-scoped.

The requested behavior is intentionally narrow and strict: built-in
local path tools may access the entire current tenant workspace tree,
but nothing outside `WORKING_DIR/<tenant_id>`. This change must create a
hard boundary for built-in tools without depending on the existing
tool-guard approval workflow, because tenant isolation is a correctness
and security requirement rather than a discretionary warning.

## Goals / Non-Goals

**Goals:**
- Enforce an authoritative tenant filesystem boundary rooted at
  `WORKING_DIR/<tenant_id>` for builtin local path tools.
- Reject cross-tenant absolute paths, `..` traversal, and symlink escape
  when resolved paths fall outside the current tenant root.
- Apply the same boundary semantics across file I/O, file search, media
  view, file send, and shell `cwd` / explicit-path handling.
- Fail closed when tenant context is unavailable for tenant-scoped local
  path operations.
- Keep the implementation centralized so future builtin path tools reuse
  the same resolution and authorization logic.

**Non-Goals:**
- Providing full semantic isolation for arbitrary shell commands or
  interpreter-internal file access.
- Extending this boundary automatically to MCP tools or other external
  tool providers.
- Changing the tenant storage layout for provider or secret data under
  `SECRET_DIR`.
- Redesigning `tool_guard` approval semantics or replacing its existing
  rule-based findings model.

## Decisions

### Decision 1: Tenant path enforcement lives in tool execution, not only in tool guard

**Choice:** Add a dedicated tenant path boundary helper used directly by
builtin tools before they touch the filesystem.

**Rationale:**
- Tenant isolation must be enforced even when no approval flow is
  active.
- Builtin tools need a single authoritative decision path instead of
  duplicating path checks.
- `tool_guard` findings can remain useful for logging and UI explanation
  without carrying the enforcement burden.

**Alternatives considered:**
- Enforce isolation only through `tool_guard`: rejected because it is an
  advisory interception layer and is not the right place for a hard
  authorization boundary.
- Add ad hoc checks to each tool independently: rejected because it
  invites drift and inconsistent path semantics.

### Decision 2: The allowed root is the tenant workspace root, not the current agent workspace

**Choice:** Use `WORKING_DIR/<tenant_id>` as the only allowed root for
this change.

**Rationale:**
- The user explicitly wants the entire tenant workspace to be
  accessible, not just a narrower agent subdirectory.
- The existing tenant workspace middleware already binds the tenant root
  as the request-scoped workspace directory.
- A single tenant root keeps the authorization rule easy to explain and
  test.

**Alternatives considered:**
- Restrict to the current agent workspace only: more conservative, but
  it does not match the requested policy.
- Allow both tenant workspace and tenant secret storage: rejected for
  this change because the requested boundary is specifically
  `WORKING_DIR/<tenant_id>`.

### Decision 3: Path authorization is based on resolved absolute paths

**Choice:** All path checks will expand user/home references, resolve
relative paths against a validated base directory, then compare the
resolved path against the tenant root with `is_relative_to`.

**Rationale:**
- This blocks direct absolute path access to sibling tenants.
- It also blocks relative traversal like `../tenant-b/...`.
- Resolving before comparison closes the obvious symlink escape path.

**Alternatives considered:**
- Compare raw strings or lexical path components: rejected because it is
  fragile and can be bypassed by normalization and symlink tricks.
- Use `strict=True` resolution everywhere: rejected because some tools
  need to authorize write targets before the destination exists.

### Decision 4: Search and media tools are in scope for the same hard boundary

**Choice:** Apply the boundary not just to text file read/write tools,
but also to search tools and media-view tools.

**Rationale:**
- `grep_search` / `glob_search` can enumerate and read other tenants'
  directory contents if left unchecked.
- `view_image` / `view_video` can expose non-text tenant data just as
  directly as `read_file`.
- The change should close the practical builtin local-file access
  surface, not only a subset of tools.

**Alternatives considered:**
- Limit scope to the five originally named file tools: rejected because
  it leaves obvious builtin read/search bypasses.

### Decision 5: Shell enforcement covers `cwd` and explicit path tokens only

**Choice:** For `execute_shell_command`, validate the effective `cwd`
against the tenant root and reject commands whose explicitly parsed path
tokens resolve outside the tenant root.

**Rationale:**
- This meaningfully reduces the obvious cross-tenant shell access paths.
- It is implementable without pretending to fully parse shell semantics.
- The behavior can be stated and tested clearly as "explicit path
  access" protection.

**Alternatives considered:**
- Claim complete shell filesystem isolation via string parsing: rejected
  as not technically defensible.
- Leave shell untouched: rejected because it would remain the most
  obvious bypass after file tools are fixed.

## Risks / Trade-offs

- [Shell parsing is incomplete] -> Scope the guarantee to explicit path
  tokens and `cwd`, and document that full shell isolation requires a
  stronger runtime sandbox.
- [Existing agent workflows may reference absolute paths outside tenant
  root] -> Return a clear permission-denied error and update tests and
  docs to reflect the new boundary.
- [Tenant-local tools can no longer reach `SECRET_DIR/<tenant_id>`] ->
  Accept this as part of the requested boundary and keep the rule simple
  and auditable.
- [Boundary helper misuse by future tools] -> Centralize helper APIs and
  route all current builtin path-taking tools through them as examples.
- [User-visible errors may leak other tenant paths] -> Keep end-user
  error messages generic and reserve detailed resolved-path logging for
  backend audit logs.

## Migration Plan

1. Add a shared tenant path boundary helper and unit tests for path
   resolution, traversal rejection, and symlink escape rejection.
2. Refactor builtin local file, search, media, and send-file tools to
   use the shared helper before any filesystem access.
3. Add shell preflight validation for `cwd` and explicit path tokens.
4. Add or update tests for allowed tenant-local access and denied
   cross-tenant access across all in-scope builtin tools.
5. Optionally follow up with tool-guard findings/logging integration if
   richer audit output is still needed after enforcement is in place.

## Open Questions

- Should failed tenant-boundary checks emit dedicated structured audit
  events in addition to normal tool error logging?
- Do we want to reuse the same helper for any future builtin tools that
  accept directory paths, such as archive extraction or export/import
  helpers?
- Should a later change add an explicit policy layer for builtin access
  to tenant-local secret storage, or is tenant workspace-only access the
  long-term rule?
