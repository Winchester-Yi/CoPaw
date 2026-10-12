import { describe, expect, it } from "vitest";
import { composition, formatTokens, usageLabel } from "./presentation";

describe("Token coverage and composition", () => {
  it("keeps unknown usage distinct from an explicitly reported zero", () => {
    expect(formatTokens(null)).toBe("未知");
    expect(formatTokens(0)).toBe("0");
    expect(usageLabel(false, 0)).toBe("未知");
    expect(usageLabel(false, 100)).toBe("已知部分");
  });
  it("anchors estimates only when actual usage and capture are complete", () => {
    const categories = [
      { category: "system", estimated_tokens: 300 },
      { category: "tool_result", estimated_tokens: 700 },
    ];
    const anchored = composition(categories, 200, 50, true);
    expect(anchored.map((x) => x.value)).toEqual([60, 140, 50]);
    expect(anchored.reduce((sum, x) => sum + x.value, 0)).toBe(250);
    expect(
      composition(categories, 200, null, false).map((x) => x.value),
    ).toEqual([300, 700]);
  });
  it("does not manufacture category attribution for an empty estimate", () => {
    expect(composition([], 100, 2, true)).toEqual([]);
  });
});
