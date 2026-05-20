## ADDED Requirements

### Requirement: Internal and external asset text read endpoints
The system SHALL provide an internal endpoint and a matching external endpoint that both read a UTF-8 text file from `SWE_WORKING_DIR/asset/<file_name>` and return its contents.

#### Scenario: Asset file exists
- **WHEN** the internal read endpoint is called with `file_name=guide.txt`
- **THEN** the system SHALL read `SWE_WORKING_DIR/asset/guide.txt`
- **AND** it SHALL return the file contents as UTF-8 text

#### Scenario: External read succeeds without internal token
- **WHEN** the external read endpoint is called with `file_name=guide.txt`
- **THEN** the system SHALL return the same UTF-8 content as the internal read endpoint
- **AND** it SHALL NOT require `X-Internal-Token`

#### Scenario: Asset file does not exist
- **WHEN** the internal read endpoint is called with a file name that does not exist under `SWE_WORKING_DIR/asset`
- **THEN** the system SHALL return HTTP 404

#### Scenario: Invalid file name is provided
- **WHEN** the internal read endpoint is called with a file name containing path traversal or separator characters
- **THEN** the system SHALL reject the request with HTTP 400

#### Scenario: Asset file is not valid UTF-8
- **WHEN** the internal read endpoint reads a file that cannot be decoded as UTF-8
- **THEN** the system SHALL reject the request with HTTP 400

### Requirement: Internal and external static text write endpoints
The system SHALL provide an internal endpoint and a matching external endpoint that accept `user_id`, `source_id`, and UTF-8 text content, generate an `.html` file name from `user_id + timestamp`, and write the content into the resolved tenant scope's `workspaces/default/static` directory.

#### Scenario: Write succeeds
- **WHEN** the internal write endpoint is called with `user_id=alice`, `source_id=portal`, and UTF-8 text content
- **THEN** the system SHALL resolve the runtime `scope_id` from `user_id` and `source_id`
- **AND** it SHALL create a server-generated `.html` file under `<WORKING_DIR>/<scope_id>/workspaces/default/static`
- **AND** it SHALL return the generated file name and permanent public URL

#### Scenario: External write succeeds without internal token
- **WHEN** the external write endpoint is called with `user_id=alice`, `source_id=portal`, and UTF-8 text content
- **THEN** the system SHALL create the same scope-aware static file as the internal write endpoint
- **AND** it SHALL return the generated file name and permanent public URL
- **AND** it SHALL NOT require `X-Internal-Token`

#### Scenario: File name includes user ID and timestamp
- **WHEN** the internal write endpoint generates a file name for `user_id=alice`
- **THEN** the generated file name SHALL include `alice`
- **AND** it SHALL include a server-generated timestamp component
- **AND** it SHALL end with `.html`

#### Scenario: Target directory does not exist
- **WHEN** the target tenant scope has not yet created `workspaces/default/static`
- **THEN** the system SHALL create the required parent directories on demand

#### Scenario: Content is not valid UTF-8
- **WHEN** the internal write endpoint receives content that cannot be treated as UTF-8 text
- **THEN** the system SHALL reject the request with HTTP 400

### Requirement: Permanent public URL generation
The system SHALL return a permanent public URL for each successfully written static text file using the existing static route shape and the configured `FILE_URL` base.

#### Scenario: URL generation succeeds
- **WHEN** a text file is written successfully for a resolved runtime `scope_id`
- **THEN** the returned URL SHALL point to `/static/<scope_id>/default/<generated_name>.html`
- **AND** the URL SHALL remain valid unless the file is removed by an out-of-band process

#### Scenario: File URL base is missing
- **WHEN** `FILE_URL` is not configured
- **THEN** the system SHALL fall back to the same default base used by `copy_file_to_static`

### Requirement: Internal and external route separation
The system SHALL keep the internal endpoints under the existing internal router boundary and expose separate external endpoints with the same read/write behavior.

#### Scenario: Missing internal token on internal route
- **WHEN** a caller invokes either internal endpoint without internal authorization while internal auth is enabled
- **THEN** the system SHALL reject the request with HTTP 401 according to the existing internal API convention

#### Scenario: External route remains callable without internal header
- **WHEN** a caller invokes either external endpoint without `X-Internal-Token`
- **THEN** the system SHALL process the request using the same validation and file-publishing rules as the internal endpoint
