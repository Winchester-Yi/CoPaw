import { describe, expect, it } from "vitest";
import type { MarketSkill } from "../../api/modules/market";
import {
  getMarketSearchResultLabel,
  matchesMarketSkillSearch,
} from "./MarketSkills";

function buildSkill(overrides: Partial<MarketSkill> = {}): MarketSkill {
  return {
    item_id: "item-1",
    skill_id: "skill-1",
    name: "deposit_growth",
    chinese_name: "",
    description: "default description",
    version: "1.0.0",
    creator_id: "creator-1",
    creator_name: "creator",
    category_id: null,
    bbk_ids: [],
    status: "active",
    created_at: null,
    updated_at: null,
    call_count: 0,
    user_count: 0,
    ...overrides,
  };
}

describe("matchesMarketSkillSearch", () => {
  it("matches the Chinese display name", () => {
    const skill = buildSkill({
      chinese_name: "存款增长分析",
      name: "deposit_growth",
      description: "default description",
    });

    expect(matchesMarketSkillSearch(skill, "存款增长分析")).toBe(true);
  });

  it("trims the search query before matching", () => {
    const skill = buildSkill({
      chinese_name: "存款增长分析",
      name: "deposit_growth",
    });

    expect(matchesMarketSkillSearch(skill, "  存款增长分析  ")).toBe(true);
  });
});

describe("getMarketSearchResultLabel", () => {
  it("only shows a match count when a search query is present", () => {
    expect(getMarketSearchResultLabel("", 15)).toBeNull();
    expect(getMarketSearchResultLabel("deposit", 3)).toBe("匹配 3 个");
  });
});
