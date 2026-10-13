import { describe, expect, it } from "vitest";
import {
  formatCount,
  formatEstimate,
  formatRatio,
  stateLabel,
  parseDiagnosisFilters,
  updateDiagnosisSearch,
  validateReturnTo,
} from "./presentation";
describe("diagnosis presentation", () => {
  it("preserves null versus zero and estimated units", () => {
    expect(formatCount(null)).toBe("未知");
    expect(formatCount(0)).toBe("0");
    expect(formatEstimate(0)).toBe("估算 0 Token");
    expect(formatRatio(null)).toBe("不可用");
    expect(formatRatio(0)).toBe("0.0%");
    expect(stateLabel("partial")).toBe("部分采集");
    expect(stateLabel("stale")).toBe("历史结果（已落后）");
  });
  it("whitelists filters and keeps embedding parameters while invalidating pagination", () => {
    const search = new URLSearchParams(
      "hideMenu=true&source=HOST&model_name=old&cursor=abc&relation_cursor=def&call=c1",
    );
    const next = updateDiagnosisSearch(search, { model_name: "new" });
    expect(next.get("hideMenu")).toBe("true");
    expect(next.get("source")).toBe("HOST");
    expect(next.get("call")).toBe("c1");
    expect(next.has("cursor")).toBe(false);
    expect(next.has("relation_cursor")).toBe(false);
    expect(search.get("model_name")).toBe("old");
    expect(
      parseDiagnosisFilters(
        new URLSearchParams(
          "source=bad&rule_id=unknown&limit=999&model_name=A",
        ),
      ),
    ).toEqual({ model_name: "A" });
    expect(
      updateDiagnosisSearch(next, { cursor: "new-page" }).get("cursor"),
    ).toBe("new-page");
  });
  it("only accepts local analytics return links", () => {
    expect(
      validateReturnTo("/analytics/token-issues?cursor=abc&hideMenu=true"),
    ).toBe("/analytics/token-issues?cursor=abc&hideMenu=true");
    for (const path of [
      "https://evil.com/analytics",
      "//evil.com/analytics",
      "/chat",
      "/analytics/../../evil",
      "/analytics\\evil",
      "/analytics/%5cevil",
    ])
      expect(validateReturnTo(path)).toBeNull();
  });
});
