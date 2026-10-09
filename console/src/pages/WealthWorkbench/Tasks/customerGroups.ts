import type { Customer, CustomerField } from "../types";

interface CustomerGroup {
  key: string;
  fields: CustomerField[];
  customers: Customer[];
}

/** 使用结构化组合键，避免字段值包含分隔符时误合并。保留接口首次出现顺序。 */
export function buildCustomerGroups(
  customers: Customer[],
  business: boolean,
): CustomerGroup[] {
  const groups = new Map<string, CustomerGroup>();
  for (const customer of customers) {
    const fields = business ? customer.groupFields ?? [] : [];
    const key = JSON.stringify(
      fields.map((field) => [field.name, field.value]),
    );
    let group = groups.get(key);
    if (!group) {
      group = { key, fields, customers: [] };
      groups.set(key, group);
    }
    group.customers.push(customer);
  }
  return [...groups.values()];
}
