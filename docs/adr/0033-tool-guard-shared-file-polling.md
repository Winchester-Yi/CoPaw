# Tool Guard uses shared-file polling for cross-instance propagation

Tool Guard security policy remains stored in the shared `config.json`; every application process polls that file on a fixed 30-second interval and reloads its local engine after a valid change. We deliberately do not add Redis, database notifications, status endpoints, or synchronization logs; atomic same-directory replacement prevents readers from observing a partially written file, and a failed reload keeps the last successful rules active for a later retry.

**Consequences**

- Propagation is eventual: the saving process reloads immediately and other processes converge within one polling interval.
- In-flight tool calls retain their existing rule snapshot; only later calls observe the reload.
- There is no centralized confirmation that every Pod has converged.
