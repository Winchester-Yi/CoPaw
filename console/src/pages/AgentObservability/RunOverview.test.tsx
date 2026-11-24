import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import RunOverview from "./RunOverview";

const request = vi.hoisted(() => vi.fn());
vi.mock("../../api/request", () => ({ request }));
afterEach(() => {
  cleanup();
  request.mockReset();
});
it("hides the old scope immediately and ignores late responses", async () => {
  let resolveOld: (value: typeof business) => void = () => {};
  let resolveSecond: (value: typeof business) => void = () => {};
  request.mockImplementation((url: string) => {
    if (!url.includes("business-population"))
      return Promise.resolve(performance);
    return new Promise((resolve) => {
      if (url.includes("2026-01-01")) resolveOld = resolve;
      else if (url.includes("2026-01-02")) resolveSecond = resolve;
    });
  });
  const { rerender } = render(
    <RunOverview
      source="a"
      start="2026-01-01T00:00:00Z"
      end="2026-01-01T01:00:00Z"
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "展开总览" }));
  await act(async () => resolveOld(business));
  expect(screen.getByText("样本量：7")).toBeInTheDocument();
  rerender(
    <RunOverview
      source="b"
      start="2026-01-02T00:00:00Z"
      end="2026-01-02T01:00:00Z"
    />,
  );
  expect(screen.queryByText("样本量：7")).not.toBeInTheDocument();
  expect(screen.getByText("正在加载业务总体…")).toBeInTheDocument();
  rerender(
    <RunOverview
      source="c"
      start="2026-01-03T00:00:00Z"
      end="2026-01-03T01:00:00Z"
    />,
  );
  // The previous request must not replace the loading state of the latest scope.
  await act(async () => resolveSecond(business));
  expect(screen.queryByText("样本量：7")).not.toBeInTheDocument();
});
const business = {
  population_source: {
    business: "turns",
    runs: "runs",
    subagents: "subagents",
  },
  business_as_of: "2026-01-01T01:00:00Z",
  observability_as_of: null,
  subagents_as_of: null,
  business: { turn_count: 7 },
  runs: { status: "unavailable", settled_count: null },
  subagents: { status: "unavailable", count: null },
};
const performance = {
  population_source: "observed_ended_spans",
  generated_at: "2026-01-01T01:00:00Z",
  sample_count: 0,
  run_count: 0,
  duration_us: { known_count: 0, p50: null, p95: null, p99: null },
  tool_failures: { count: 0, sample_count: 0, rate: null },
};
it("uses the same current source and range, and changes range without retaining old data", async () => {
  request.mockImplementation((url: string) =>
    Promise.resolve(
      url.includes("business-population") ? business : performance,
    ),
  );
  const { rerender } = render(
    <RunOverview
      source="source-a"
      start="2026-01-01T00:00:00Z"
      end="2026-01-01T01:00:00Z"
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "展开总览" }));
  expect(await screen.findByText("样本量：7")).toBeInTheDocument();
  expect(
    screen.getByText("当前时间范围内没有观测性能样本"),
  ).toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(2);
  for (const [url, options] of request.mock.calls) {
    expect(new URLSearchParams(url.split("?")[1]).get("start")).toBe(
      "2026-01-01T00:00:00Z",
    );
    expect(new URLSearchParams(url.split("?")[1]).get("end")).toBe(
      "2026-01-01T01:00:00Z",
    );
    expect(options.headers["X-Source-Id"]).toBe("source-a");
  }
  rerender(
    <RunOverview
      source="source-b"
      start="2026-01-02T00:00:00Z"
      end="2026-01-02T01:00:00Z"
    />,
  );
  await waitFor(() => expect(request).toHaveBeenCalledTimes(4));
  expect(request.mock.calls[2][0]).toContain("2026-01-02");
  expect(request.mock.calls[2][1].headers["X-Source-Id"]).toBe("source-b");
});
it("keeps performance visible when business is unavailable", async () => {
  request.mockImplementation((url: string) =>
    url.includes("business-population")
      ? Promise.reject(new Error("offline"))
      : Promise.resolve(performance),
  );
  render(
    <RunOverview
      source="s"
      start="2026-01-01T00:00:00Z"
      end="2026-01-01T01:00:00Z"
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "展开总览" }));
  expect(await screen.findByText("业务总体暂不可用")).toBeInTheDocument();
  expect(
    await screen.findByText("当前时间范围内没有观测性能样本"),
  ).toBeInTheDocument();
});
it("keeps business visible when performance fails", async () => {
  request.mockImplementation((url: string) =>
    url.includes("business-population")
      ? Promise.resolve(business)
      : Promise.reject(new Error("offline")),
  );
  render(
    <RunOverview
      source="s"
      start="2026-01-01T00:00:00Z"
      end="2026-01-01T01:00:00Z"
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "展开总览" }));
  expect(await screen.findByText("样本量：7")).toBeInTheDocument();
  expect(await screen.findByText("观测性能暂不可用")).toBeInTheDocument();
});
