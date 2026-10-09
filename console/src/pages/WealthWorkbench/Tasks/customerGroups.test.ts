import { describe, expect, it } from "vitest";
import type { Customer } from "../types";
import { buildCustomerGroups } from "./customerGroups";

const customer = (id: string, age: string, risk: string): Customer => ({
  id,
  custUid: id,
  skillId: "skill",
  name: id,
  label: "",
  reason: "",
  category: "基金",
  task: "经营",
  done: false,
  channel: "",
  time: "",
  note: "",
  groupFields: [
    { name: "age", label: "年龄", value: age },
    { name: "risk", label: "风险等级", value: risk },
  ],
});

describe("经营分组", () => {
  it("按组合归类，同龄不同风险不合并，保持客户顺序", () => {
    const rows = [
      customer("1", "48", "稳健"),
      customer("2", "48", "进取"),
      customer("3", "48", "稳健"),
    ];
    const groups = buildCustomerGroups(rows, true);
    expect(groups.map((g) => g.customers.map((c) => c.id))).toEqual([
      ["1", "3"],
      ["2"],
    ]);
  });
  it("客户视角和空配置不分组", () => {
    const rows = [customer("1", "48", "稳健"), customer("2", "50", "进取")];
    expect(buildCustomerGroups(rows, false)).toHaveLength(1);
    expect(
      buildCustomerGroups(
        rows.map((c) => ({ ...c, groupFields: [] })),
        true,
      )[0].fields,
    ).toEqual([]);
  });
  it("字段包含分隔符时仍区分组合", () => {
    expect(
      buildCustomerGroups(
        [customer("1", "a|b", "c"), customer("2", "a", "b|c")],
        true,
      ),
    ).toHaveLength(2);
  });
});
