## 1. Add shared tenant path boundary primitives

- [x] 1.1 Add a shared helper for resolving paths against the current tenant root and rejecting paths outside `WORKING_DIR/<tenant_id>`
- [x] 1.2 Add unit tests covering tenant-root resolution, absolute-path rejection, `..` traversal rejection, missing-tenant-context failure, and symlink escape rejection

## 2. Enforce the boundary in builtin local path tools

- [x] 2.1 Refactor `read_file`, `write_file`, `edit_file`, and `append_file` to use the shared tenant path boundary before any filesystem access
- [x] 2.2 Refactor `send_file_to_user`, `grep_search`, `glob_search`, `view_image`, and `view_video` to use the shared tenant path boundary for target files or search roots
- [x] 2.3 Normalize user-facing permission-denied errors so tenant-boundary failures do not expose other tenants' resolved paths

## 3. Enforce explicit shell tenant path checks

- [x] 3.1 Validate shell `cwd` against the tenant root and reject execution when the effective working directory escapes `WORKING_DIR/<tenant_id>`
- [x] 3.2 Reuse or extract shell path-token parsing to reject commands whose explicit file paths resolve outside the current tenant root
- [x] 3.3 Add tests covering allowed tenant-local shell paths and denied cross-tenant `cwd`, relative traversal, and absolute-path access

## 4. Verify boundary coverage and follow-on safety behavior

- [x] 4.1 Add regression tests confirming search and media tools cannot read or enumerate sibling tenant directories
- [x] 4.2 Review and update any affected docs or prompts that currently imply builtin tools may fall back to global `WORKING_DIR`
- [x] 4.3 Optionally add tenant-boundary audit/logging integration with `tool_guard` or adjacent logging paths if richer operator visibility is needed
