import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { CustomerMetrics } from "./CustomerMetrics";

afterEach(cleanup);

describe("客户指标", () => {
  it("默认六项，展开完整指标，收起后恢复；每项均有名称和值", () => {
    const fields = Array.from({ length: 8 }, (_, i) => ({
      name: `field-${i}`,
      label: `指标${i}`,
      value: String(i),
    }));
    const { container } = render(<CustomerMetrics fields={fields} />);
    expect(container.querySelectorAll("dt")).toHaveLength(6);
    expect(container.querySelector("dt")).toHaveTextContent("指标0：");
    expect(container.querySelector("dd")).toHaveTextContent("0");
    fireEvent.click(screen.getByRole("button", { name: "展开全部（8项）" }));
    expect(container.querySelectorAll("dt")).toHaveLength(8);
    expect(screen.getByRole("button", { name: "收起" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(container.querySelectorAll("dt")).toHaveLength(6);
  });

  it("键盘聚焦可查看完整长名称和值", async () => {
    const { container } = render(
      <CustomerMetrics
        fields={[
          {
            name: "aum",
            label: "非常长的日均资产字段名称",
            value: "完整值123456789",
          },
        ]}
      />,
    );
    fireEvent.focus(container.querySelector('[tabindex="0"]')!);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "非常长的日均资产字段名称：完整值123456789",
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("空指标不渲染无关字段", () => {
    render(<CustomerMetrics />);
    expect(screen.getByText("暂无指标")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
