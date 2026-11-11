import type { InsuranceCustomer } from "./api";

export type SortKey = "assets" | "contactPlan";
export type ListSort = { key: SortKey; direction: "asc" | "desc" } | null;

function assetAmount(value?: string | number): number | undefined {
  if (value == null || (typeof value === "string" && !value.trim()))
    return undefined;
  const amount = Number(value);
  return Number.isFinite(amount) ? amount : undefined;
}

function sortValue(customer: InsuranceCustomer, key: SortKey) {
  if (key === "assets") return assetAmount(customer.assets);
  // 与单元格默认展示一致：接口及本地保存均按日期倒序排列。
  const date = customer.contactPlans?.[0]?.date;
  const time = date ? Date.parse(date) : NaN;
  return Number.isFinite(time) ? time : undefined;
}

export function orderCustomers(customers: InsuranceCustomer[], sort: ListSort) {
  if (!sort) return customers;
  return [...customers].sort((a, b) => {
    const left = sortValue(a, sort.key);
    const right = sortValue(b, sort.key);
    if (left === undefined) return right === undefined ? 0 : 1;
    if (right === undefined) return -1;
    return (left - right) * (sort.direction === "asc" ? 1 : -1);
  });
}
