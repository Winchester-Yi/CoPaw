# Chat Model Selector Redesign

## Goal

Replace the chat model selector's nested provider menu with a reference-led
model card panel that makes a tenant's active model and its thinking defaults
easy to inspect and change.

## Scope

- Keep the existing chat model-name trigger, outside-click dismissal, and
  Escape-key dismissal.
- List every configured model as a card. A card's main line is `model.id`; its
  metadata line is `provider.name · model.name`.
- Selecting a card immediately persists the tenant's active model. The
  thinking configuration on the right always belongs to that newly active
  model.
- Render the thinking configuration only when the active model declares a
  thinking toggle or at least one supported reasoning effort.
- Persist Chat changes through the existing per-model runtime configuration
  endpoint. They are tenant-wide defaults for later model calls, never
  per-message or per-session overrides.
- Preserve a selected reasoning effort when a thinking-capable model is
  turned off. The control becomes visible but disabled; subsequent enablement
  restores it. Runtime request adaptation continues to determine whether the
  value is sent.
- For reasoning-only models, show only the reasoning-effort control and leave
  it enabled.
- When an effort has not been selected, show all declared options with no
  selection and the prompt “请选择思考强度”. Opening the panel must not persist
  a default.
- Map `low`, `high`, and `max` to “低”, “高”, and “极高”; show only declared
  values and distribute them evenly. Supporting text is respectively “更快响
  应”, “平衡推理效果与速度”, and “优先推理质量”.
- Use selection state as successful-save feedback. On failed model selection
  or configuration update, restore the latest successfully saved state and
  show the existing error message pattern.

## UI And Responsive Behavior

The panel follows the supplied visual reference: large model cards, a soft
active treatment, a quiet divider, and a distinct thinking configuration
region. It uses the Conversation Workspace emphasis color and existing local
icon sources or a neutral fallback instead of importing external brand assets.

On wide hosts, the model list and thinking configuration are side-by-side. On
narrow hosts, they stack with models above configuration. The model list is
independently scrollable after roughly three to four visible cards, while the
configuration stays visible. A model without either declared thinking
capability uses the full panel width with no empty configuration area.

## Data And State

`useProviderModelStore` remains the source of provider catalog data and the
tenant's active model. `providerApi.setActiveLlm` persists model selection;
`providerApi.updateModelRuntimeConfig` persists thinking defaults. A
successful update merges the returned config into the store. A successful
model selection refreshes catalog and active-model data before showing the
right configuration, preventing a previous model's values from appearing
under the new selection.

No API schema, provider capability field, authorization contract, or runtime
request mapping changes as part of this UI redesign. The only backend behavior
change is preserving `reasoning_effort` in the stored configuration when a
thinking toggle is turned off; the existing generation logic continues to
omit it while disabled.

## Verification

Component tests cover the card labels, capability-dependent configuration
visibility, effort labels and empty selection, disabled preserved effort,
reasoning-only behavior, immediate active-model persistence, and error
rollback. Provider unit tests cover preserving the selected reasoning effort
on a disabled thinking update. Focused Vitest and pytest runs, Console
typecheck/lint/build, and a browser review validate the changed surface.
