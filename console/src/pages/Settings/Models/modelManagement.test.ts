import { describe, expect, it } from "vitest";
import { getRemoteProviders, isActiveModel } from "./modelManagement";

describe("model management", () => {
  it("filters local providers from management candidates", () => {
    const providers = [
      { id: "openai", is_local: false },
      { id: "copaw-local", is_local: true },
      { id: "legacy-local", is_local: true },
    ];

    expect(getRemoteProviders(providers)).toEqual([
      { id: "openai", is_local: false },
    ]);
  });

  it("marks a model active only when both provider and model match", () => {
    const active = { provider_id: "openai", model: "gpt-5" };

    expect(isActiveModel(active, "openai", "gpt-5")).toBe(true);
    expect(isActiveModel(active, "openai", "gpt-4")).toBe(false);
    expect(isActiveModel(active, "anthropic", "gpt-5")).toBe(false);
    expect(isActiveModel(undefined, "openai", "gpt-5")).toBe(false);
  });
});
