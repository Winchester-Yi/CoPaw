# Workflow Scheduled Jobs Use an Independent Engine

Workflow Scheduled Jobs resolve one published Skill-associated binding and call
one synchronous JSON HTTP endpoint per run attempt. Their engine accepts an
explicit configuration and target identity; it does not build an Agent, invoke
AgentRunner, select an LLM, or reuse the agent/text CronExecutor as its HTTP
executor. CronManager remains the Scheduled Run owner: it binds source context,
controls concurrency, records the terminal execution, updates the task Chat and
unread state, and routes batch feedback and completion notification.

The alternative of routing workflow through AgentRunner would make the model
choose or call tools and change the run's success, persistence, and cost
semantics. Routing it through the text executor would couple HTTP configuration
and JSON result rendering to fixed-text delivery. A separate engine with a
shared Cron execution-result contract keeps those responsibilities explicit.

The shared binding has immutable published versions. Ordinary and manual runs
read the current version at execution start; Scheduler fixes the version and
`provider_id/model_id` when it creates a Dispatch Batch. Those model fields
identify the Dispatch Model Pool only. Each broadcast recipient uses the same
binding version and its own runtime-scope identity and credentials. Only
workflow jobs carry `workflow_binding_id`; agent and text jobs keep their
existing contracts.

Consequences: the binding/version tables and Monitor job projection must be
available before workflow batch dispatch. A trace for workflow records a Skill
invocation and terminal outcome without inventing LLM spans. The existing
Cron execution table remains the history source; it is not replaced by a
workflow-specific execution table.
