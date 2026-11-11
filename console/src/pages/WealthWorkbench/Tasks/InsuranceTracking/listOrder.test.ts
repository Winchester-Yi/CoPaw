import { expect, it } from "vitest";
import { orderCustomers } from "./listOrder";
import type { InsuranceCustomer } from "./api";
const rows: InsuranceCustomer[] = ["—", 10000, "5000", 0, "1万"].map(
  (assets, i) => ({
    id: String(i),
    custUid: String(i),
    name: String(i),
    assets,
    intention: 0,
  }),
);
it("金额直接按后端数值比较、不换算单位，零值参与排序，缺失或无法识别始终排末尾，且不修改原数据", () => {
  expect(
    orderCustomers(rows, { key: "assets", direction: "asc" }).map((r) => r.id),
  ).toEqual(["3", "2", "1", "0", "4"]);
  expect(
    orderCustomers(rows, { key: "assets", direction: "desc" }).map((r) => r.id),
  ).toEqual(["1", "2", "3", "0", "4"]);
  expect(orderCustomers(rows, null)).toBe(rows);
  expect(rows.map((r) => r.id)).toEqual(["0", "1", "2", "3", "4"]);
});
it("接触计划按最新一条的日期排序，空计划和无效日期始终排末尾", () => {
  const dates = [undefined, "2026-11-10", "2026-11-02", "bad", "2026-11-10"];
  const customers = rows.map((row, i) => ({
    ...row,
    contactPlans: dates[i]
      ? [{ id: row.id, date: dates[i]!, content: "计划", method: 0 as const }]
      : [],
  }));
  expect(
    orderCustomers(customers, { key: "contactPlan", direction: "asc" }).map(
      (r) => r.id,
    ),
  ).toEqual(["2", "1", "4", "0", "3"]);
  expect(
    orderCustomers(customers, { key: "contactPlan", direction: "desc" }).map(
      (r) => r.id,
    ),
  ).toEqual(["1", "4", "2", "0", "3"]);
});
