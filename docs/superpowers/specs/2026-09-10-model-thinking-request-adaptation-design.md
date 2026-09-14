# Model Thinking Request Adaptation

## Goal

Map the tenant-scoped thinking configuration to the request shape required by
each OpenAI-compatible model family, without exposing provider API differences
to model configuration callers or individual execution entry points.

## Scope

`OpenAIProvider` will build generation arguments with the model ID and apply a
case-insensitive substring match. `deepseek`, `minimax`, and `qwen` select
their respective request forms; an unmatched ID uses the Qwen form.

| Model family | Thinking state | Reasoning effort |
| --- | --- | --- |
| DeepSeek | `extra_body.thinking.type`: `enabled` or `disabled` | Top-level `reasoning_effort`, only when thinking is enabled |
| MiniMax | `extra_body.thinking.type`: `adaptive` or `disabled` | Never sent |
| Qwen / fallback | `extra_body.enable_thinking`: boolean | `extra_body.reasoning_effort`, only when thinking is enabled |

The user-selected effort remains stored when thinking is disabled, but no
reasoning-effort field is sent until thinking is enabled again. Models that do
not declare thinking support do not receive a thinking-state field. Existing
sampling and output-length mappings remain unchanged.

## Data Flow

Every model creation route calls `Provider.build_generation_kwargs` before it
constructs the chat model. The provider maps the shared model configuration to
provider-ready arguments, and the OpenAI-compatible chat model passes those
arguments to the SDK. Supplying the model ID to this boundary makes the mapping
identical for Chat, SubAgent, Cron, Hook, and internal calls.

## Verification

Unit tests will first demonstrate the unsupported top-level
`enable_thinking` argument is absent. They will assert complete request
arguments for each model family, the unmatched fallback, disabled thinking,
and preserved sampling/output parameters.
