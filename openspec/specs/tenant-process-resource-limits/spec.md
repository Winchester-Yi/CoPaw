# tenant-process-resource-limits Specification

## Purpose
TBD - created by archiving change tenant-process-resource-limits. Update Purpose after archive.
## Requirements
### Requirement: Built-in defaults SHALL define process launch policy
The system SHALL resolve process limits from the built-in `ProcessLimitsConfig` defaults without reading `security.process_limits` from any `config.json`. Tenant identity remains available for shell concurrency accounting.

#### Scenario: Configuration files do not change process limits
- **GIVEN** global or tenant configuration files contain process-limit settings
- **WHEN** any tenant-scoped shell or MCP `stdio` launch occurs
- **THEN** the system SHALL ignore those settings and use the built-in defaults
- **AND** shell CPU time SHALL default to 90 seconds and virtual memory to 34000 MiB
- **AND** shell enforcement SHALL be enabled and MCP `stdio` enforcement disabled by default

#### Scenario: Shell concurrency uses built-in settings and tenant slots
- **WHEN** a tenant-scoped builtin shell execution starts
- **THEN** the system SHALL use the built-in limit of five concurrent executions per tenant and a five-second slot wait
- **AND** the system SHALL count the execution against that tenant's local slots

### Requirement: Shell subprocesses SHALL honor configured per-process ceilings
The system SHALL apply configured per-process CPU time and memory ceilings to tenant-scoped builtin shell subprocess launches on supported Unix platforms.

#### Scenario: Shell launch applies configured CPU time and memory ceilings
- **GIVEN** tenant process limits are enabled for shell launches
- **WHEN** the system starts a tenant-scoped builtin shell command on a supported Unix platform
- **THEN** the child process SHALL start with the configured CPU time ceiling
- **AND** the child process SHALL start with the configured memory ceiling

#### Scenario: Shell subprocess exceeding CPU time ceiling is terminated
- **GIVEN** tenant process limits are enabled for shell launches with a low CPU time ceiling
- **WHEN** a tenant-scoped builtin shell command consumes more CPU time than the configured limit
- **THEN** the operating system SHALL terminate the subprocess
- **AND** the builtin shell tool SHALL return a failure result indicating the command exceeded process limits

#### Scenario: Shell subprocess exceeding memory ceiling is terminated
- **GIVEN** tenant process limits are enabled for shell launches with a low memory ceiling
- **WHEN** a tenant-scoped builtin shell command exceeds the configured memory ceiling
- **THEN** the operating system SHALL terminate or fail the subprocess
- **AND** the builtin shell tool SHALL return a failure result indicating the command exceeded process limits

### Requirement: Builtin shell execution SHALL enforce per-tenant local concurrency slots
The system SHALL limit concurrent tenant-scoped builtin shell tool calls inside one Swe backend process with `security.process_limits.shell_max_concurrent`.

#### Scenario: Shell execution acquires and releases a tenant slot
- **GIVEN** tenant process limits are enabled for shell launches with `shell_max_concurrent=5`
- **WHEN** a tenant-scoped builtin shell command starts
- **THEN** the shell tool SHALL acquire one tenant shell execution slot before launching the subprocess
- **AND** the shell tool SHALL release that slot only after the shell tool returns and Unix process group cleanup has completed

#### Scenario: Shell execution waits briefly for a busy tenant slot
- **GIVEN** tenant `tenant-a` already has `shell_max_concurrent` builtin shell commands in flight in the same Swe backend process
- **WHEN** another builtin shell command starts for `tenant-a`
- **THEN** the shell tool SHALL wait up to `security.process_limits.shell_acquire_timeout_seconds` for a tenant shell execution slot
- **AND** if no slot becomes available, the shell tool SHALL fail before launching a subprocess with a shell concurrency limit failure

#### Scenario: Shell slot limits do not count forked OS child processes
- **GIVEN** one builtin shell command runs a script that forks child processes
- **WHEN** the system evaluates tenant shell execution slots
- **THEN** the script and its forked child processes SHALL count as one in-flight builtin shell execution
- **AND** this capability SHALL NOT require counting every OS process forked by the script

### Requirement: Builtin shell execution SHALL clean up its Unix process group
The system SHALL terminate remaining processes in the Unix process group created for tenant-scoped builtin shell execution before the tool call is considered complete.

#### Scenario: Successful shell command leaves a background process
- **WHEN** a tenant-scoped builtin shell command exits successfully after starting a background process in the same Unix process group
- **THEN** the shell tool SHALL terminate the remaining process group before returning success

#### Scenario: Failed shell command leaves a background process
- **WHEN** a tenant-scoped builtin shell command fails after starting a background process in the same Unix process group
- **THEN** the shell tool SHALL terminate the remaining process group before returning failure

### Requirement: MCP `stdio` subprocesses SHALL honor the same configured ceilings
The system SHALL apply the same built-in per-process CPU time and memory ceilings when enabled in code to tenant-scoped MCP `stdio` server subprocess launches, including rebuild paths that reconnect a stdio client from stored metadata.

#### Scenario: Initial MCP `stdio` launch uses tenant process limits
- **GIVEN** tenant process limits are enabled for MCP `stdio` launches
- **WHEN** the system creates a tenant-scoped MCP `stdio` client from tenant config
- **THEN** the launched MCP server subprocess SHALL inherit the configured CPU time ceiling
- **AND** the launched MCP server subprocess SHALL inherit the configured memory ceiling

#### Scenario: Rebuilt MCP `stdio` launch preserves tenant process limits
- **GIVEN** a tenant-scoped MCP `stdio` client stores rebuild metadata
- **AND** tenant process limits are enabled for MCP `stdio` launches
- **WHEN** the system rebuilds that MCP client from stored metadata
- **THEN** the rebuilt MCP server subprocess SHALL launch with the same built-in process-limit policy

### Requirement: Process-limit enforcement SHALL preserve current scope boundaries
The system SHALL apply tenant process limits only to tenant-scoped builtin shell launches and tenant-scoped MCP `stdio` launches in this capability.

#### Scenario: Out-of-scope platform-managed subprocess is not covered by tenant process-limit policy
- **WHEN** the system starts an out-of-scope platform-managed subprocess such as a local model runtime, tunnel helper, or CLI maintenance worker
- **THEN** this capability SHALL NOT require tenant process limits to be applied to that subprocess

#### Scenario: Existing shell wall-clock timeout behavior remains in effect
- **WHEN** a tenant-scoped builtin shell command runs with process limits enabled
- **THEN** the system SHALL continue enforcing the existing wall-clock timeout behavior independently from CPU time limits

### Requirement: Unsupported platforms SHALL not silently claim enforcement
The system SHALL avoid silently claiming process-limit enforcement on unsupported platforms.

#### Scenario: Unsupported platform leaves launch behavior unchanged with diagnostics
- **GIVEN** tenant process limits are enabled
- **WHEN** an in-scope subprocess launch occurs on a platform where this capability does not enforce process limits
- **THEN** the system SHALL leave subprocess launch behavior unchanged
- **AND** the system SHALL emit diagnostics indicating that process limits were not enforced on that platform
