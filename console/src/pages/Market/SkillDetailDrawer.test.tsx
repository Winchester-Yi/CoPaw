import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SkillDetailDrawer } from "./SkillDetailDrawer";
import type { MarketSkillDetail } from "../../api/modules/market";

const mocks = vi.hoisted(() => ({
  readSkillFile: vi.fn(),
  updateSkillCnName: vi.fn(),
  downloadSkill: vi.fn(),
  updateSkillStatisticsConfig: vi.fn(),
}));

vi.mock("../../api/modules/market", async () => {
  const actual = await vi.importActual<typeof import("../../api/modules/market")>(
    "../../api/modules/market",
  );
  return {
    ...actual,
    marketApi: mocks,
  };
});

function buildSkill(overrides: Partial<MarketSkillDetail> = {}): MarketSkillDetail {
  return {
    item_id: "market-item-1",
    skill_id: "skill-001",
    name: "demo_skill",
    skill_name: "demo_skill",
    chinese_name: "旧名称",
    description: "demo",
    version: "1.0.0",
    creator_id: "admin",
    creator_name: "管理员",
    category_id: null,
    bbk_ids: [],
    status: "active",
    created_at: null,
    updated_at: null,
    call_count: 0,
    user_count: 0,
    user_stats: [],
    ...overrides,
  };
}

describe("SkillDetailDrawer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.readSkillFile.mockResolvedValue({
      content: "# demo",
      path: "SKILL.md",
      exists: true,
    });
    mocks.downloadSkill.mockResolvedValue({
      blob: new Blob(["demo"]),
      filename: "demo.zip",
    });
    mocks.updateSkillStatisticsConfig.mockResolvedValue({
      success: true,
      message: "",
    });
  });

  it("opens the unified edit flow from the detail drawer", () => {
    const onEdit = vi.fn();

    render(
      <SkillDetailDrawer
        open
        skill={buildSkill()}
        onClose={vi.fn()}
        isManager
        sourceId="source-1"
        onEdit={onEdit}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    expect(onEdit).toHaveBeenCalledTimes(1);
  });
});
