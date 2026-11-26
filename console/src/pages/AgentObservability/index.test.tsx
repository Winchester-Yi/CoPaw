import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import Page from ".";
import { useAgentStore } from "../../stores/agentStore";
const state = vi.hoisted(() => ({
  source: "a",
  userId: "u",
  manager: true,
  isSuperManager: false,
}));
const request = vi.hoisted(() => vi.fn());
vi.mock("../../stores/iframeStore", () => ({
  useIframeStore: (select: (s: typeof state) => unknown) => select(state),
}));
vi.mock("../../api/request", () => ({ request }));
const run = {
  run_id: "abc",
  agent_key: "agent",
  started_at: null,
  status: "succeeded",
  duration_us: 0,
  llm_calls: 0,
  tool_calls: null,
  input_tokens: 0,
  output_tokens: 0,
  projected_at: "2025-12-01T00:00:00Z",
};
function mount(path = "/agent-observability/runs?hideMenu=true") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/agent-observability/runs" element={<Page />} />
        <Route path="/agent-observability/runs/:runId" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
}
beforeEach(() => {
  request.mockReset();
  state.source = "a";
  useAgentStore.getState().setSelectedAgent("default");
});
afterEach(cleanup);

it("shows known partial model usage and leaves an unknown Run total empty", async () => {
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : {
            run: {
              ...run,
              llm_calls: 1,
              request_attempt_count: 2,
              input_tokens_known_requests: 1,
              output_tokens_known_requests: 0,
              input_tokens: 20,
              output_tokens: null,
            },
            attempts: [],
          },
    ),
  );
  mount("/agent-observability/runs/abc");
  const summary = await screen.findByRole("region", { name: "Run 摘要" });
  expect(
    within(summary).getByText("模型请求次数").nextSibling,
  ).toHaveTextContent("2");
  expect(
    within(summary).getByText("输入 Tokens").nextSibling,
  ).toHaveTextContent("已观测 20");
  expect(
    within(summary).getByText("输出 Tokens").nextElementSibling,
  ).toBeEmptyDOMElement();
});

it("shows the Run data quality independently of its status", async () => {
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : { items: [{ ...run, quality: "partial" }], next_cursor: null },
    ),
  );
  mount();
  expect(await screen.findByText("partial")).toBeInTheDocument();
  expect(
    screen.getByRole("columnheader", { name: "数据质量" }),
  ).toBeInTheDocument();
  expect(screen.getByText("succeeded")).toBeInTheDocument();
});

it("clears an invalid deep link when the Run has no Attempts", async () => {
  request.mockImplementation((path: string) =>
    path.includes("attempt_id=")
      ? Promise.reject(Object.assign(new Error(), { status: 404 }))
      : Promise.resolve(
          path.includes("capabilities")
            ? { capture_enabled: true }
            : { run, attempts: [] },
        ),
  );
  mount("/agent-observability/runs/abc?attempt=missing&span=missing");
  expect(
    await screen.findByText("目标 Attempt 或 Span 已失效"),
  ).toBeInTheDocument();
  expect(screen.getByText("尚无已观测的执行片段")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "清除失效定位" }));
  expect(
    screen.queryByText("目标 Attempt 或 Span 已失效"),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /刷\s*新/ })).toBeEnabled();
});

it("offers retry for a span-only link when no Attempts are available", async () => {
  let attempts = 0;
  request.mockImplementation((path: string) => {
    if (path.includes("/ancestors")) {
      attempts += 1;
      return Promise.reject(
        Object.assign(new Error(), { status: attempts === 1 ? 500 : 404 }),
      );
    }
    return Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : { run, attempts: [] },
    );
  });
  mount("/agent-observability/runs/abc?span=missing");
  expect(await screen.findByText("定位暂不可用")).toBeInTheDocument();
  expect(screen.getByText("尚无已观测的执行片段")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试定位" }));
  expect(
    await screen.findByText("目标 Attempt 或 Span 已失效"),
  ).toBeInTheDocument();
  expect(attempts).toBe(2);
});

it("shows a real parent cycle and opens its linked target on a narrow view", async () => {
  const originalObserver = globalThis.ResizeObserver;
  class NarrowObserver {
    constructor(private callback: ResizeObserverCallback) {}
    observe() {
      this.callback(
        [{ contentRect: { width: 700 } } as ResizeObserverEntry],
        this as unknown as ResizeObserver,
      );
    }
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", NarrowObserver);
  try {
    const attempt = {
      attempt_id: "a1",
      attempt_index: 1,
      status: "succeeded",
      started_at: null,
      ended_at: null,
      duration_us: null,
      quality: "complete",
    };
    const spans = ["s1", "s2"].map((id, index) => ({
      span_id: id,
      attempt_id: "a1",
      parent_span_id: index ? "s1" : "s2",
      name: `cycle ${id}`,
      span_type: "tool",
      operation: "execute",
      started_at: null,
      ended_at: null,
      status: "succeeded",
      quality: "complete",
      duration_us: null,
    }));
    request.mockImplementation((path: string) =>
      Promise.resolve(
        path.includes("capabilities")
          ? { capture_enabled: true }
          : path.includes("/ancestors")
          ? { items: spans, quality: "cycle", next_cursor: null }
          : path.includes("attempt_id=")
          ? { spans, spans_next_cursor: null }
          : { run, attempts: [attempt] },
      ),
    );
    mount("/agent-observability/runs/abc?attempt=a1&span=s2&hideMenu=true");
    expect(await screen.findByText("父链存在循环")).toBeInTheDocument();
    expect(screen.getAllByRole("treeitem")).toHaveLength(2);
    expect(
      await screen.findByRole("complementary", { name: "节点详情" }),
    ).toHaveTextContent("cycle s2");
    fireEvent.click(screen.getByRole("button", { name: "收起 cycle s1" }));
    expect(
      screen.queryByRole("treeitem", { name: /cycle s2/ }),
    ).not.toBeInTheDocument();
  } finally {
    vi.stubGlobal("ResizeObserver", originalObserver);
  }
});

it.each(["link", "search", "errors"])(
  "shows inline details after %s location",
  async (mode) => {
    const originalObserver = globalThis.ResizeObserver;
    class NarrowObserver {
      constructor(private callback: ResizeObserverCallback) {}
      observe() {
        this.callback(
          [{ contentRect: { width: 700 } } as ResizeObserverEntry],
          this as unknown as ResizeObserver,
        );
      }
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", NarrowObserver);
    try {
      const attempt = {
        attempt_id: "a1",
        attempt_index: 1,
        status: "succeeded",
        started_at: null,
        ended_at: null,
        duration_us: null,
        quality: "complete",
      };
      const span = {
        span_id: "s1",
        attempt_id: "a1",
        parent_span_id: null,
        name: "target",
        span_type: "tool",
        operation: "execute",
        started_at: null,
        ended_at: null,
        status: "succeeded",
        quality: "complete",
        duration_us: null,
      };
      request.mockImplementation((path: string) =>
        Promise.resolve(
          path.includes("capabilities")
            ? { capture_enabled: true }
            : path.includes("/ancestors")
            ? { items: [span], quality: "complete", next_cursor: null }
            : path.includes("/spans/search")
            ? { items: [span], next_cursor: null }
            : path.includes("attempt_id=")
            ? { spans: [span], spans_next_cursor: null }
            : { run, attempts: [attempt] },
        ),
      );
      mount(
        `/agent-observability/runs/abc?hideMenu=true${
          mode === "link" ? "&attempt=a1&span=s1" : ""
        }`,
      );
      if (mode !== "link") {
        if (mode === "search") {
          fireEvent.change(await screen.findByLabelText("搜索当前 Attempt"), {
            target: { value: "target" },
          });
          fireEvent.click(screen.getByRole("button", { name: /搜\s*索/ }));
        } else {
          fireEvent.click(
            (await screen.findAllByRole("button", { name: "定位异常" }))[0],
          );
        }
        fireEvent.click(
          await screen.findByRole("button", { name: /target · a1/ }),
        );
      }
      expect(
        await screen.findByRole("complementary", { name: "节点详情" }),
      ).toHaveTextContent("target");
      expect(screen.getByRole("treeitem", { name: /target/ })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    } finally {
      vi.stubGlobal("ResizeObserver", originalObserver);
    }
  },
);

it("refreshes loaded pages and keeps the selected attempt and span when a new attempt arrives", async () => {
  const attempts = [1, 2].map((n) => ({
    attempt_id: `a${n}`,
    attempt_index: n,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  }));
  const span = (id: string, time: string) => ({
    span_id: id,
    attempt_id: "a1",
    parent_span_id: null,
    name: id,
    span_type: "tool",
    operation: "execute",
    started_at: time,
    ended_at: time,
    status: "succeeded",
    quality: "complete",
    duration_us: 0,
  });
  const first = span("first", "2026-01-01T00:00:01Z");
  const second = span("second", "2026-01-01T00:00:02Z");
  const late = span("late", "2026-01-01T00:00:00Z");
  let refreshing = false;
  request.mockImplementation((path: string) => {
    if (path.includes("capabilities"))
      return Promise.resolve({ capture_enabled: true });
    if (!path.includes("attempt_id="))
      return Promise.resolve({ run, attempts: [attempts[0]] });
    const next = path.includes("span_cursor=");
    return Promise.resolve({
      run,
      attempts: refreshing ? attempts : [attempts[0]],
      spans: next ? [second] : refreshing ? [late, first] : [first],
      spans_next_cursor: next ? null : "next",
      generated_at: "2026-01-01T00:00:10Z",
    });
  });
  await act(async () => {
    mount("/agent-observability/runs/abc");
  });
  const loadMore = screen.getByRole("button", { name: /加载更多/ });
  await waitFor(() => expect(loadMore).not.toHaveClass("ant-btn-loading"), {
    timeout: 3000,
  });
  fireEvent.click(loadMore);
  fireEvent.click(await screen.findByRole("treeitem", { name: /second/ }));
  refreshing = true;
  fireEvent.click(screen.getByRole("button", { name: /刷\s*新/ }));
  expect(await screen.findByText("发现新的 Attempt 2")).toBeInTheDocument();
  expect(screen.getByRole("treeitem", { name: /second/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const rows = screen.getAllByRole("treeitem");
  expect(rows.map((row) => row.dataset.spanId)).toEqual([
    "late",
    "first",
    "second",
  ]);
  expect(screen.getByText(/投影时间：/)).toHaveTextContent(
    new Date(run.projected_at).toLocaleString(),
  );
  fireEvent.click(screen.getByRole("button", { name: "切换查看" }));
  expect(screen.getByRole("button", { name: /Attempt 2/ })).toHaveClass(
    "ant-btn-primary",
  );
});

it("polls an active run and stops when the refreshed run is terminal", async () => {
  const interval = vi
    .spyOn(window, "setInterval")
    .mockReturnValue(42 as unknown as ReturnType<typeof window.setInterval>);
  const clear = vi.spyOn(window, "clearInterval");
  const activeRun = { ...run, status: "running" };
  const attempt = {
    attempt_id: "a1",
    attempt_index: 1,
    status: "running",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  };
  let terminal = false;
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : {
            run: terminal ? run : activeRun,
            attempts: [attempt],
            spans: [],
            spans_next_cursor: null,
            generated_at: "2026-01-01T00:00:00Z",
          },
    ),
  );
  mount("/agent-observability/runs/abc");
  await screen.findByRole("button", { name: /Attempt 1/ });
  await waitFor(() =>
    expect(
      request.mock.calls.filter(([p]) => (p as string).includes("attempt_id="))
        .length,
    ).toBe(1),
  );
  terminal = true;
  await act(async () => {
    (interval.mock.calls.find((call) => call[1] === 5000)![0] as () => void)();
  });
  expect(
    request.mock.calls.filter(([p]) => (p as string).includes("attempt_id="))
      .length,
  ).toBe(2);
  expect(clear).toHaveBeenCalledWith(42);
  interval.mockRestore();
  clear.mockRestore();
});
it("loads span evidence only on the detail tab and switches spans", async () => {
  const attempt = {
    attempt_id: "a1",
    attempt_index: 1,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  };
  const span = (id: string) => ({
    span_id: id,
    attempt_id: "a1",
    parent_span_id: null,
    name: id,
    span_type: "tool",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "succeeded",
    quality: "complete",
    duration_us: null,
  });
  request.mockImplementation((p: string) =>
    Promise.resolve(
      p.includes("capabilities")
        ? { capture_enabled: true }
        : p.includes("/evidence")
        ? {
            items: [
              {
                evidence_ref: "e",
                status: "available",
                message: p.includes("/s1/") ? "first" : "second",
              },
            ],
          }
        : p.includes("attempt_id=")
        ? { spans: [span("s1"), span("s2")], spans_next_cursor: null }
        : { run, attempts: [attempt] },
    ),
  );
  mount("/agent-observability/runs/abc?hideMenu=true");
  fireEvent.click(await screen.findByText("s1"));
  expect(request.mock.calls.some(([p]) => p.includes("/evidence"))).toBe(false);
  fireEvent.click(screen.getByRole("tab", { name: "正文证据" }));
  expect(await screen.findByText('"first"')).toBeInTheDocument();
  fireEvent.click(screen.getByText("s2"));
  expect(screen.queryByText('"first"')).not.toBeInTheDocument();
  expect(await screen.findByText('"second"')).toBeInTheDocument();
});
it("shows history with capture off and opens the embedded detail", async () => {
  request.mockImplementation((p: string) =>
    Promise.resolve(
      p.includes("capabilities")
        ? { capture_enabled: false }
        : p.includes("/runs/")
        ? { run }
        : { items: [run], next_cursor: null },
    ),
  );
  mount();
  expect(await screen.findByText(/采集已关闭/)).toBeInTheDocument();
  expect(await screen.findByText("0 ms")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: "abc" }));
  expect(await screen.findByText("Run 摘要")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "返回执行记录" })).toHaveAttribute(
    "href",
    "/agent-observability/runs?hideMenu=true",
  );
});
it("retries storage failures and displays an empty list", async () => {
  let failed = true;
  request.mockImplementation((p: string) =>
    p.includes("capabilities")
      ? Promise.resolve({ capture_enabled: true })
      : failed
      ? Promise.reject(new Error())
      : Promise.resolve({ items: [], next_cursor: null }),
  );
  mount();
  const button = await screen.findByRole("button", { name: /重\s*试/ });
  failed = false;
  fireEvent.click(button);
  expect(
    await screen.findByText("当前时间范围内暂无执行记录"),
  ).toBeInTheDocument();
});
it("shows a recoverable missing state for an inaccessible Run", async () => {
  request.mockImplementation((p: string) =>
    p.includes("capabilities")
      ? Promise.resolve({ capture_enabled: true })
      : Promise.reject(Object.assign(new Error(), { status: 404 })),
  );
  mount("/agent-observability/runs/abc");
  expect(await screen.findByText("未找到此 Run")).toBeInTheDocument();
});
it("discards a response from the previous source", async () => {
  let resolveOld!: (value: unknown) => void;
  request.mockImplementation((p: string) => {
    if (p.includes("capabilities"))
      return Promise.resolve({ capture_enabled: true });
    if (state.source === "a")
      return new Promise((resolve) => {
        resolveOld = resolve;
      });
    return Promise.resolve({ items: [], next_cursor: null });
  });
  const view = mount();
  state.source = "b";
  view.rerender(
    <MemoryRouter initialEntries={["/agent-observability/runs"]}>
      <Routes>
        <Route path="/agent-observability/runs" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
  resolveOld({ items: [run], next_cursor: null });
  expect(
    await screen.findByText("当前时间范围内暂无执行记录"),
  ).toBeInTheDocument();
  expect(screen.queryByText("abc")).not.toBeInTheDocument();
});
it.each([
  [400, "请求参数无效"],
  [403, "无权访问可观测数据"],
])("reports HTTP %s distinctly", async (status, message) => {
  request.mockImplementation((p: string) =>
    p.includes("capabilities")
      ? Promise.resolve({ capture_enabled: true })
      : Promise.reject(Object.assign(new Error(), { status })),
  );
  mount("/agent-observability/runs/abc");
  expect(await screen.findByText(message)).toBeInTheDocument();
  expect(screen.queryByText("未找到此 Run")).not.toBeInTheDocument();
  expect(screen.getByText("当前 Source：a")).toBeInTheDocument();
});

it("queries historical exact IDs without an implicit time window and restores the cursor", async () => {
  request.mockImplementation((p: string) =>
    Promise.resolve(
      p.includes("capabilities")
        ? { capture_enabled: true }
        : p.includes("/runs/")
        ? { run }
        : {
            items: [run],
            next_cursor: p.includes("cursor=") ? null : "next-token",
          },
    ),
  );
  mount();
  await screen.findByText("abc");
  fireEvent.change(screen.getByLabelText("完整 ID"), {
    target: { value: "historic-id" },
  });
  fireEvent.click(screen.getByRole("button", { name: /查\s*询/ }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining("run_id=historic-id"),
    ),
  );
  const query = request.mock.calls
    .map(([p]) => p as string)
    .find((p) => p.includes("run_id=historic-id"))!;
  expect(query).not.toContain("start=");
  fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining("cursor=next-token"),
    ),
  );
  fireEvent.click(await screen.findByRole("link", { name: "abc" }));
  expect(
    await screen.findByRole("link", { name: "返回执行记录" }),
  ).toHaveAttribute("href", expect.stringContaining("cursor=next-token"));
});

it("applies combined filters and offers recovery when the candidate limit is exceeded", async () => {
  request.mockImplementation((p: string) =>
    p.includes("capabilities")
      ? Promise.resolve({ capture_enabled: true })
      : p.includes("agent_id=")
      ? Promise.reject(
          Object.assign(new Error("range_too_large"), { status: 422 }),
        )
      : Promise.resolve({ items: [], next_cursor: null }),
  );
  mount(
    "/agent-observability/runs?agent_id=worker&status=failed&min_duration_ms=20&sort_by=duration_us&start=2026-01-01T00%3A00&end=2026-01-02T00%3A00",
  );
  expect(await screen.findByText(/候选记录过多/)).toBeInTheDocument();
  const query = request.mock.calls
    .map(([p]) => p as string)
    .find((p) => p.includes("agent_id="))!;
  expect(query).toContain("status=failed");
  expect(query).toContain("min_duration_ms=20");
  expect(query).toContain("sort_by=duration_us");
  expect(query).toContain("start=");
  fireEvent.click(screen.getByRole("button", { name: "清除筛选" }));
  expect(
    await screen.findByText(/当前时间范围内暂无执行记录/),
  ).toBeInTheDocument();
});

it.each(["attempt_id", "session_id"])(
  "searches historical %s exactly",
  async (idType) => {
    request.mockImplementation((p: string) =>
      Promise.resolve(
        p.includes("capabilities")
          ? { capture_enabled: true }
          : { items: [run], next_cursor: null },
      ),
    );
    mount(`/agent-observability/runs?idType=${idType}&id=historic`);
    await screen.findByText("abc");
    const query = request.mock.calls
      .map(([p]) => p as string)
      .find((p) => p.includes(`${idType}=historic`))!;
    expect(query).toBeTruthy();
    expect(query).not.toContain("start=");
  },
);

it("restores the content scroll and row focus and keeps the initial window when paging back", async () => {
  request.mockImplementation((p: string) =>
    Promise.resolve(
      p.includes("capabilities")
        ? { capture_enabled: true }
        : p.includes("/runs/")
        ? { run }
        : {
            items: [run],
            next_cursor: p.includes("cursor=") ? null : "next",
            window_start: "2026-01-01T00:00:00.123456Z",
            window_end: "2026-01-02T00:00:00.654321Z",
          },
    ),
  );
  render(
    <MemoryRouter initialEntries={["/agent-observability/runs"]}>
      <div className="page-content">
        <Routes>
          <Route path="/agent-observability/runs" element={<Page />} />
          <Route path="/agent-observability/runs/:runId" element={<Page />} />
        </Routes>
      </div>
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining("cursor=next"),
    ),
  );
  const nextQuery = request.mock.calls
    .map(([p]) => p as string)
    .find((p) => p.includes("cursor=next"))!;
  expect(new URLSearchParams(nextQuery.split("?")[1]).get("start")).toBe(
    "2026-01-01T00:00:00.123456Z",
  );
  expect(new URLSearchParams(nextQuery.split("?")[1]).get("end")).toBe(
    "2026-01-02T00:00:00.654321Z",
  );
  fireEvent.click(await screen.findByRole("button", { name: "上一页" }));
  await waitFor(() =>
    expect(
      request.mock.calls.some(
        ([p]) =>
          (p as string).includes("start=2026-01-01") &&
          !(p as string).includes("cursor="),
      ),
    ).toBe(true),
  );
  const container = document.querySelector(".page-content")!;
  container.scrollTop = 350;
  fireEvent.click(await screen.findByRole("link", { name: "abc" }));
  fireEvent.click(await screen.findByRole("link", { name: "返回执行记录" }));
  await waitFor(() =>
    expect(screen.getByRole("link", { name: "abc" })).toHaveFocus(),
  );
  expect(container.scrollTop).toBe(350);
});

it("clears an old cursor when the authorization context changes", async () => {
  request.mockImplementation((p: string) =>
    Promise.resolve(
      p.includes("capabilities")
        ? { capture_enabled: true }
        : { items: [], next_cursor: null },
    ),
  );
  const view = mount("/agent-observability/runs?cursor=old&previous=");
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(expect.stringContaining("cursor=old")),
  );
  request.mockClear();
  state.source = "b";
  view.rerender(
    <MemoryRouter
      initialEntries={["/agent-observability/runs?cursor=old&previous="]}
    >
      <Routes>
        <Route path="/agent-observability/runs" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(
      request.mock.calls.some(([p]) => (p as string).includes("/runs?")),
    ).toBe(true),
  );
  expect(
    request.mock.calls.some(([p]) => (p as string).includes("cursor=old")),
  ).toBe(false);
});

it("offers recovery for invalid dates in a shared URL", async () => {
  request.mockResolvedValue({ capture_enabled: true });
  mount("/agent-observability/runs?start=invalid");
  expect(await screen.findByText("请求参数无效")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "清除筛选" })).toBeInTheDocument();
});

it("uses the list window for overview requests after a time change and isolates failures", async () => {
  request.mockImplementation((path: string) => {
    if (path.includes("capabilities"))
      return Promise.resolve({ capture_enabled: true });
    if (path.includes("business-population"))
      return Promise.reject(new Error("unavailable"));
    if (path.includes("performance"))
      return Promise.resolve({
        population_source: "observed_ended_spans",
        generated_at: "2026-01-02T00:00:00Z",
        sample_count: 0,
        run_count: 0,
        duration_us: { known_count: 0, p50: null, p95: null, p99: null },
        tool_failures: { count: 0, sample_count: 0, rate: null },
      });
    const query = new URLSearchParams(path.split("?")[1]);
    return Promise.resolve({
      items: [run],
      next_cursor: null,
      window_start: query.get("start") || "2026-01-01T00:00:00Z",
      window_end: query.get("end") || "2026-01-02T00:00:00Z",
    });
  });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "展开总览" }));
  expect(await screen.findByText("业务总体暂不可用")).toBeInTheDocument();
  expect(
    await screen.findByText("当前时间范围内没有观测性能样本"),
  ).toBeInTheDocument();
  const overviewCalls = () =>
    request.mock.calls.filter(([path]) =>
      /business-population|performance/.test(path as string),
    );
  expect(overviewCalls()).toHaveLength(2);
  for (const [path, options] of overviewCalls()) {
    const query = new URLSearchParams((path as string).split("?")[1]);
    expect(query.get("start")).toBe("2026-01-01T00:00:00Z");
    expect(query.get("end")).toBe("2026-01-02T00:00:00Z");
    expect(options.headers["X-Source-Id"]).toBe("a");
  }
  fireEvent.change(screen.getByLabelText("开始时间"), {
    target: { value: "2026-02-01T00:00" },
  });
  fireEvent.change(screen.getByLabelText("结束时间"), {
    target: { value: "2026-02-02T00:00" },
  });
  fireEvent.click(screen.getByRole("button", { name: /查\s*询/ }));
  fireEvent.click(await screen.findByRole("button", { name: "展开总览" }));
  await waitFor(() => expect(overviewCalls()).toHaveLength(4));
  const listCalls = request.mock.calls
    .map(([path]) => path as string)
    .filter((path) => path.includes("/runs?"));
  const listCall = listCalls[listCalls.length - 1];
  const listQuery = new URLSearchParams(listCall.split("?")[1]);
  for (const [path] of overviewCalls().slice(2)) {
    const query = new URLSearchParams((path as string).split("?")[1]);
    expect(query.get("start")).toBe(listQuery.get("start"));
    expect(query.get("end")).toBe(listQuery.get("end"));
  }
});

it("locates an exception in an earlier Attempt with unloaded ancestors", async () => {
  const attempts = [1, 2].map((n) => ({
    attempt_id: `a${n}`,
    attempt_index: n,
    status: n === 1 ? "failed" : "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  }));
  const span = (id: string, parent: string | null) => ({
    span_id: id,
    attempt_id: "a1",
    parent_span_id: parent,
    name: id,
    span_type: "tool",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "failed",
    quality: "complete",
    duration_us: null,
  });
  const target = span("failure", "parent");
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : path.includes("/spans/search")
        ? {
            items: [target],
            next_cursor: null,
            generated_at: "2026-01-01T00:00:00Z",
          }
        : path.includes("/ancestors")
        ? {
            items: [target, span("parent", null)],
            quality: "complete",
            next_cursor: null,
          }
        : path.includes("attempt_id=")
        ? { spans: [], spans_next_cursor: "next" }
        : { run, attempts },
    ),
  );
  mount("/agent-observability/runs/abc?hideMenu=true");
  await screen.findByRole("button", { name: /Attempt 2/ });
  fireEvent.click(screen.getAllByRole("button", { name: "定位异常" })[0]);
  fireEvent.click(await screen.findByRole("button", { name: /failure · a1/ }));
  expect(
    await screen.findByRole("treeitem", { name: /failure/ }),
  ).toHaveAttribute("aria-selected", "true");
  expect(
    await screen.findByRole("treeitem", { name: /parent/ }),
  ).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByRole("treeitem", { name: /failure/ })).toHaveAttribute(
      "aria-selected",
      "true",
    ),
  );
  const query = request.mock.calls
    .map(([p]) => p as string)
    .find((p) => p.includes("/spans/search"))!;
  expect(query).toContain("errors_only=true");
  expect(query).not.toContain("attempt_id=");
});

it("keeps the Run summary and offers recovery for an invalid shared span", async () => {
  const attempt = {
    attempt_id: "a1",
    attempt_index: 1,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  };
  request.mockImplementation((path: string) =>
    path.includes("/ancestors")
      ? Promise.reject(Object.assign(new Error(), { status: 404 }))
      : Promise.resolve(
          path.includes("capabilities")
            ? { capture_enabled: true }
            : path.includes("attempt_id=")
            ? { spans: [], spans_next_cursor: null }
            : { run, attempts: [attempt] },
        ),
  );
  mount("/agent-observability/runs/abc?attempt=a1&span=missing");
  expect(
    await screen.findByText("目标 Attempt 或 Span 已失效"),
  ).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Run 摘要" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "回到最近 Attempt" }));
  expect(
    screen.queryByText("目标 Attempt 或 Span 已失效"),
  ).not.toBeInTheDocument();
});

it("paginates current Attempt search and restores a shared span with a missing parent", async () => {
  const attempt = {
    attempt_id: "a1",
    attempt_index: 1,
    status: "failed",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  };
  const span = {
    span_id: "lost-child",
    attempt_id: "a1",
    parent_span_id: "missing-parent",
    name: "lost child",
    span_type: "tool",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "failed",
    quality: "complete",
    duration_us: null,
  };
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : path.includes("/spans/search")
        ? {
            items: path.includes("cursor=") ? [span] : [],
            next_cursor: path.includes("cursor=") ? null : "search-next",
            generated_at: "2026-01-01T00:00:00Z",
          }
        : path.includes("/ancestors")
        ? { items: [span], quality: "parent_missing", next_cursor: null }
        : path.includes("attempt_id=")
        ? { spans: [], spans_next_cursor: "span-next" }
        : { run, attempts: [attempt] },
    ),
  );
  mount("/agent-observability/runs/abc?attempt=a1&span=lost-child");
  expect(await screen.findByText("父节点缺失")).toBeInTheDocument();
  expect(screen.getByRole("treeitem", { name: /lost child/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  fireEvent.change(screen.getByLabelText("搜索当前 Attempt"), {
    target: { value: "tool" },
  });
  fireEvent.click(screen.getByRole("button", { name: /搜\s*索/ }));
  fireEvent.change(screen.getByLabelText("搜索当前 Attempt"), {
    target: { value: "changed" },
  });
  fireEvent.click(await screen.findByRole("button", { name: "更多结果" }));
  expect(
    await screen.findByRole("button", { name: /lost child · a1/ }),
  ).toBeInTheDocument();
  const calls = request.mock.calls
    .map(([p]) => p as string)
    .filter((p) => p.includes("/spans/search"));
  expect(calls[0]).toContain("attempt_id=a1");
  expect(calls[1]).toContain("cursor=search-next");
  expect(calls[1]).toContain("q=tool");
});

it("distinguishes a temporary ancestor failure and retries the shared location", async () => {
  const attempt = {
    attempt_id: "a1",
    attempt_index: 1,
    status: "failed",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  };
  const span = {
    span_id: "s1",
    attempt_id: "a1",
    parent_span_id: null,
    name: "target",
    span_type: "tool",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "failed",
    quality: "complete",
    duration_us: null,
  };
  let unavailable = true;
  request.mockImplementation((path: string) =>
    path.includes("/ancestors") && unavailable
      ? Promise.reject(Object.assign(new Error(), { status: 503 }))
      : Promise.resolve(
          path.includes("capabilities")
            ? { capture_enabled: true }
            : path.includes("/ancestors")
            ? { items: [span], quality: "complete", next_cursor: null }
            : path.includes("attempt_id=")
            ? { spans: [], spans_next_cursor: null }
            : { run, attempts: [attempt] },
        ),
  );
  mount("/agent-observability/runs/abc?attempt=a1&span=s1");
  expect(await screen.findByText("定位暂不可用")).toBeInTheDocument();
  expect(
    screen.queryByText("目标 Attempt 或 Span 已失效"),
  ).not.toBeInTheDocument();
  unavailable = false;
  fireEvent.click(screen.getByRole("button", { name: "重试定位" }));
  expect(
    await screen.findByRole("treeitem", { name: /target/ }),
  ).toHaveAttribute("aria-selected", "true");
});

it("reports copy failure and offers the clipboard fallback", async () => {
  const attempt = {
    attempt_id: "a1",
    attempt_index: 1,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  };
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : path.includes("attempt_id=")
        ? { spans: [], spans_next_cursor: null }
        : { run, attempts: [attempt] },
    ),
  );
  mount("/agent-observability/runs/abc");
  fireEvent.click(await screen.findByRole("button", { name: "复制分享链接" }));
  expect(
    await screen.findByText("复制失败，请手动复制下方链接"),
  ).toBeInTheDocument();
});

it("ignores an earlier search response after switching Attempt", async () => {
  const attempts = [1, 2].map((n) => ({
    attempt_id: `a${n}`,
    attempt_index: n,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  }));
  let resolveSearch!: (value: unknown) => void;
  request.mockImplementation((path: string) =>
    path.includes("/spans/search")
      ? new Promise((resolve) => {
          resolveSearch = resolve;
        })
      : Promise.resolve(
          path.includes("capabilities")
            ? { capture_enabled: true }
            : path.includes("attempt_id=")
            ? { spans: [], spans_next_cursor: null }
            : { run, attempts },
        ),
  );
  mount("/agent-observability/runs/abc");
  fireEvent.change(await screen.findByLabelText("搜索当前 Attempt"), {
    target: { value: "old" },
  });
  fireEvent.click(screen.getByRole("button", { name: /搜\s*索/ }));
  fireEvent.click(screen.getByRole("button", { name: /Attempt 1/ }));
  resolveSearch({
    items: [{ span_id: "old", attempt_id: "a2", name: "old result" }],
    next_cursor: "old-cursor",
  });
  await waitFor(() =>
    expect(screen.queryByText(/old result/)).not.toBeInTheDocument(),
  );
  expect(
    screen.queryByRole("button", { name: "更多结果" }),
  ).not.toBeInTheDocument();
});

it("restores locations when navigating within the same Run and through history", async () => {
  const attempts = [1, 2].map((n) => ({
    attempt_id: `a${n}`,
    attempt_index: n,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  }));
  const span = (n: number) => ({
    span_id: `s${n}`,
    attempt_id: `a${n}`,
    parent_span_id: null,
    name: `target${n}`,
    span_type: "tool",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "succeeded",
    quality: "complete",
    duration_us: null,
  });
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : path.includes("/ancestors")
        ? {
            items: [span(path.includes("/s1/") ? 1 : 2)],
            quality: "complete",
            next_cursor: null,
          }
        : path.includes("attempt_id=")
        ? { spans: [], spans_next_cursor: null }
        : { run, attempts },
    ),
  );
  function HistoryControls() {
    const navigate = useNavigate();
    return (
      <>
        <button onClick={() => navigate("?attempt=a1&span=s1")}>
          First link
        </button>
        <button onClick={() => navigate("?attempt=a2&span=s2")}>
          Second link
        </button>
        <button onClick={() => navigate(-1)}>Back</button>
        <button onClick={() => navigate(1)}>Forward</button>
      </>
    );
  }
  render(
    <MemoryRouter initialEntries={["/agent-observability/runs/abc"]}>
      <HistoryControls />
      <Routes>
        <Route path="/agent-observability/runs/:runId" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByText("First link"));
  expect(
    await screen.findByRole("treeitem", { name: /target1/ }),
  ).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByText("Second link"));
  expect(
    await screen.findByRole("treeitem", { name: /target2/ }),
  ).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByText("Back"));
  expect(
    await screen.findByRole("treeitem", { name: /target1/ }),
  ).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByText("Forward"));
  expect(
    await screen.findByRole("treeitem", { name: /target2/ }),
  ).toHaveAttribute("aria-selected", "true");
});

it("loads span records on demand, follows a link and clears the old source", async () => {
  const attempt = {
    attempt_id: "a1",
    attempt_index: 1,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  };
  const span = (id: string) => ({
    span_id: id,
    attempt_id: "a1",
    parent_span_id: null,
    name: id,
    span_type: "tool",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "succeeded",
    quality: "complete",
    duration_us: null,
  });
  let resolveOld!: (value: unknown) => void;
  request.mockImplementation((path: string) => {
    if (path.includes("capabilities"))
      return Promise.resolve({ capture_enabled: true });
    if (path.includes("/events")) {
      if (state.source === "b")
        return Promise.resolve({
          items: [{ event_id: "new", event_name: "new source event" }],
          next_cursor: null,
        });
      if (path.includes("view=links"))
        return Promise.resolve({
          items: [
            {
              event_id: "link",
              event_name: "relation",
              metadata: {
                links: [
                  {
                    relation_type: "retry_of",
                    obs_attempt_id: "a1",
                    obs_span_id: "s2",
                  },
                ],
              },
            },
          ],
          next_cursor: null,
        });
      return new Promise((resolve) => {
        resolveOld = resolve;
      });
    }
    if (path.includes("/assets"))
      return Promise.resolve({
        items: [
          { asset_ref: "asset", asset_id: "model", asset_source: "runtime" },
        ],
        next_cursor: null,
      });
    if (path.includes("/ancestors"))
      return Promise.resolve({
        items: [span("s2")],
        quality: "complete",
        next_cursor: null,
      });
    if (path.includes("attempt_id="))
      return Promise.resolve({
        spans: [span("s1"), span("s2")],
        spans_next_cursor: null,
      });
    return Promise.resolve({ run, attempts: [attempt] });
  });
  const view = mount("/agent-observability/runs/abc?hideMenu=true");
  fireEvent.click(await screen.findByText("s1"));
  expect(
    request.mock.calls.some(
      ([p]) => p.includes("/events") || p.includes("/assets"),
    ),
  ).toBe(false);
  fireEvent.click(screen.getByRole("tab", { name: "资产" }));
  expect(await screen.findByText(/来源：runtime/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "事件与关联" }));
  fireEvent.click(screen.getByRole("button", { name: "关联" }));
  const link = await screen.findByRole("link", {
    name: "retry_of · 打开关联 Run",
  });
  expect(link).toHaveAttribute(
    "href",
    "/agent-observability/runs/abc?attempt=a1&span=s2&hideMenu=true",
  );
  fireEvent.click(link);
  await waitFor(() =>
    expect(screen.getByRole("treeitem", { name: /s2/ })).toHaveAttribute(
      "aria-selected",
      "true",
    ),
  );
  state.source = "b";
  view.rerender(
    <MemoryRouter
      initialEntries={["/agent-observability/runs/abc?hideMenu=true"]}
    >
      <Routes>
        <Route path="/agent-observability/runs/:runId" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
  resolveOld({
    items: [{ event_id: "old", event_name: "old source event" }],
    next_cursor: null,
  });
  await waitFor(() =>
    expect(screen.queryByText("old source event")).not.toBeInTheDocument(),
  );
  fireEvent.click(await screen.findByText("s1"));
  fireEvent.click(screen.getByRole("tab", { name: "事件与关联" }));
  expect(await screen.findByText("new source event")).toBeInTheDocument();
});

it("does not relocate a shared target or reopen another branch when refresh adds an attempt", async () => {
  const scroll = vi.fn();
  const previous = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "scrollIntoView",
  );
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: scroll,
  });
  const attempts = [1, 2].map((n) => ({
    attempt_id: `a${n}`,
    attempt_index: n,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  }));
  const makeSpan = (id: string, parent: string | null) => ({
    span_id: id,
    attempt_id: "a1",
    parent_span_id: parent,
    name: id,
    span_type: "tool",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "succeeded",
    quality: "complete",
    duration_us: null,
  });
  const target = makeSpan("target", "parent");
  const parent = makeSpan("parent", null);
  const other = makeSpan("other", null);
  const child = makeSpan("child", "other");
  let refreshing = false;
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : path.includes("/ancestors")
        ? { items: [target, parent], quality: "complete", next_cursor: null }
        : {
            run,
            attempts: refreshing ? attempts : [attempts[0]],
            spans: [parent, target, other, child],
            spans_next_cursor: null,
            generated_at: "2026-01-01T00:00:00Z",
          },
    ),
  );
  mount("/agent-observability/runs/abc?attempt=a1&span=target");
  await waitFor(() => expect(scroll).toHaveBeenCalled());
  const tree = screen.getByRole("tree");
  tree.scrollTop = 150;
  fireEvent.click(screen.getByRole("button", { name: "收起 other" }));
  expect(
    screen.queryByRole("treeitem", { name: /child/ }),
  ).not.toBeInTheDocument();
  scroll.mockClear();
  refreshing = true;
  fireEvent.click(screen.getByRole("button", { name: /刷\s*新/ }));
  await screen.findByText("发现新的 Attempt 2");
  expect(scroll).not.toHaveBeenCalled();
  expect(tree.scrollTop).toBe(150);
  expect(
    screen.queryByRole("treeitem", { name: /child/ }),
  ).not.toBeInTheDocument();
  if (previous)
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", previous);
  else
    delete (HTMLElement.prototype as { scrollIntoView?: unknown })
      .scrollIntoView;
});

it("keeps polling a known background span after switching attempts and stops on its terminal state", async () => {
  const interval = vi
    .spyOn(window, "setInterval")
    .mockReturnValue(42 as unknown as ReturnType<typeof window.setInterval>);
  const clear = vi.spyOn(window, "clearInterval");
  const attempts = [1, 2].map((n) => ({
    attempt_id: `a${n}`,
    attempt_index: n,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  }));
  const background = {
    span_id: "bg",
    attempt_id: "a1",
    parent_span_id: null,
    name: "background",
    span_type: "subagent",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "running",
    quality: "complete",
    duration_us: null,
    subagent: { mode: "background" },
  };
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : path.includes("/ancestors")
        ? {
            items: [{ ...background, status: "succeeded" }],
            quality: "complete",
            next_cursor: null,
          }
        : {
            run,
            attempts,
            spans: path.includes("attempt_id=a1") ? [background] : [],
            spans_next_cursor: null,
            generated_at: "2026-01-01T00:00:00Z",
          },
    ),
  );
  mount("/agent-observability/runs/abc?attempt=a1");
  await screen.findByRole("treeitem", { name: /background/ });
  fireEvent.click(screen.getByRole("button", { name: /Attempt 2/ }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining("attempt_id=a2"),
    ),
  );
  await waitFor(() =>
    expect(interval.mock.calls.some((call) => call[1] === 5000)).toBe(true),
  );
  const polls = interval.mock.calls.filter((call) => call[1] === 5000);
  const poll = polls[polls.length - 1][0] as () => void;
  await act(async () => {
    poll();
  });
  expect(request).toHaveBeenCalledWith(
    expect.stringContaining("/spans/bg/ancestors"),
  );
  expect(clear).toHaveBeenCalledWith(42);
  interval.mockRestore();
  clear.mockRestore();
});

it.each(["load first", "refresh first"])(
  "serializes pagination and refresh when %s starts",
  async (order) => {
    const attempt = {
      attempt_id: "a1",
      attempt_index: 1,
      status: "succeeded",
      started_at: null,
      ended_at: null,
      duration_us: null,
      quality: "complete",
    };
    const makeSpan = (id: string, name: string) => ({
      span_id: id,
      attempt_id: "a1",
      parent_span_id: null,
      name,
      span_type: "tool",
      operation: "execute",
      started_at: null,
      ended_at: null,
      status: "succeeded",
      quality: "complete",
      duration_us: null,
    });
    const first = makeSpan("first", "first");
    const second = makeSpan("second", "second");
    const page = (spans: (typeof first)[], cursor: string | null) => ({
      run,
      attempts: [attempt],
      spans,
      spans_next_cursor: cursor,
      generated_at: "2026-01-01T00:00:10Z",
    });
    let resolveMore!: (value: unknown) => void;
    let resolveRefresh!: (value: unknown) => void;
    let refreshing = false;
    request.mockImplementation((path: string) => {
      if (path.includes("capabilities"))
        return Promise.resolve({ capture_enabled: true });
      if (!path.includes("attempt_id=")) return Promise.resolve(page([], null));
      if (path.includes("span_cursor="))
        return new Promise((resolve) => {
          resolveMore = resolve;
        });
      if (!refreshing) return Promise.resolve(page([first], "next"));
      return new Promise((resolve) => {
        resolveRefresh = resolve;
      });
    });
    mount("/agent-observability/runs/abc");
    const load = await screen.findByRole("button", { name: /加载更多/ });
    await waitFor(() => expect(load).not.toHaveClass("ant-btn-loading"), {
      timeout: 3000,
    });
    const refresh = screen.getByRole("button", { name: /刷\s*新/ });
    const calls = () =>
      request.mock.calls.filter(([path]) =>
        (path as string).includes("attempt_id="),
      ).length;
    if (order === "load first") {
      fireEvent.click(load);
      refreshing = true;
      fireEvent.click(refresh);
      expect(calls()).toBe(2);
      await act(async () => {
        resolveMore(page([makeSpan("first", "older"), second], null));
      });
      fireEvent.click(refresh);
      expect(calls()).toBe(3);
      await act(async () => {
        resolveRefresh(page([makeSpan("first", "newer"), second], null));
      });
    } else {
      refreshing = true;
      fireEvent.click(refresh);
      fireEvent.click(load);
      expect(calls()).toBe(2);
      await act(async () => {
        resolveRefresh(page([makeSpan("first", "older")], "next"));
      });
      fireEvent.click(load);
      expect(calls()).toBe(3);
      await act(async () => {
        resolveMore(page([makeSpan("first", "newer"), second], null));
      });
    }
    expect(screen.getByRole("treeitem", { name: /newer/ })).toBeInTheDocument();
    expect(
      screen.queryByRole("treeitem", { name: /older/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("treeitem", { name: /second/ }),
    ).toBeInTheDocument();
  },
);

it("rejects a shared Span when the URL changes to a different Attempt", async () => {
  const attempts = [1, 2].map((n) => ({
    attempt_id: `a${n}`,
    attempt_index: n,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  }));
  const span = {
    span_id: "s1",
    attempt_id: "a1",
    parent_span_id: null,
    name: "target",
    span_type: "tool",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "succeeded",
    quality: "complete",
    duration_us: null,
  };
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : path.includes("/ancestors")
        ? { items: [span], quality: "complete", next_cursor: null }
        : path.includes("attempt_id=")
        ? { spans: [], spans_next_cursor: null }
        : { run, attempts },
    ),
  );
  function ChangeLink() {
    const navigate = useNavigate();
    return (
      <button onClick={() => navigate("?attempt=a2&span=s1")}>
        Change Attempt
      </button>
    );
  }
  render(
    <MemoryRouter
      initialEntries={["/agent-observability/runs/abc?attempt=a1&span=s1"]}
    >
      <ChangeLink />
      <Routes>
        <Route path="/agent-observability/runs/:runId" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(
    await screen.findByRole("treeitem", { name: /target/ }),
  ).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("button", { name: "Change Attempt" }));
  expect(
    await screen.findByText("目标 Attempt 或 Span 已失效"),
  ).toBeInTheDocument();
  expect(screen.queryByText("定位暂不可用")).not.toBeInTheDocument();
});

it("treats a malformed Span identity as an invalid shared link", async () => {
  const attempt = {
    attempt_id: "a1",
    attempt_index: 1,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  };
  request.mockImplementation((path: string) =>
    path.includes("/ancestors")
      ? Promise.reject(Object.assign(new Error(), { status: 400 }))
      : Promise.resolve(
          path.includes("capabilities")
            ? { capture_enabled: true }
            : path.includes("attempt_id=")
            ? { spans: [], spans_next_cursor: null }
            : { run, attempts: [attempt] },
        ),
  );
  mount("/agent-observability/runs/abc?attempt=a1&span=malformed");
  expect(
    await screen.findByText("目标 Attempt 或 Span 已失效"),
  ).toBeInTheDocument();
  expect(screen.queryByText("定位暂不可用")).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "回到最近 Attempt" }),
  ).toBeInTheDocument();
});
it.each(["Enter", " "])(
  "opens a focused run row with %s without intercepting the link",
  async (key) => {
    request.mockImplementation((path: string) =>
      Promise.resolve(
        path.includes("capabilities")
          ? { capture_enabled: true }
          : path.includes("/runs/")
          ? { run, attempts: [], spans: [] }
          : { items: [run], next_cursor: null },
      ),
    );
    mount();
    const row = await screen.findByRole("row", { name: "打开 Run abc" });
    expect(row).toHaveAttribute("tabindex", "0");
    const link = screen.getByRole("link", { name: "abc" });
    fireEvent.keyDown(link, { key });
    expect(screen.queryByText("Run 摘要")).not.toBeInTheDocument();
    row.focus();
    fireEvent.keyDown(row, { key });
    expect(await screen.findByText("Run 摘要")).toBeInTheDocument();
  },
);

it.each(["refresh", "load"])(
  "releases the page lock after %s fails",
  async (operation) => {
    const attempt = {
      attempt_id: "a1",
      attempt_index: 1,
      status: "succeeded",
      started_at: null,
      ended_at: null,
      duration_us: null,
      quality: "complete",
    };
    const page = {
      run,
      attempts: [attempt],
      spans: [],
      spans_next_cursor: "next",
      generated_at: "2026-01-01T00:00:00Z",
    };
    let failed = false;
    request.mockImplementation((path: string) => {
      if (path.includes("capabilities"))
        return Promise.resolve({ capture_enabled: true });
      if (!path.includes("attempt_id=")) return Promise.resolve(page);
      if (
        !failed &&
        (operation === "load"
          ? path.includes("span_cursor=")
          : request.mock.calls.filter(([p]) =>
              (p as string).includes("attempt_id="),
            ).length > 1)
      ) {
        failed = true;
        return Promise.reject(new Error("failed"));
      }
      return Promise.resolve(page);
    });
    mount("/agent-observability/runs/abc");
    const load = await screen.findByRole("button", { name: /加载更多/ });
    await waitFor(() => expect(load).not.toHaveClass("ant-btn-loading"), {
      timeout: 3000,
    });
    fireEvent.click(
      operation === "load"
        ? load
        : screen.getByRole("button", { name: /刷\s*新/ }),
    );
    await screen.findByText(
      operation === "load" ? "加载 Span 失败" : "刷新失败，请重试",
    );
    const previous = request.mock.calls.length;
    fireEvent.click(
      operation === "load"
        ? screen.getByRole("button", { name: /刷\s*新/ })
        : load,
    );
    await waitFor(() =>
      expect(request.mock.calls.length).toBeGreaterThan(previous),
    );
  },
);

it("keeps the new Attempt lock when an old request finishes, then retries polling", async () => {
  const interval = vi
    .spyOn(window, "setInterval")
    .mockReturnValue(42 as unknown as ReturnType<typeof window.setInterval>);
  const clear = vi.spyOn(window, "clearInterval");
  const attempts = [1, 2].map((n) => ({
    attempt_id: `a${n}`,
    attempt_index: n,
    status: "running",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  }));
  const page = {
    run: { ...run, status: "running" },
    attempts,
    spans: [],
    spans_next_cursor: "next",
    generated_at: "2026-01-01T00:00:00Z",
  };
  let finishOld!: (value: unknown) => void;
  let finishNew!: (value: unknown) => void;
  request.mockImplementation((path: string) => {
    if (path.includes("capabilities"))
      return Promise.resolve({ capture_enabled: true });
    if (path.includes("span_cursor=") && path.includes("a1"))
      return new Promise((resolve) => {
        finishOld = resolve;
      });
    if (path.includes("span_cursor=") && path.includes("a2"))
      return new Promise((resolve) => {
        finishNew = resolve;
      });
    return Promise.resolve(page);
  });
  mount("/agent-observability/runs/abc?attempt=a1");
  let load = await screen.findByRole("button", { name: /加载更多/ });
  await waitFor(() => expect(load).not.toHaveClass("ant-btn-loading"), {
    timeout: 3000,
  });
  fireEvent.click(load);
  fireEvent.click(screen.getByRole("button", { name: /Attempt 2/ }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining("attempt_id=a2"),
    ),
  );
  load = await screen.findByRole("button", { name: /加载更多/ });
  await waitFor(() => expect(load).not.toHaveClass("ant-btn-loading"), {
    timeout: 3000,
  });
  fireEvent.click(load);
  await act(async () => {
    finishOld(page);
  });
  const polls = interval.mock.calls.filter((call) => call[1] === 5000);
  const poll = polls[polls.length - 1][0] as () => void;
  const before = request.mock.calls.length;
  await act(async () => {
    poll();
  });
  expect(request.mock.calls.length).toBe(before);
  await act(async () => {
    finishNew({ ...page, spans_next_cursor: null });
  });
  await act(async () => {
    poll();
  });
  expect(request.mock.calls.length).toBe(before + 1);
  expect(request.mock.calls[before][0]).toContain("attempt_id=a2");
  interval.mockRestore();
  clear.mockRestore();
});
it("clears a cursor when the selected agent changes", async () => {
  request.mockImplementation((p: string) =>
    Promise.resolve(
      p.includes("capabilities")
        ? { capture_enabled: true }
        : { items: [], next_cursor: null },
    ),
  );
  mount("/agent-observability/runs?cursor=old");
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(expect.stringContaining("cursor=old")),
  );
  request.mockClear();
  act(() => useAgentStore.getState().setSelectedAgent("other"));
  await waitFor(() =>
    expect(
      request.mock.calls.some(([p]) => (p as string).includes("/runs?")),
    ).toBe(true),
  );
  expect(
    request.mock.calls.some(([p]) => (p as string).includes("cursor=old")),
  ).toBe(false);
});
it("clears evidence and discards a late response when the selected agent changes", async () => {
  const attempt = {
    attempt_id: "a1",
    attempt_index: 1,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  };
  const span = {
    span_id: "s1",
    attempt_id: "a1",
    parent_span_id: null,
    name: "s1",
    span_type: "tool",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "succeeded",
    quality: "complete",
    duration_us: null,
  };
  let resolveOld!: (value: unknown) => void;
  request.mockImplementation((p: string) => {
    if (p.includes("capabilities"))
      return Promise.resolve({ capture_enabled: true });
    if (p.includes("/evidence")) {
      if (useAgentStore.getState().selectedAgent === "default")
        return new Promise((resolve) => {
          resolveOld = resolve;
        });
      return Promise.resolve({
        items: [
          { evidence_ref: "new", status: "available", message: "new evidence" },
        ],
      });
    }
    if (p.includes("attempt_id="))
      return Promise.resolve({ spans: [span], spans_next_cursor: null });
    return Promise.resolve({ run, attempts: [attempt] });
  });
  mount("/agent-observability/runs/abc");
  fireEvent.click(await screen.findByText("s1"));
  fireEvent.click(screen.getByRole("tab", { name: "正文证据" }));
  await waitFor(() => expect(resolveOld).toBeDefined());
  act(() => useAgentStore.getState().setSelectedAgent("other"));
  await act(async () =>
    resolveOld({
      items: [
        { evidence_ref: "old", status: "available", message: "old evidence" },
      ],
    }),
  );
  expect(screen.queryByText('"old evidence"')).not.toBeInTheDocument();
  fireEvent.click(await screen.findByText("s1"));
  fireEvent.click(screen.getByRole("tab", { name: "正文证据" }));
  expect(await screen.findByText('"new evidence"')).toBeInTheDocument();
});

it("marks backend error conditions and identifies unlisted span types", async () => {
  const attempt = {
    attempt_id: "a1",
    attempt_index: 1,
    status: "succeeded",
    started_at: null,
    ended_at: null,
    duration_us: null,
    quality: "complete",
  };
  const spans = [
    { name: "flagged", has_error: 1 },
    { name: "technical", technical_status: "ERROR" },
    { name: "rejected", status: "rejected" },
    { name: "other", span_type: "Guard" },
  ].map((item, index) => ({
    span_id: `s${index}`,
    attempt_id: "a1",
    parent_span_id: null,
    span_type: "Tool",
    operation: "execute",
    started_at: null,
    ended_at: null,
    status: "succeeded",
    quality: "complete",
    duration_us: null,
    ...item,
  }));
  request.mockImplementation((path: string) =>
    Promise.resolve(
      path.includes("capabilities")
        ? { capture_enabled: true }
        : path.includes("attempt_id=")
        ? { spans, spans_next_cursor: null }
        : { run, attempts: [attempt] },
    ),
  );
  mount("/agent-observability/runs/abc");
  for (const name of ["flagged", "technical", "rejected"]) {
    const row = await screen.findByRole("treeitem", { name: new RegExp(name) });
    expect(within(row).getByText("异常")).toBeInTheDocument();
    expect(
      (row.querySelector('[style*="background-color"]') as HTMLElement).style
        .backgroundColor,
    ).toBe("rgb(224, 77, 107)");
  }
  const other = screen.getByRole("treeitem", { name: /other/ });
  expect(
    (other.querySelector('[style*="background-color"]') as HTMLElement).style
      .backgroundColor,
  ).toBe("rgb(139, 107, 98)");
  expect(screen.getByLabelText("Span 类型图例")).toHaveTextContent("其他类型");
});
