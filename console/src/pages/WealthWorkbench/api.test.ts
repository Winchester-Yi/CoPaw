/**
 * 智能财富工作台 —— 数据访问层测试
 * 规划、场景与客户名单接口不做假数据回退：离线时读路径返回空列表、写路径上抛
 * （错误传播用例见 api.http-errors.test.ts）。
 * 本文件覆盖客户名单映射、触达登记覆盖层与草稿等内存行为。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import { request } from "../../api/request";

vi.mock("../../api/request", () => ({ request: vi.fn() }));

const mockRequest = vi.mocked(request);

const TASK = {
  skillId: "skill-loan-1",
  sceneName: "信贷需求挖掘",
  category: "贷款",
};

const NAME_LIST_FIXTURE = [
  {
    custUid: "CUST001",
    custNm: "张三",
    sapId: "10086",
    filename: "http://example/cust001",
    recomReason: "命中贷款核验规则",
  },
  {
    custUid: "CUST002",
    custNm: "李四",
    sapId: "10086",
    filename: "http://example/cust002",
    recomReason: "",
  },
];

beforeEach(() => {
  api.resetMockDb();
  mockRequest.mockReset();
  mockRequest.mockImplementation(async (path: unknown) => {
    const p = String(path);
    if (p.startsWith("/wealth/name-list")) {
      return { items: NAME_LIST_FIXTURE };
    }
    if (p.startsWith("/wealth/plans")) {
      return { items: [] };
    }
    throw new Error(`未 mock 的请求：${p}`);
  });
});

describe("WealthWorkbench api", () => {
  it("fetchBootstrap 返回历史与空草稿；客户名单不在 bootstrap 内", async () => {
    const data = await api.fetchBootstrap("rm");
    expect(data.plans).toEqual([]);
    expect(data.history).toHaveLength(34);
    expect(data.draft).toEqual({ name: "", items: [] });
    expect(data.savedAt).toBe("");
    expect("customers" in data).toBe(false);
  });

  it("fetchNameList 透传 skillId 与 sapId；接口失败返回空列表", async () => {
    const list = await api.fetchNameList("skill-loan-1", "10086");
    expect(list).toHaveLength(2);
    expect(mockRequest.mock.calls[0]?.[0]).toContain("skill_id=skill-loan-1");
    expect(mockRequest.mock.calls[0]?.[0]).toContain("sap_id=10086");

    await api.fetchNameList("skill-loan-1");
    expect(mockRequest.mock.calls[1]?.[0]).not.toContain("sap_id=");

    mockRequest.mockRejectedValueOnce(new Error("boom"));
    await expect(api.fetchNameList("skill-loan-1")).resolves.toEqual([]);
  });

  it("fetchTodayCustomers 按任务上下文映射名单并去重", async () => {
    const customers = await api.fetchTodayCustomers([
      TASK,
      { ...TASK }, // 同一技能重复任务只查一次、只出一条
    ]);
    expect(customers).toHaveLength(2);
    expect(customers[0]).toMatchObject({
      id: "skill-loan-1|CUST001",
      custUid: "CUST001",
      skillId: "skill-loan-1",
      name: "张三",
      task: "信贷需求挖掘",
      category: "贷款",
      reason: "命中贷款核验规则",
      link: "http://example/cust001",
      done: false,
    });
    const nameListCalls = mockRequest.mock.calls.filter(([p]) =>
      String(p).startsWith("/wealth/name-list"),
    );
    expect(nameListCalls).toHaveLength(1);
  });

  it("reportContact 写入覆盖层，重新拉取名单后回填触达结果", async () => {
    await api.fetchTodayCustomers([TASK]);
    const { customers } = await api.reportContact(
      {
        id: "skill-loan-1|CUST001",
        channel: "电话",
        outcome: "done",
        note: "已沟通",
      },
      "2026-09-14",
    );
    const hit = customers.find((c) => c.id === "skill-loan-1|CUST001");
    expect(hit?.done).toBe(true);
    expect(hit?.channel).toBe("电话");
    expect(hit?.time.startsWith("2026-09-14 ")).toBe(true);

    // 切换视角重新拉取后触达结果仍回填
    const again = await api.fetchTodayCustomers([TASK]);
    expect(again.find((c) => c.id === "skill-loan-1|CUST001")?.done).toBe(true);
    expect(again.find((c) => c.id === "skill-loan-1|CUST002")?.done).toBe(
      false,
    );
  });

  it("resetMockDb 后触达覆盖层与草稿恢复初始（模拟刷新）", async () => {
    await api.fetchTodayCustomers([TASK]);
    await api.reportContact(
      {
        id: "skill-loan-1|CUST001",
        channel: "电话",
        outcome: "done",
        note: "已沟通",
      },
      "2026-09-14",
    );
    await api.saveDraft("rm", { name: "改过的草稿", items: [] });
    api.resetMockDb();
    const customers = await api.fetchTodayCustomers([TASK]);
    expect(customers.find((c) => c.id === "skill-loan-1|CUST001")?.done).toBe(
      false,
    );
    const data = await api.fetchBootstrap("rm");
    expect(data.draft).toEqual({ name: "", items: [] });
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

  it("fetchPlanList 规划接口不可达时返回空列表（不回退假数据）", async () => {
    mockRequest.mockRejectedValue(new Error("boom"));
    expect(await api.fetchPlanList()).toEqual([]);
  });

  it("fetchScenesByCategory 场景接口不可达时返回空列表（不做假数据兜底）", async () => {
    mockRequest.mockRejectedValue(new Error("boom"));
    expect(await api.fetchScenesByCategory("insurance")).toEqual([]);
    expect(await api.fetchScenesByCategory("")).toEqual([]);
  });
});
