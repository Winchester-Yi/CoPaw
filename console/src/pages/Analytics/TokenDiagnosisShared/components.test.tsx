import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FilterBar } from "./FilterBar";
import { EvidenceState } from "./EvidenceState";
afterEach(cleanup);
describe("common diagnosis controls", () => {
  it("has visible filter labels, keeps returned UTC window, and clears cursor on apply", () => {
    const onChange = vi.fn();
    render(
      <FilterBar
        filters={{ model_name: "old", cursor: "page" }}
        window={{
          start: "2026-10-11T00:00:00Z",
          end: "2026-10-12T00:00:00Z",
          semantics: "UTC half-open [start,end)",
        }}
        onChange={onChange}
      />,
    );
    fireEvent.change(screen.getByLabelText("模型"), {
      target: { value: "new" },
    });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(onChange.mock.calls[0][0]).toMatchObject({
      model_name: "new",
      start: "2026-10-11T00:00:00Z",
      end: "2026-10-12T00:00:00Z",
    });
    expect(onChange.mock.calls[0][0].cursor).toBeUndefined();
    expect(screen.getByLabelText("执行 Agent")).toBeInTheDocument();
    expect(screen.getByLabelText("根 Agent 归属")).toBeInTheDocument();
    expect(screen.queryByLabelText("规则")).not.toBeInTheDocument();
    expect(screen.getByText(/UTC 半开区间/)).toBeInTheDocument();
  });
  it("rejects a reversed window and exposes optional rule/state labels", () => {
    const onChange = vi.fn();
    render(
      <FilterBar
        filters={{ start: "2026-10-12T00:00:00Z", end: "2026-10-11T00:00:00Z" }}
        onChange={onChange}
        showRule
        showState
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "开始时间须早于结束时间",
    );
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByLabelText("规则")).toBeInTheDocument();
    expect(screen.getByLabelText("状态")).toBeInTheDocument();
  });
  it("names evidence limits and expiration beyond color", () => {
    render(
      <EvidenceState
        state="partial"
        reason="candidate_limit"
        missingFields={["usage"]}
        limited
        expired
      />,
    );
    expect(screen.getByText("部分采集")).toBeInTheDocument();
    expect(screen.getByText("候选检索超限")).toBeInTheDocument();
    expect(screen.getByText("查询受限")).toBeInTheDocument();
    expect(screen.getByText("证据已过期")).toBeInTheDocument();
    expect(screen.getByText("缺少字段：usage")).toBeInTheDocument();
  });
  it("rejects windows over thirty days", () => {
    const onChange = vi.fn();
    render(
      <FilterBar
        filters={{ start: "2026-09-01T00:00:00Z", end: "2026-10-12T00:00:00Z" }}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(screen.getByRole("alert")).toHaveTextContent("时间范围最多 30 天");
    expect(onChange).not.toHaveBeenCalled();
  });
  it("model-only apply preserves exact returned microsecond window after paging", () => {
    const onChange = vi.fn();
    const window = {
      start: "2026-10-11T10:24:59.123456Z",
      end: "2026-10-12T10:24:59.654321Z",
      semantics: "UTC half-open [start,end)",
    };
    const { rerender } = render(
      <FilterBar
        filters={{ model_name: "old" }}
        window={window}
        onChange={onChange}
      />,
    );
    rerender(
      <FilterBar
        filters={{ model_name: "old", cursor: "next-page" }}
        window={window}
        onChange={onChange}
      />,
    );
    fireEvent.change(screen.getByLabelText("模型"), {
      target: { value: "new" },
    });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(onChange.mock.calls[0][0]).toMatchObject({
      start: window.start,
      end: window.end,
      model_name: "new",
    });
    expect(onChange.mock.calls[0][0].cursor).toBeUndefined();
  });
  it("editing one boundary preserves the exact untouched boundary", () => {
    const onChange = vi.fn();
    render(
      <FilterBar
        filters={{
          start: "2026-10-11T10:24:59.123456Z",
          end: "2026-10-12T10:24:59.654321Z",
        }}
        onChange={onChange}
      />,
    );
    fireEvent.change(screen.getByLabelText("开始时间（UTC）"), {
      target: { value: "2026-10-11T10:25" },
    });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(onChange.mock.calls[0][0]).toMatchObject({
      start: "2026-10-11T10:25:00.000Z",
      end: "2026-10-12T10:24:59.654321Z",
    });
  });
  it("reset preserves the exact window while clearing other filters and paging", () => {
    const onChange = vi.fn();
    const window = {
      start: "2026-10-11T10:24:59.123456Z",
      end: "2026-10-12T10:24:59.654321Z",
      semantics: "UTC half-open [start,end)",
    };
    render(
      <FilterBar
        filters={{ model_name: "old", cursor: "next-page", limit: 25 }}
        window={window}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "重置筛选" }));
    expect(onChange).toHaveBeenCalledWith({
      start: window.start,
      end: window.end,
      limit: 25,
    });
  });
});
