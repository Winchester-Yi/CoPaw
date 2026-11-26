import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import AttemptView from "./AttemptView";
import { MemoryRouter } from "react-router-dom";
const request = vi.hoisted(() => vi.fn());
vi.mock("../../api/request", () => ({ request }));
class Observer {
  observe() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", Observer);
afterEach(() => {
  cleanup();
  request.mockReset();
});
const attempts = [1, 2].map((i) => ({
  attempt_id: `a${i}`,
  attempt_index: i,
  status: i === 1 ? "waiting_approval" : "succeeded",
  started_at: "2026-01-01T00:00:00Z",
  ended_at: null,
  duration_us: null,
  quality: "complete",
}));
const span = (id: string, parent: string | null, attempt = "a2") => ({
  span_id: id,
  attempt_id: attempt,
  parent_span_id: parent,
  name: id,
  span_type: "tool",
  operation: "execute",
  started_at: "2026-01-01T00:00:01Z",
  ended_at: null,
  status: "running",
  quality: "complete",
  duration_us: null,
  input_tokens: 0,
  output_tokens: 0,
});
it("loads only the latest attempt, merges pages into the real tree, and synchronizes selection", async () => {
  request.mockImplementation((url: string) =>
    Promise.resolve(
      url.includes("/ancestors")
        ? {
            items: [span("child", "parent"), span("parent", null)],
            quality: "complete",
          }
        : url.includes("span_cursor")
        ? { spans: [span("parent", null)], spans_next_cursor: null }
        : {
            spans: [span("child", "parent"), span("foreign", null, "a1")],
            spans_next_cursor: "next",
          },
    ),
  );
  render(
    <MemoryRouter>
      <AttemptView
        runId="run"
        attempts={attempts}
        runStatus="succeeded"
        projectedAt={null}
        onRunRefresh={() => {}}
      />
    </MemoryRouter>,
  );
  expect(await screen.findByText("child")).toBeInTheDocument();
  expect(request).toHaveBeenCalledWith(
    expect.stringContaining("attempt_id=a2"),
  );
  expect(screen.queryByText("foreign")).not.toBeInTheDocument();
  expect(await screen.findByText("父节点未加载")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
  await waitFor(() =>
    expect(screen.queryByText("父节点未加载")).not.toBeInTheDocument(),
  );
  fireEvent.click(screen.getByText("child"));
  expect(screen.getByRole("treeitem", { name: /child/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(screen.getByText("Tokens 合计").nextSibling).toHaveTextContent("0");
  fireEvent.click(screen.getByRole("button", { name: /Attempt 1/ }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      expect.stringContaining("attempt_id=a1"),
    ),
  );
});

it("confirms missing parents and preserves microsecond order and keyboard expansion", async () => {
  const parent = {
    ...span("parent", null),
    started_at: "2026-01-01T00:00:01.000002Z",
  };
  const earlier = {
    ...span("z-earlier", null),
    started_at: "2026-01-01T00:00:01.000001Z",
  };
  request.mockImplementation((url: string) =>
    Promise.resolve(
      url.includes("/ancestors")
        ? { items: [span("orphan", "absent")], quality: "parent_missing" }
        : {
            spans: [
              parent,
              earlier,
              span("child", "parent"),
              span("orphan", "absent"),
            ],
            spans_next_cursor: null,
            generated_at: "2026-01-01T00:00:10Z",
          },
    ),
  );
  render(
    <MemoryRouter>
      <AttemptView
        runId="run"
        attempts={attempts}
        runStatus="succeeded"
        projectedAt={null}
        onRunRefresh={() => {}}
      />
    </MemoryRouter>,
  );
  expect(await screen.findByText("父节点缺失")).toBeInTheDocument();
  const rows = screen.getAllByRole("treeitem");
  expect(
    rows.findIndex((row) => row.textContent?.includes("z-earlier")),
  ).toBeLessThan(rows.findIndex((row) => row.textContent?.includes("parent")));
  const toggle = screen.getByRole("button", { name: "收起 parent" });
  fireEvent.keyDown(toggle, { key: "Enter" });
  fireEvent.click(toggle);
  expect(screen.queryByText("child")).not.toBeInTheDocument();
  expect(screen.getByRole("treeitem", { name: /parent/ })).toHaveAttribute(
    "aria-selected",
    "false",
  );
});

it("distinguishes running intervals from unknown quality and displays projected details", async () => {
  const running = {
    ...span("running", null),
    quality: "partial",
    model_key: "model",
    provider_key: "provider",
    subagent: { business_subagent_key: "worker", result_status: "running" },
  };
  const unknown = { ...span("unknown", null), quality: "unknown" };
  request.mockResolvedValue({
    spans: [running, unknown],
    spans_next_cursor: null,
    generated_at: "2026-01-01T00:00:10Z",
  });
  render(
    <MemoryRouter>
      <AttemptView
        runId="run"
        attempts={attempts}
        runStatus="succeeded"
        projectedAt={null}
        onRunRefresh={() => {}}
      />
    </MemoryRouter>,
  );
  const row = await screen.findByRole("treeitem", { name: /running/ });
  expect(row).toHaveTextContent("进行中");
  expect(row.querySelector('[style*="width"]')).toHaveStyle({ width: "90%" });
  expect(screen.getByRole("treeitem", { name: /unknown/ })).toHaveTextContent(
    "结束时间未知",
  );
  fireEvent.click(row);
  expect(screen.getByText("provider")).toBeInTheDocument();
  expect(screen.getByText("worker")).toBeInTheDocument();
  expect(screen.getByText("开始时间")).toBeInTheDocument();
});

it("shows request facts and distinguishes missing usage from zero", async () => {
  request.mockResolvedValue({
    spans: [
      {
        ...span("provider-request", null),
        span_type: "LLM",
        operation: "request_attempt",
        request_attempt_index: 2,
        response_model_key: "deepseek-v4-flash",
        model_key_source: "response",
        streaming: true,
        finish_reason: "stop",
        input_tokens: 0,
        output_tokens: null,
      },
    ],
    spans_next_cursor: null,
  });
  render(
    <MemoryRouter>
      <AttemptView
        runId="run"
        attempts={attempts}
        runStatus="succeeded"
        projectedAt={null}
        onRunRefresh={() => {}}
      />
    </MemoryRouter>,
  );
  fireEvent.click(
    await screen.findByRole("treeitem", { name: /provider-request/ }),
  );
  expect(screen.getByText("请求序号").nextSibling).toHaveTextContent("2");
  expect(screen.getByText("实际模型").nextSibling).toHaveTextContent(
    "deepseek-v4-flash",
  );
  expect(screen.getByText("流式模式").nextSibling).toHaveTextContent("流式");
  expect(screen.getByText("结束原因").nextSibling).toHaveTextContent("stop");
  expect(screen.getByText("输入 Tokens").nextSibling).toHaveTextContent("0");
  expect(screen.getByText("输出 Tokens").nextSibling).toHaveTextContent(
    "未获取",
  );
  expect(screen.getByText("Tokens 合计").nextSibling).toHaveTextContent(
    "未获取",
  );
});

it("shows compaction estimates and checkpoint actions in the selected span", async () => {
  const checkpointId = "750ade7f-fcce-4f4a-aa51-fc2a1273ced1";
  request.mockResolvedValue({
    spans: [
      {
        ...span("compaction", null),
        span_type: "Context",
        operation: "compact",
        compaction_strategy: "checkpoint_degraded",
        context_before_tokens: 0,
        context_after_tokens: null,
      },
      ...["save", "restore"].map((operation) => ({
        ...span(`checkpoint-${operation}`, null),
        span_type: "Checkpoint",
        operation,
        checkpoint_id: operation === "save" ? checkpointId : null,
      })),
    ],
    spans_next_cursor: null,
  });
  render(
    <MemoryRouter>
      <AttemptView
        runId="run"
        attempts={attempts}
        runStatus="succeeded"
        projectedAt={null}
        onRunRefresh={() => {}}
      />
    </MemoryRouter>,
  );

  fireEvent.click(await screen.findByRole("treeitem", { name: /compaction/ }));
  expect(screen.getByText("压缩策略").nextSibling).toHaveTextContent(
    "应用降级 Checkpoint",
  );
  expect(
    screen.getByText("压缩前 Tokens（估算）").nextSibling,
  ).toHaveTextContent("0");
  expect(
    screen.getByText("压缩后 Tokens（估算）").nextSibling,
  ).toHaveTextContent("—");
  expect(screen.queryByText("Tokens 合计")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("treeitem", { name: /checkpoint-save/ }));
  expect(screen.getByText("Checkpoint 动作").nextSibling).toHaveTextContent(
    "保存",
  );
  expect(screen.getByText("Checkpoint ID").nextSibling).toHaveTextContent(
    checkpointId,
  );
  expect(screen.queryByText("压缩策略")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("treeitem", { name: /checkpoint-restore/ }));
  expect(screen.getByText("Checkpoint 动作").nextSibling).toHaveTextContent(
    "恢复",
  );
  expect(screen.getByText("Checkpoint ID").nextSibling).toHaveTextContent("—");
});

it("keeps the Attempt overview visible in a narrow container", async () => {
  class NarrowObserver {
    callback: ResizeObserverCallback;
    constructor(callback: ResizeObserverCallback) {
      this.callback = callback;
    }
    observe() {
      this.callback(
        [{ contentRect: { width: 700 } } as ResizeObserverEntry],
        this as unknown as ResizeObserver,
      );
    }
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", NarrowObserver);
  request.mockResolvedValue({ spans: [], spans_next_cursor: null });
  render(
    <MemoryRouter>
      <AttemptView
        runId="run"
        attempts={attempts}
        runStatus="succeeded"
        projectedAt={null}
        onRunRefresh={() => {}}
      />
    </MemoryRouter>,
  );
  expect(await screen.findByText("Attempt 概览")).toBeInTheDocument();
  vi.stubGlobal("ResizeObserver", Observer);
});

it("orders whole seconds and fractional seconds in UTC", async () => {
  request.mockResolvedValue({
    spans: [
      { ...span("fraction", null), started_at: "2026-01-01T00:00:01.000001Z" },
      { ...span("whole", null), started_at: "2026-01-01T08:00:01+08:00" },
      { ...span("later", null), started_at: "2026-01-01T00:00:01.000002Z" },
    ],
    spans_next_cursor: null,
  });
  render(
    <MemoryRouter>
      <AttemptView
        runId="run"
        attempts={attempts}
        runStatus="succeeded"
        projectedAt={null}
        onRunRefresh={() => {}}
      />
    </MemoryRouter>,
  );
  await screen.findByText("fraction");
  expect(screen.getAllByRole("treeitem").map((row) => row.textContent)).toEqual(
    [
      expect.stringContaining("whole"),
      expect.stringContaining("fraction"),
      expect.stringContaining("later"),
    ],
  );
});

it.each([
  ["parent_missing", "祖先节点缺失"],
  ["depth_limited", "祖先路径深度受限"],
  ["cycle", "祖先路径存在循环"],
])("shows %s even when the direct parent exists", async (quality, label) => {
  request.mockImplementation((url: string) =>
    Promise.resolve(
      url.includes("/ancestors")
        ? {
            items: [span("child", "parent"), span("parent", "grandparent")],
            quality,
          }
        : { spans: [span("child", "parent")], spans_next_cursor: null },
    ),
  );
  render(
    <MemoryRouter>
      <AttemptView
        runId="run"
        attempts={attempts}
        runStatus="succeeded"
        projectedAt={null}
        onRunRefresh={() => {}}
      />
    </MemoryRouter>,
  );
  expect(await screen.findByText(new RegExp(label))).toHaveTextContent(
    "父节点未加载",
  );
});

it("keeps the timeline header in the same scroll container as its rows", async () => {
  request.mockResolvedValue({
    spans: [span("row", null)],
    spans_next_cursor: null,
  });
  render(
    <MemoryRouter>
      <AttemptView
        runId="run"
        attempts={attempts}
        runStatus="succeeded"
        projectedAt={null}
        onRunRefresh={() => {}}
      />
    </MemoryRouter>,
  );
  const row = await screen.findByRole("treeitem", { name: /row/ });
  const tree = screen.getByRole("tree", { name: "执行树" });
  expect(tree).toContainElement(
    screen.getByLabelText("时间轴（相对 Attempt 起点）"),
  );
  expect(tree).toContainElement(row);
});
