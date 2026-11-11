import { beforeEach, expect, it, vi } from "vitest";
import { request } from "@/api/request";
import {
  fetchInsuranceTracking,
  joinInsuranceTracking,
  removeInsuranceTracking,
  saveInsuranceChange,
  InsuranceSyncError,
} from "./api";
import type { Customer } from "../../types";
vi.mock("@/api/request", () => ({ request: vi.fn() }));
vi.mock("@/utils/identity", () => ({ getUserId: () => "qa-user" }));
vi.mock("../../operations", () => ({
  getWorkbenchContext: () => ({
    bbk: "secondary-100",
    orgCode: "branch-10001",
    positionId: "RB0101",
  }),
}));
beforeEach(() => {
  vi.resetAllMocks();
});
const customer: Customer = {
  id: "insurance|c1",
  custUid: "c1",
  skillId: "insurance",
  bbkOrgId: "customer-first-bbk",
  name: "林芳",
  task: "保险初筛识别",
  category: "保险",
  label: "",
  reason: "",
  done: false,
  channel: "",
  time: "",
  note: "",
  dynamicFields: [{ name: "aum", label: "资产", value: "0" }],
};
const values = {
  insurancePlan: "家庭保障",
  intention: 0 as const,
  contactPlans: [
    { date: "2026-11-07", method: 0 as const, content: "企微沟通" },
  ],
};
const wireRow = () => ({
  id: 1,
  sapId: "qa-user",
  bbkId: "manager-first-bbk",
  custUid: "c1",
  custBbkOrgId: "customer-first-bbk",
  custName: "林芳",
  aum: "0",
  skillId: "insurance",
  skillName: "保险初筛识别",
  bxPlan: "家庭保障",
  custIntention: 3,
  touchList: [
    {
      id: 10,
      traceId: 1,
      type: 0,
      touchPlan: "企微沟通",
      touchType: 0,
      touchDate: "2026-11-07",
    },
  ],
  traceList: [
    {
      id: 11,
      traceId: 1,
      type: 1,
      tracePlan: "客户待跟进",
      traceType: 1,
      traceTime: "2026-11-05 10:00:00",
      touched: 0,
    },
  ],
});
const queryResult = () => ({
  code: "200",
  data: [{ sapId: "qa-user", custTraceList: [wireRow()] }],
});
function body(index = 0) {
  return JSON.parse(String(vi.mocked(request).mock.calls[index][1]?.body));
}
async function listedCustomer() {
  vi.mocked(request).mockResolvedValueOnce(queryResult());
  return (await fetchInsuranceTracking()).customers[0];
}

it("按当前客户经理及二级分行查询，解包分组、映射主从字段并计算全量人数", async () => {
  vi.mocked(request).mockResolvedValueOnce(queryResult());
  const result = await fetchInsuranceTracking();
  expect(request).toHaveBeenCalledWith(
    "/agent/workspace/cust-trace/query",
    expect.objectContaining({ method: "POST" }),
  );
  expect(body()).toEqual({
    items: [{ sapId: "qa-user", bbkId: "secondary-100" }],
  });
  expect(result.counts).toEqual({
    customers: 1,
    tracking: 1,
    highIntention: 1,
  });
  expect(result.customers[0]).toMatchObject({
    id: "1",
    custUid: "c1",
    name: "林芳",
    assets: "0",
    skillId: "insurance",
    sceneName: "保险初筛识别",
    bbkOrgId: "customer-first-bbk",
    insurancePlan: "家庭保障",
    intention: 3,
    contactPlans: [
      { id: "10", content: "企微沟通", method: 0, date: "2026-11-07" },
    ],
    followUps: [
      {
        id: "11",
        content: "客户待跟进",
        method: 1,
        date: "2026-11-05T10:00:00",
        result: 0,
      },
    ],
  });
});
it("空分组返回真实空名单；格式错误和业务错误不伪造空名单", async () => {
  vi.mocked(request).mockResolvedValueOnce({ code: 200, data: [] });
  expect((await fetchInsuranceTracking()).counts.tracking).toBe(0);
  vi.mocked(request).mockResolvedValueOnce({ code: 200, data: null });
  await expect(fetchInsuranceTracking()).rejects.toThrow("数据格式异常");
  vi.mocked(request).mockResolvedValueOnce({ code: 400, message: "查询失败" });
  await expect(fetchInsuranceTracking()).rejects.toThrow("查询失败");
});
it("批量新增携带姓名、原始AUM、客户归属机构与互斥touch字段，不传临时参数", async () => {
  vi.mocked(request).mockResolvedValueOnce({
    code: 200,
    data: { successCount: 1, skipCount: 0, skippedKeys: [] },
  });
  expect(await joinInsuranceTracking(customer, values)).toEqual({
    created: true,
  });
  expect(vi.mocked(request).mock.calls[0][0]).toBe(
    "/agent/workspace/cust-trace/batch-create",
  );
  expect(body()).toEqual({
    items: [
      {
        sapId: "qa-user",
        bbkId: "secondary-100",
        custBbkOrgId: "customer-first-bbk",
        custUid: "c1",
        custName: "林芳",
        aum: "0",
        skillId: "insurance",
        skillName: "保险初筛识别",
        bxPlan: "家庭保障",
        touched: 0,
        custIntention: 0,
        extList: [
          {
            type: 0,
            touchPlan: "企微沟通",
            touchType: 0,
            touchDate: "2026-11-07",
          },
        ],
      },
    ],
  });
});
it("已存在的客户按skipCount识别，零处理结果不报成功", async () => {
  vi.mocked(request).mockResolvedValueOnce({
    code: 200,
    data: { successCount: 0, skipCount: 1 },
  });
  expect(await joinInsuranceTracking(customer, values)).toEqual({
    created: false,
  });
  vi.mocked(request).mockResolvedValueOnce({
    code: 200,
    data: { successCount: 0, skipCount: 0 },
  });
  await expect(joinInsuranceTracking(customer, values)).rejects.toThrow(
    "缺少处理结果",
  );
});
it("主表更新只传ID和本次编辑字段，不修改锁定字段，零意向正常提交", async () => {
  const target = await listedCustomer();
  vi.mocked(request).mockResolvedValue({ code: 200, data: null });
  await saveInsuranceChange(target, {
    kind: "insurancePlan",
    value: "保障安排",
  });
  await saveInsuranceChange(target, { kind: "intention", value: 0 });
  expect(vi.mocked(request).mock.calls[1][0]).toBe(
    "/agent/workspace/cust-trace/update",
  );
  expect(body(1)).toEqual({ id: "1", bxPlan: "保障安排" });
  expect(body(2)).toEqual({ id: "1", custIntention: 0 });
});
it("新增接触计划成功null后回查正式ID，不发送trace组或requestId", async () => {
  const target = await listedCustomer();
  vi.mocked(request)
    .mockResolvedValueOnce({ code: 200, data: null })
    .mockResolvedValueOnce(queryResult());
  const saved = await saveInsuranceChange(target, {
    kind: "contactPlan",
    requestId: "local-only",
    value: values.contactPlans[0],
  });
  expect(body(1)).toEqual({
    traceId: "1",
    type: 0,
    touchPlan: "企微沟通",
    touchType: 0,
    touchDate: "2026-11-07",
  });
  expect(vi.mocked(request).mock.calls[1][0]).toBe(
    "/agent/workspace/cust-trace/ext/add",
  );
  expect(saved).toMatchObject({ records: [{ id: "10" }] });
  expect(body(2)).toEqual({
    items: [{ sapId: "qa-user", bbkId: "secondary-100" }],
  });
});
it("编辑单条跟进发送ext ID、技能和触达结果，时间转换为本地秒格式，null响应正常成功", async () => {
  const target = await listedCustomer();
  vi.mocked(request)
    .mockResolvedValueOnce({ code: 200, data: null })
    .mockResolvedValueOnce({
      code: 200,
      data: [
        {
          sapId: "qa-user",
          custTraceList: [
            {
              ...wireRow(),
              touched: 0,
              traceList: [
                {
                  ...wireRow().traceList[0],
                  tracePlan: "待下次沟通",
                  traceTime: "2026-11-05 14:30:00",
                },
              ],
            },
          ],
        },
      ],
    });
  const value = {
    date: "2026-11-05T14:30",
    method: 1 as const,
    content: "待下次沟通",
    result: 0 as const,
  };
  expect(
    await saveInsuranceChange(target, {
      kind: "followUp",
      recordId: "11",
      requestId: "local-only",
      value,
    }),
  ).toMatchObject({
    records: [{ ...value, id: "11", date: "2026-11-05T14:30:00" }],
  });
  expect(vi.mocked(request).mock.calls[1][0]).toBe(
    "/agent/workspace/cust-trace/ext/update",
  );
  expect(body(1)).toEqual({
    id: "11",
    type: 1,
    tracePlan: "待下次沟通",
    traceSkillId: "insurance",
    traceSkillName: "保险初筛识别",
    traceType: 1,
    traceTime: "2026-11-05 14:30:00",
    touched: 0,
  });
  expect(request).toHaveBeenCalledTimes(3);
  expect(vi.mocked(request).mock.calls[2][0]).toBe(
    "/agent/workspace/cust-trace/query",
  );
});
it("新增跟进与接触计划使用同一ext/add路径但只发送各自字段组", async () => {
  const target = await listedCustomer();
  vi.mocked(request)
    .mockResolvedValueOnce({ code: 200, data: null })
    .mockResolvedValueOnce(queryResult());
  await saveInsuranceChange(target, {
    kind: "followUp",
    requestId: "local-only",
    value: {
      date: "2026-11-05T10:00:00",
      method: 2,
      result: 1,
      content: "已沟通",
    },
  });
  expect(body(1)).toEqual({
    traceId: "1",
    type: 1,
    tracePlan: "已沟通",
    traceType: 2,
    traceTime: "2026-11-05 10:00:00",
    traceSkillId: "insurance",
    traceSkillName: "保险初筛识别",
    touched: 1,
  });
});
it("新增已成功但回查失败抛出专用同步错误；写入失败不会继续查询", async () => {
  const target = await listedCustomer();
  const change = {
    kind: "contactPlan" as const,
    requestId: "local-only",
    value: values.contactPlans[0],
  };
  vi.mocked(request)
    .mockResolvedValueOnce({ code: 200, data: null })
    .mockRejectedValueOnce(new Error("回查网络异常"));
  await expect(saveInsuranceChange(target, change)).rejects.toBeInstanceOf(
    InsuranceSyncError,
  );
  vi.mocked(request).mockResolvedValueOnce({
    code: 404,
    message: "客户已移除",
  });
  await expect(saveInsuranceChange(target, change)).rejects.toThrow(
    "客户已移除",
  );
  expect(request).toHaveBeenCalledTimes(4);
});
it("删除以sapId、请求二级分行和客户UID定位，不使用查询返回的一级分行", async () => {
  const target = await listedCustomer();
  vi.mocked(request).mockResolvedValueOnce({ code: 200, data: null });
  await removeInsuranceTracking(target);
  expect(vi.mocked(request).mock.calls[1][0]).toBe(
    "/agent/workspace/cust-trace/batch-delete",
  );
  expect(body(1)).toEqual({
    items: [{ sapId: "qa-user", bbkId: "secondary-100", custUid: "c1" }],
  });
});
it("请求取消与网络错误向上抛出，不当作保存成功", async () => {
  vi.mocked(request).mockRejectedValueOnce(new Error("网络错误"));
  await expect(joinInsuranceTracking(customer, values)).rejects.toThrow(
    "网络错误",
  );
  const controller = new AbortController();
  controller.abort();
  vi.mocked(request).mockImplementationOnce(async (_url, options) => {
    expect(options?.signal?.aborted).toBe(true);
    throw new DOMException("aborted", "AbortError");
  });
  await expect(fetchInsuranceTracking(controller.signal)).rejects.toMatchObject(
    { name: "AbortError" },
  );
});

it("查询主表touched带入跟进结果，单条记录返回结果时优先使用单条", async () => {
  const row = wireRow();
  vi.mocked(request).mockResolvedValueOnce({
    code: 200,
    data: [
      {
        sapId: "qa-user",
        custTraceList: [
          {
            ...row,
            touched: 1,
            traceList: [
              { ...row.traceList[0], touched: undefined },
              { ...row.traceList[0], id: 12, touched: 0 },
            ],
          },
        ],
      },
    ],
  });
  const result = await fetchInsuranceTracking();
  expect(result.customers[0].followUps?.map((record) => record.result)).toEqual(
    [1, 0],
  );
});

it("加入时保留原客户清单的touched，不把已触达客户重置为未触达", async () => {
  vi.mocked(request).mockResolvedValueOnce({
    code: 200,
    data: { successCount: 1, skipCount: 0 },
  });
  await joinInsuranceTracking({ ...customer, touched: 1 }, values);
  expect(body().items[0].touched).toBe(1);
});

it("跟进结果先取列表值；修改后同步其他主表回填值，下次编辑不会把旧值写回且仍可再次修改", async () => {
  const traceList = [
    { ...wireRow().traceList[0], id: 11, touched: undefined },
    {
      ...wireRow().traceList[0],
      id: 12,
      traceTime: "2026-11-04 10:00:00",
      touched: undefined,
    },
  ];
  const response = (touched: number) => ({
    code: 200,
    data: [
      {
        sapId: "qa-user",
        custTraceList: [{ ...wireRow(), touched, traceList }],
      },
    ],
  });
  vi.mocked(request).mockResolvedValueOnce(response(1));
  const customer = (await fetchInsuranceTracking()).customers[0];
  expect(customer.followUps?.map((entry) => entry.result)).toEqual([1, 1]);
  vi.mocked(request)
    .mockResolvedValueOnce({ code: 200, data: null })
    .mockResolvedValueOnce(response(0));
  const saved = await saveInsuranceChange(customer, {
    kind: "followUp",
    recordId: "12",
    requestId: "local-only",
    value: {
      date: "2026-11-04T10:00:00",
      content: "改为待跟进",
      method: 1,
      result: 0,
    },
  });
  expect(saved).toMatchObject({
    records: [
      { id: "11", result: 0 },
      { id: "12", result: 0 },
    ],
  });
  if (!saved || !("records" in saved)) throw new Error("应回填最新跟进列表");
  const refreshedCustomer = { ...customer, followUps: saved.records };
  vi.mocked(request)
    .mockResolvedValueOnce({ code: 200, data: null })
    .mockResolvedValueOnce(response(0));
  const latest = refreshedCustomer.followUps[0];
  if (!("result" in latest) || latest.result == null || latest.method == null)
    throw new Error("应带入最新列表结果");
  await saveInsuranceChange(refreshedCustomer, {
    kind: "followUp",
    recordId: latest.id,
    requestId: "local-only",
    value: {
      ...latest,
      method: latest.method,
      result: latest.result,
      content: "只修改内容",
    },
  });
  expect(body(3).touched).toBe(0);
  vi.mocked(request)
    .mockResolvedValueOnce({ code: 200, data: null })
    .mockResolvedValueOnce(response(1));
  await saveInsuranceChange(refreshedCustomer, {
    kind: "followUp",
    recordId: latest.id,
    requestId: "local-only",
    value: { ...latest, method: latest.method, result: 1 },
  });
  expect(body(5).touched).toBe(1);
});

it("查询中的null或缺失方式保持未知，不默认企微；明确零值仍为企微", async () => {
  const row = wireRow();
  vi.mocked(request).mockResolvedValueOnce({
    code: 200,
    data: [
      {
        sapId: "qa-user",
        custTraceList: [
          {
            ...row,
            touchList: [
              { ...row.touchList[0], touchType: null },
              { ...row.touchList[0], id: 21, touchType: undefined },
              { ...row.touchList[0], id: 22, touchType: 0 },
            ],
            traceList: [
              { ...row.traceList[0], traceType: null },
              { ...row.traceList[0], id: 23, traceType: undefined },
              { ...row.traceList[0], id: 24, traceType: 0 },
            ],
          },
        ],
      },
    ],
  });
  const customer = (await fetchInsuranceTracking()).customers[0];
  expect(customer.contactPlans?.map((entry) => entry.method)).toEqual([
    undefined,
    undefined,
    0,
  ]);
  expect(customer.followUps?.map((entry) => entry.method)).toEqual([
    undefined,
    undefined,
    0,
  ]);
});

it("新增和编辑跟进均优先提交用户选择的场景，不能被主记录或历史技能覆盖", async () => {
  const customer = await listedCustomer();
  customer.followUps![0].skillId = "old-trace";
  customer.followUps![0].sceneName = "原跟进场景";
  const value = {
    date: "2026-11-05T10:00",
    method: 0 as const,
    result: 1 as const,
    content: "切换场景",
    skillId: "selected",
    sceneName: "选中场景",
  };
  for (const recordId of [undefined, "11"]) {
    vi.mocked(request)
      .mockResolvedValueOnce({ code: 200, data: null })
      .mockResolvedValueOnce(queryResult());
    await saveInsuranceChange(customer, {
      kind: "followUp",
      requestId: "local",
      recordId,
      value,
    });
  }
  expect(body(1)).toMatchObject({
    traceSkillId: "selected",
    traceSkillName: "选中场景",
  });
  expect(body(3)).toMatchObject({
    id: "11",
    traceSkillId: "selected",
    traceSkillName: "选中场景",
  });
});

it("加入跟踪原样携带原清单经营方案地址，查询返回filename后映射到预览地址", async () => {
  const filename =
    "https://wealth.example/reportView?resultId=plan-1&source=task";
  vi.mocked(request).mockResolvedValueOnce({
    code: 200,
    data: { successCount: 1, skipCount: 0 },
  });
  await joinInsuranceTracking({ ...customer, link: filename }, values);
  expect(body().items[0].filename).toBe(filename);
  vi.mocked(request).mockResolvedValueOnce({
    code: 200,
    data: [{ sapId: "qa-user", custTraceList: [{ ...wireRow(), filename }] }],
  });
  expect((await fetchInsuranceTracking()).customers[0].link).toBe(filename);
});

it("原清单没有经营方案地址时不构造filename，查询缺失地址继续显示占位", async () => {
  vi.mocked(request).mockResolvedValueOnce({
    code: 200,
    data: { successCount: 1, skipCount: 0 },
  });
  await joinInsuranceTracking(customer, values);
  expect(body().items[0]).not.toHaveProperty("filename");
  vi.mocked(request).mockResolvedValueOnce(queryResult());
  expect((await fetchInsuranceTracking()).customers[0].link).toBeUndefined();
});
