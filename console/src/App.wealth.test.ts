import { describe, expect, it } from "vitest";

import appSource from "./App.tsx?raw";

describe("WealthWorkbench routing", () => {
  it("renders outside the local AuthGuard", () => {
    expect(appSource).toMatch(
      /path="\/wealth\/\*"\s+element=\{<WealthWorkbench \/>\}/,
    );
  });
});
