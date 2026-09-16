/**
 * 智能财富工作台 —— 数据访问层测试
 * 规划、场景与客户名单接口不做假数据回退：离线时读路径返回空列表、写路径上抛
 * （错误传播用例见 api.http-errors.test.ts）。
 * 本文件覆盖客户名单映射与草稿等内存行为。
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
  it("fetchBootstrap 返回空草稿；客户名单与触达历史均不在 bootstrap 内", async () => {
    const data = await api.fetchBootstrap("rm");
    expect(data.plans).toEqual([]);
    expect(data.draft).toEqual({ name: "", items: [] });
    expect(data.savedAt).toBe("");
    expect("customers" in data).toBe(false);
    expect("history" in data).toBe(false);
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

  it("fetchAvailableSceneCount 返回全部大类场景数；失败返回 null", async () => {
    mockRequest.mockResolvedValueOnce({
      items: [{ skillId: "a" }, { skillId: "b" }, { skillId: "c" }],
    });
    await expect(api.fetchAvailableSceneCount()).resolves.toBe(3);
    expect(String(mockRequest.mock.calls[0]?.[0])).toBe(
      "/wealth/scene-skills?category=",
    );

    mockRequest.mockRejectedValueOnce(new Error("boom"));
    await expect(api.fetchAvailableSceneCount()).resolves.toBeNull();
  });

  it("fetchSkillStats 按场景统计键映射结果；失败返回 null", async () => {
    mockRequest.mockResolvedValueOnce({
      items: [
        { skillId: "s1", targetCustomerCount: 125, generatedTaskCount: 30 },
        { skillId: "s2", targetCustomerCount: 89, generatedTaskCount: 17 },
      ],
    });
    const queries = [
      { skillId: "s1", startDate: "2026-09-01", endDate: "2026-09-30" },
      { skillId: "s2", startDate: "2026-09-05", endDate: "2026-09-15" },
    ];
    const stats = await api.fetchSkillStats(queries);
    expect(stats).toEqual({
      "s1|2026-09-01|2026-09-30": {
        targetCustomerCount: 125,
        generatedTaskCount: 30,
      },
      "s2|2026-09-05|2026-09-15": {
        targetCustomerCount: 89,
        generatedTaskCount: 17,
      },
    });
    const [path, options] = mockRequest.mock.calls[0] ?? [];
    expect(String(path)).toBe("/wealth/skill-stats");
    expect(JSON.parse(String((options as RequestInit).body))).toEqual({
      skills: queries,
    });

    mockRequest.mockRejectedValueOnce(new Error("boom"));
    await expect(api.fetchSkillStats(queries)).resolves.toBeNull();

    // 空查询不发请求
    await expect(api.fetchSkillStats([])).resolves.toEqual({});
  });

  it("fetchTodayCustomers 按任务上下文映射名单并去重", async () => {
    const customers = await api.fetchTodayCustomers(
      [
        TASK,
        { ...TASK }, // 同一技能重复任务只查一次、只出一条
      ],
      "10086",
      "business",
    );
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
    expect(String(nameListCalls[0]?.[0])).toContain("sap_id=10086");
  });

  it("fetchTodayCustomers 客户视角：一次查询不带 skillId，按客户聚合并标注命中场景", async () => {
    mockRequest.mockImplementation(async (path: unknown) => {
      const p = String(path);
      if (p.startsWith("/wealth/name-list")) {
        return {
          items: [
            { ...NAME_LIST_FIXTURE[0], skillIds: ["skill-loan-1"] },
            { ...NAME_LIST_FIXTURE[1], skillIds: ["skill-unknown"] },
          ],
        };
      }
      throw new Error(`未 mock 的请求：${p}`);
    });

    const customers = await api.fetchTodayCustomers(
      [TASK],
      "10086",
      "customer",
    );

    const nameListCalls = mockRequest.mock.calls.filter(([p]) =>
      String(p).startsWith("/wealth/name-list"),
    );
    expect(nameListCalls).toHaveLength(1);
    expect(String(nameListCalls[0]?.[0])).not.toContain("skill_id=");
    expect(String(nameListCalls[0]?.[0])).toContain("sap_id=10086");
    // 以 custUid 为页面 id；今日树内的技能映射为场景名标签，树外技能不产生标签
    expect(customers[0]).toMatchObject({
      id: "CUST001",
      name: "张三",
      label: "信贷需求挖掘",
      task: "信贷需求挖掘",
      category: "贷款",
    });
    expect(customers[1]).toMatchObject({ id: "CUST002", label: "", task: "" });
  });

  it("fetchPendingCustomers / fetchDoneCustomers 以 touched 区分名单口径", async () => {
    await api.fetchPendingCustomers([TASK], "10086");
    await api.fetchDoneCustomers([TASK], "10086");
    const calls = mockRequest.mock.calls.map(([p]) => String(p));
    expect(calls[0]).toContain("touched=0");
    expect(calls[1]).toContain("touched=1");
    // 两个口径都不带 skillId（客户视角粒度），都带 sapId
    for (const c of calls) {
      expect(c).not.toContain("skill_id=");
      expect(c).toContain("sap_id=10086");
    }
  });

  it("fetchDoneCustomers 名单全部为已触达，触达方式/时间置空待接口补字段", async () => {
    const done = await api.fetchDoneCustomers([TASK], "10086");
    expect(done).toHaveLength(2);
    expect(done.every((c) => c.done)).toBe(true);
    expect(done[0]).toMatchObject({ channel: "", time: "", note: "" });
  });

  it("resetMockDb 后草稿恢复初始（模拟刷新）", async () => {
    await api.saveDraft("rm", { name: "改过的草稿", items: [] });
    api.resetMockDb();
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
