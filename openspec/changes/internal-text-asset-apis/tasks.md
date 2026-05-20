## 1. API Contracts

- [x] 1.1 Add internal and external read endpoints for `SWE_WORKING_DIR/asset/<file_name>` with UTF-8 decoding and 404 on missing files
- [x] 1.2 Add internal and external write endpoints that generate `user_id + timestamp + .html` file names, resolve `scope_id`, and write to `workspaces/default/static`
- [x] 1.3 Define response schemas for read and write success/error cases

## 2. Validation And URL Generation

- [x] 2.1 Add safe file-name validation for the asset read path
- [x] 2.2 Add UTF-8 content validation for read and write paths
- [x] 2.3 Generate permanent public URLs using `FILE_URL` and the existing `/static/{user_id}/{agent_id}/{file_name}` route shape

## 3. Tests

- [x] 3.1 Add tests for missing asset files returning 404
- [x] 3.2 Add tests for invalid asset file names and invalid UTF-8 payloads
- [x] 3.3 Add tests for generated file names, scope-aware storage, returned public URLs, and anonymous external routes

## 4. Review

- [x] 4.1 Verify the spec has no placeholders or contradictions
- [x] 4.2 Confirm the change is still scoped to text asset read/write only, with internal and external route layers
