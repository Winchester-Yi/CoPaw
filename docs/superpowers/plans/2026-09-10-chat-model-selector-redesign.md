# Chat Model Selector Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Chat's nested model menu with a responsive card selector that persists tenant-wide active-model and thinking defaults.

**Architecture:** Keep `ProviderInfo`, `ModelRuntimeConfig`, `providerApi`, and `useProviderModelStore` as the only data interfaces. The Chat selector flattens configured provider models into cards, derives the active card's runtime configuration, and persists user changes through the existing APIs. The Provider merge behavior retains a selected reasoning effort when a thinking toggle is disabled; request adaptation remains outside this change.

**Tech Stack:** React 18, TypeScript, Ant Design, Less CSS Modules, Vitest/Testing Library; Python, Pydantic, pytest.

---

### Task 1: Preserve stored effort when thinking is disabled

**Files:**
- Modify: `tests/unit/providers/test_model_configs.py`
- Modify: `src/swe/providers/provider.py:255-279`

- [ ] **Step 1: Write the failing Provider regression test**

Add this test after `test_provider_updates_one_model_config_without_legacy_generate_kwargs`:

```python
def test_provider_disabling_thinking_preserves_reasoning_effort() -> None:
    provider = _provider()
    provider.update_model_config(
        "gpt-5",
        {
            "supports_enable_thinking": True,
            "supported_reasoning_efforts": ["low", "high"],
            "enable_thinking": True,
            "reasoning_effort": "high",
        },
    )

    updated = provider.update_model_config("gpt-5", {"enable_thinking": False})

    assert updated.enable_thinking is False
    assert updated.reasoning_effort == "high"
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
venv/bin/python -m pytest tests/unit/providers/test_model_configs.py::test_provider_disabling_thinking_preserves_reasoning_effort -v
```

Expected: FAIL because `Provider.update_model_config` clears `reasoning_effort` when `enable_thinking` becomes false.

- [ ] **Step 3: Remove only the clearing branch**

In `Provider.update_model_config`, retain the supported-effort validation branch but delete the branch that sets `reasoning_effort` to `None` when `enable_thinking` is false:

```python
if "supported_reasoning_efforts" in updates:
    supported = updates.get("supported_reasoning_efforts") or []
    selected = updates.get("reasoning_effort", current.reasoning_effort)
    if selected not in supported:
        updates = {**updates, "reasoning_effort": None}
updated = ModelRuntimeConfig.model_validate(
    {**current.model_dump(), **updates},
)
```

Do not alter `ModelRuntimeConfig.generation_kwargs`; it already omits the stored effort when a thinking-capable model is disabled.

- [ ] **Step 4: Run focused Provider tests and verify GREEN**

Run:

```bash
venv/bin/python -m pytest tests/unit/providers/test_model_configs.py -v
```

Expected: PASS, including the preservation regression and existing request-argument assertions.

- [ ] **Step 5: Commit the isolated behavior change**

```bash
git add src/swe/providers/provider.py tests/unit/providers/test_model_configs.py
git commit -m "fix(models): preserve thinking effort when disabled"
```

### Task 2: Cover the Chat selector's configured-model interactions

**Files:**
- Create: `console/src/pages/Chat/ModelSelector/index.test.tsx`
- Modify: `console/src/pages/Chat/ModelSelector/index.tsx`

- [ ] **Step 1: Write failing component tests using the actual selector**

Mock `react-i18next`, `useAppMessage`, `providerApi`, `useProviderModelStore`, and `useLocation`. Use a store fixture with one thinking-capable OpenAI model, one reasoning-only Qwen model, and one model without thinking capabilities. Include tests with the following assertions:

```tsx
it("renders model id and provider-model metadata for every configured model", () => {
  render(<ModelSelector />);
  fireEvent.click(screen.getByText("gpt-5"));

  expect(screen.getByText("gpt-5")).toBeInTheDocument();
  expect(screen.getByText("OpenAI · GPT-5")).toBeInTheDocument();
});

it("shows unselected declared efforts and persists the selected tenant default", async () => {
  render(<ModelSelector />);
  fireEvent.click(screen.getByText("gpt-5"));
  fireEvent.click(screen.getByRole("button", { name: "高" }));

  await waitFor(() =>
    expect(providerApi.updateModelRuntimeConfig).toHaveBeenCalledWith(
      "openai",
      "gpt-5",
      { reasoning_effort: "high" },
    ),
  );
  expect(screen.getByText("平衡推理效果与速度")).toBeInTheDocument();
});

it("keeps a selected effort visible but disabled while thinking is off", () => {
  render(<ModelSelector />);
  fireEvent.click(screen.getByText("gpt-5"));

  expect(screen.getByRole("button", { name: "高" })).toBeDisabled();
  expect(screen.getByText("平衡推理效果与速度")).toBeInTheDocument();
});

it("shows only effort controls for a reasoning-only model", async () => {
  render(<ModelSelector />);
  fireEvent.click(screen.getByText("qwen-max"));

  await waitFor(() => expect(providerApi.setActiveLlm).toHaveBeenCalled());
  expect(screen.queryByText("思考模式")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "低" })).toBeEnabled();
});
```

Also assert that selecting a model calls `setActiveLlm` with `scope: "global"`, refreshes `loadModelData({ scope: "effective" })`, and that an API rejection restores the previous active card and calls `message.error`.

- [ ] **Step 2: Run the new component test and verify RED**

Run:

```bash
cd console && pnpm test:run src/pages/Chat/ModelSelector/index.test.tsx
```

Expected: FAIL because the existing selector renders nested provider menus and does not expose model card metadata, buttons, or the new loading/rollback behavior.

- [ ] **Step 3: Replace nested provider menu state with flat selectable cards**

In `ModelSelector`, use a memoized flat list containing `{ providerId, providerName, model }` for every eligible provider model. Render each item as a semantic `button`, with its active status indicated using `aria-pressed`, the `model.id` title, `provider.name · model.name` metadata, a provider icon from `providerIcon(providerId)`, and an explicit fallback image alt text.

Keep `Dropdown` as the existing open/close owner. The card click handler must:

```tsx
await providerApi.setActiveLlm({
  provider_id: providerId,
  model: modelId,
  scope: "global",
});
await loadModelData({ scope: "effective" });
window.dispatchEvent(new CustomEvent("model-switched"));
```

Set the saving flag around the complete sequence. On failure, do not mutate the store locally; retain its prior active-model data and call the current `message.error` path. Do not show a success message.

- [ ] **Step 4: Render capability-driven configuration controls**

Derive `hasThinkingConfiguration` from the active model's runtime config. If false, render only the model-list region. If true, render a `模型配置` region:

```tsx
const reasoningOptions = runtimeConfig.supported_reasoning_efforts.map(
  (effort) => ({
    effort,
    label: REASONING_EFFORT_LABELS[effort],
  }),
);
const effortDisabled =
  runtimeConfig.supports_enable_thinking && !runtimeConfig.enable_thinking;
```

Render the existing Ant Design `Switch` only if `supports_enable_thinking` is true. Render every declared effort as an Ant Design `Button`, give it an accessible name from the Chinese label, use `type="primary"` only for the selected effort, and set `disabled={saving || effortDisabled}`. Keep an empty selection unselected and show “请选择思考强度”. Map an effort to the approved helper copy and update through `updateModelRuntimeConfig` only after a click.

When a config update fails, restore the previous runtime config and use the current error message. Successful updates call `setModelRuntimeConfig` with the response and do not show a success message.

- [ ] **Step 5: Run component tests and verify GREEN**

Run:

```bash
cd console && pnpm test:run src/pages/Chat/ModelSelector/index.test.tsx
```

Expected: PASS for card metadata, empty effort selection, disabled preserved effort, reasoning-only configuration, activation persistence, refresh, and rollback.

- [ ] **Step 6: Commit the tested component behavior**

```bash
git add console/src/pages/Chat/ModelSelector/index.tsx console/src/pages/Chat/ModelSelector/index.test.tsx
git commit -m "feat(chat): redesign model selector interactions"
```

### Task 3: Implement the reference-led responsive visual treatment

**Files:**
- Modify: `console/src/pages/Chat/ModelSelector/index.module.less`
- Modify: `console/src/locales/zh.json`

- [ ] **Step 1: Write failing component assertions for configuration visibility and helper copy**

Extend `index.test.tsx` with:

```tsx
it("uses the full model panel when the active model has no thinking capability", async () => {
  render(<ModelSelector />);
  fireEvent.click(screen.getByText("plain-model"));

  await waitFor(() => expect(providerApi.setActiveLlm).toHaveBeenCalled());
  expect(screen.queryByText("模型配置")).not.toBeInTheDocument();
  expect(screen.queryByText("思考模式")).not.toBeInTheDocument();
});
```

Add locale keys for all headings, effort labels, and effort helper copy, then use translation keys rather than inline Chinese UI text.

- [ ] **Step 2: Run the component test and verify RED**

Run:

```bash
cd console && pnpm test:run src/pages/Chat/ModelSelector/index.test.tsx
```

Expected: FAIL until the active-card configuration visibility and translated strings are implemented.

- [ ] **Step 3: Replace legacy menu styles with the responsive card-panel styles**

Replace the nested `.providerItem` and `.submenu` styles with scoped classes for the panel, model list, card, card icon, metadata, split configuration region, option group, selected option, and helper copy. Use the Conversation Workspace's `#3769FC` emphasis with a subtle, non-decorative selected background; do not introduce global styles, a font dependency, or an external brand asset.

Use a CSS grid for wide panels:

```less
.panelWithConfig {
  display: grid;
  grid-template-columns: minmax(300px, 1fr) minmax(280px, 0.95fr);
  max-width: min(760px, calc(100vw - 24px));
}

.modelList {
  max-height: 356px;
  overflow-y: auto;
}

@media (max-width: 640px) {
  .panelWithConfig {
    grid-template-columns: minmax(0, 1fr);
  }
}
```

Use visible `:focus-visible`, disabled, loading, hover, and selected states. Ensure long provider names, model IDs, and metadata ellipsize instead of enlarging the panel. Preserve existing dark-mode compatibility only as needed; do not extend it with new visual rules.

- [ ] **Step 4: Run component tests and verify GREEN**

Run:

```bash
cd console && pnpm test:run src/pages/Chat/ModelSelector/index.test.tsx
```

Expected: PASS, including the no-thinking full-panel behavior.

- [ ] **Step 5: Commit translations and styles**

```bash
git add console/src/pages/Chat/ModelSelector/index.module.less console/src/locales/zh.json console/src/pages/Chat/ModelSelector/index.test.tsx
git commit -m "style(chat): add responsive model configuration panel"
```

### Task 4: Verify and review the integrated change

**Files:**
- Verify: `src/swe/providers/provider.py`
- Verify: `tests/unit/providers/test_model_configs.py`
- Verify: `console/src/pages/Chat/ModelSelector/index.tsx`
- Verify: `console/src/pages/Chat/ModelSelector/index.test.tsx`
- Verify: `console/src/pages/Chat/ModelSelector/index.module.less`

- [ ] **Step 1: Run focused regression suites**

Run:

```bash
venv/bin/python -m pytest tests/unit/providers/test_model_configs.py -v
cd console && pnpm test:run src/pages/Chat/ModelSelector/index.test.tsx src/stores/providerModelStore.test.ts
```

Expected: both commands exit 0.

- [ ] **Step 2: Run Console static checks and production build**

Run:

```bash
cd console && pnpm typecheck
cd console && pnpm lint src/pages/Chat/ModelSelector/index.tsx src/pages/Chat/ModelSelector/index.test.tsx
cd console && pnpm build
```

Expected: each command exits 0; resolve type, lint, and build errors before continuing.

- [ ] **Step 3: Inspect the rendered interaction**

Start the Console using the repository's dev command, open the Chat page, and verify: reference-like two-column desktop panel; card selection; 3–4-card list scroll; thinking toggle; preserved disabled effort; unselected effort prompt; reasoning-only model; no-capability full width; narrow stacked layout; Escape/outside close; long IDs; and error rollback. Record any visual discrepancy and correct it before completion.

- [ ] **Step 4: Run graph change analysis**

Run:

```bash
node .gitnexus/run.cjs detect-changes --scope all --repo .
```

Expected: a complete, non-truncated result. Review all changed-symbol consumers before committing the final verification fix, if any.

- [ ] **Step 5: Perform the CoPaw frontend quality review**

Review the changed Chat selector against `console/DESIGN.md` and the `copaw-f2e-review` checklist: accessible semantic buttons and focus, no color-only state, stable API/store contract, resilient long values, responsive layout, disabled/loading/error states, and no broad visual-system changes.

- [ ] **Step 6: Commit final verification adjustments only if needed**

If review finds no defect, make no extra commit. Otherwise stage only the
affected files listed in Tasks 1–3 and commit:

```bash
git commit -m "fix(chat): polish model selector states"
```

Do not stage or commit `CONTEXT.md` or the parallel Provider
request-adaptation work unless its owner explicitly asks to include it.
