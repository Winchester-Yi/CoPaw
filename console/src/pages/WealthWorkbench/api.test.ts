/**
 * 智能财富工作台 —— 数据访问层测试
 * 规划与场景接口不做假数据回退：离线时读路径返回空列表、写路径上抛
 * （错误传播用例见 api.http-errors.test.ts）。
 * 本文件覆盖客户/触达/草稿等内存 mock 行为。
 */
import { beforeEach, describe, expect, it } from "vitest";
import * as api from "./api";

describe("WealthWorkbench api", () => {
  beforeEach(() => {
    api.resetMockDb();
  });

  it("fetchBootstrap 返回客户、历史与空草稿；规划接口不可达时 plans 为空", async () => {
    const data = await api.fetchBootstrap("rm");
    expect(data.plans).toEqual([]);
    expect(data.customers).toHaveLength(28);
    expect(data.history).toHaveLength(34);
    expect(data.draft).toEqual({ name: "", items: [] });
    expect(data.savedAt).toBe("");
  });

  it("fetchPlanList 规划接口不可达时返回空列表（不回退假数据）", async () => {
    expect(await api.fetchPlanList()).toEqual([]);
  });

  it("fetchScenesByCategory 场景接口不可达时返回空列表（不做假数据兜底）", async () => {
    expect(await api.fetchScenesByCategory("insurance")).toEqual([]);
    expect(await api.fetchScenesByCategory("")).toEqual([]);
  });

  it("saveDraft 按账户隔离草稿并记录保存时间", async () => {
    const draft = { name: "行长的草稿", items: [] };
    const { savedAt } = await api.saveDraft("president", draft);
    expect(savedAt).toMatch(/^\d{2}:\d{2}$/);
    const data = await api.fetchBootstrap("president");
    expect(data.draft.name).toBe("行长的草稿");
    expect(data.savedAt).toBe(savedAt);
    // 其他账户不受影响
    const rmData = await api.fetchBootstrap("rm");
    expect(rmData.draft).toEqual({ name: "", items: [] });
  });

  it("reportContact 登记触达结果", async () => {
    const result = await api.reportContact(
      { id: 1, channel: "电话", outcome: "done", note: "已沟通" },
      "2026-09-08",
    );
    const c = result.customers.find((x) => x.id === 1);
    expect(c?.done).toBe(true);
    expect(c?.note).toBe("已沟通");
    expect(c?.channel).toBe("电话");
    expect(c?.time.startsWith("2026-09-08 ")).toBe(true);
  });

  it("resetMockDb 后客户与草稿恢复初始（模拟刷新重置）", async () => {
    await api.reportContact(
      { id: 1, channel: "电话", outcome: "done", note: "已沟通" },
      "2026-09-08",
    );
    await api.saveDraft("rm", { name: "改过的草稿", items: [] });
    api.resetMockDb();
    const data = await api.fetchBootstrap("rm");
    expect(data.customers.find((c) => c.id === 1)?.done).toBe(false);
    expect(data.draft).toEqual({ name: "", items: [] });
  });
});
