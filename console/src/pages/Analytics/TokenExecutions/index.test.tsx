import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  tokenDiagnosisV2Api,
  type ContextDTO,
} from "../../../api/modules/tokenDiagnosisV2";
import TokenExecutionsPage from "./index";
import {
  task,
  branches,
  calls,
  contexts,
  prefixes,
  evaluations,
  operations,
  metadata,
} from "./readerFixture";

vi.mock("../../../api/modules/tokenDiagnosisV2", async (original) => ({
  ...(await original<typeof import("../../../api/modules/tokenDiagnosisV2")>()),
  tokenDiagnosisV2Api: {
    task: vi.fn(),
    branches: vi.fn(),
    calls: vi.fn(),
    context: vi.fn(),
    prefix: vi.fn(),
    evaluation: vi.fn(),
    operations: vi.fn(),
  },
}));
function Location() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname + location.search}
    </output>
  );
}
function open(search = "") {
  return render(
    <MemoryRouter
      initialEntries={[`/analytics/token-executions/task${search}`]}
    >
      <Routes>
        <Route
          path="/analytics/token-executions/:taskId"
          element={<TokenExecutionsPage />}
        />
      </Routes>
      <Location />
    </MemoryRouter>,
  );
}
afterEach(cleanup);
beforeEach(() => {
  vi.mocked(tokenDiagnosisV2Api.task).mockReset().mockResolvedValue(task);
  vi.mocked(tokenDiagnosisV2Api.branches)
    .mockReset()
    .mockResolvedValue(branches);
  vi.mocked(tokenDiagnosisV2Api.calls).mockReset().mockResolvedValue(calls);
  vi.mocked(tokenDiagnosisV2Api.context)
    .mockReset()
    .mockImplementation(async (id) => contexts[id]);
  vi.mocked(tokenDiagnosisV2Api.prefix)
    .mockReset()
    .mockImplementation(async (id) => prefixes[id]);
  vi.mocked(tokenDiagnosisV2Api.evaluation)
    .mockReset()
    .mockImplementation(async (id) => evaluations[id]);
  vi.mocked(tokenDiagnosisV2Api.operations)
    .mockReset()
    .mockResolvedValue(operations);
});
describe("Token execution evidence", () => {
  it("switches the complete Call inspection together and keeps NULL Usage unknown", async () => {
    open();
    await screen.findByRole("region", { name: "任务用量范围" });
    await waitFor(() =>
      expect(tokenDiagnosisV2Api.calls).toHaveBeenCalledWith(
        "task",
        expect.any(AbortSignal),
        expect.objectContaining({
          start: metadata.window.start,
          end: metadata.window.end,
        }),
      ),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("status", { name: "加载Call" }),
      ).not.toBeInTheDocument(),
    );
    const call1 = screen.getByRole("button", {
      name: "选择 Call call-1",
    });
    expect(call1).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByText("缺少比较证据")).toBeInTheDocument();
    expect(tokenDiagnosisV2Api.task).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "选择 Call call-2" }));
    await waitFor(() =>
      expect(
        within(screen.getByRole("region", { name: "文本前缀证据" })).getByText(
          "ctx-2",
        ),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "选择 Call call-3" }));
    const usage = screen.getByRole("region", { name: "当前 Call Usage" });
    expect(within(usage).getByText("3,000")).toBeInTheDocument();
    expect(within(usage).getByText("输出 Token：未知")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        within(screen.getByRole("region", { name: "Context 构成" })).getByText(
          "ctx-3",
        ),
      ).toBeInTheDocument(),
    );
    expect(
      within(screen.getByRole("region", { name: "Context 构成" })).getAllByText(
        "估算 300 Token",
      )[0],
    ).toBeInTheDocument();
    expect(await screen.findByText("历史分析已落后")).toBeInTheDocument();
    expect(
      await screen.findByText(/controlled-instance-3/),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "任务用量范围" })).getByText(
        /任务完整生命周期/,
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/业务 Step 身份未记录/)[0]).toBeInTheDocument();
  });
  it("does not let a late obsolete Context overwrite Call 3", async () => {
    let resolve!: (value: ContextDTO) => void;
    vi.mocked(tokenDiagnosisV2Api.context).mockImplementation((id) =>
      id === "ctx-1"
        ? new Promise((done) => {
            resolve = done;
          })
        : Promise.resolve(contexts[id]),
    );
    open();
    await screen.findByRole("button", { name: "选择 Call call-3" });
    await waitFor(() => expect(resolve).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "选择 Call call-3" }));
    await screen.findByText(/controlled-instance-3/);
    await act(async () => resolve(contexts["ctx-1"]));
    expect(
      within(screen.getByRole("region", { name: "Context 构成" })).getByText(
        "ctx-3",
      ),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Context 构成" })).queryByText(
        "ctx-1",
      ),
    ).not.toBeInTheDocument();
  });
  it("keeps an explicit missing Call unresolved, even when a context locator exists", async () => {
    open("?call_id=not-on-page&context_id=ctx-3&branch_run_id=branch");
    expect(
      await screen.findByText(/请求的 Call 尚未在当前页解析/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "当前 Call Usage" }),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByText(/Context 与请求的 Call 不匹配/),
    ).toBeInTheDocument();
  });
  it("pins the response UTC window and keeps all page cursors independent", async () => {
    vi.mocked(tokenDiagnosisV2Api.calls).mockResolvedValue({
      ...calls,
      next_cursor: "call-next",
    });
    vi.mocked(tokenDiagnosisV2Api.branches).mockResolvedValue({
      ...branches,
      next_cursor: "branch-next",
    });
    open("?origin=host&hideMenu=true&model_name=controlled-model");
    await screen.findByRole("button", { name: "下一页 Call" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "下一页 Call" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "下一页 Call" }));
    await waitFor(() =>
      expect(tokenDiagnosisV2Api.calls).toHaveBeenLastCalledWith(
        "task",
        expect.any(AbortSignal),
        expect.objectContaining({
          cursor: "call-next",
          start: metadata.window.start,
          end: metadata.window.end,
        }),
      ),
    );
    expect(tokenDiagnosisV2Api.branches).toHaveBeenLastCalledWith(
      "task",
      expect.any(AbortSignal),
      expect.not.objectContaining({ cursor: "call-next" }),
    );
    expect(screen.getByTestId("location")).toHaveTextContent("hideMenu=true");
    expect(screen.getByTestId("location")).toHaveTextContent("origin=host");
  });
  it("uses operation-owned context pairs, a server saved estimate and separately paged relations", async () => {
    const operation = operations.items[0];
    vi.mocked(tokenDiagnosisV2Api.operations).mockResolvedValue({
      ...operations,
      items: [
        {
          ...operation,
          segments: { ...operation.segments, next_cursor: "relation-next" },
        },
      ],
      next_cursor: "operation-next",
    });
    open();
    const panel = await screen.findByRole("region", {
      name: "操作 controlled-operation",
    });
    expect(within(panel).getByText("operation-before")).toBeInTheDocument();
    expect(within(panel).getByText("operation-after")).toBeInTheDocument();
    expect(
      within(panel).getByText("服务器估算减少：100 Token"),
    ).toBeInTheDocument();
    expect(within(panel).getByText("业务质量：未知")).toBeInTheDocument();
    fireEvent.click(
      within(panel).getByRole("button", {
        name: "下一页 关系 controlled-operation",
      }),
    );
    await waitFor(() =>
      expect(tokenDiagnosisV2Api.operations).toHaveBeenLastCalledWith(
        "task",
        expect.any(AbortSignal),
        expect.not.objectContaining({ cursor: "relation-next" }),
        {
          operation_id: "controlled-operation",
          relation_cursor: "relation-next",
        },
      ),
    );
  });
  it("recovers a 503 without losing filters or unsafe returnTo navigation", async () => {
    vi.mocked(tokenDiagnosisV2Api.task).mockRejectedValueOnce(
      Object.assign(new Error("offline"), { status: 503 }),
    );
    open("?model_name=controlled-model&returnTo=https%3A%2F%2Fevil.invalid");
    expect(
      await screen.findByText("Token 诊断数据服务暂不可用"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试任务" }));
    await screen.findByRole("button", { name: "选择 Call call-1" });
    expect(screen.getByTestId("location")).toHaveTextContent(
      "model_name=controlled-model",
    );
    expect(
      screen.getByRole("link", { name: "返回问题或任务列表" }),
    ).toHaveAttribute(
      "href",
      expect.stringContaining("/analytics/token-overview"),
    );
  });
  it("resolves a context deep locator to its own Call and preserves the exact return path", async () => {
    const returnTo =
      "/analytics/token-issues?cursor=issue-page&rule_id=context_growth&hideMenu=true#instances";
    open(
      `?branch_run_id=branch&context_id=ctx-3&returnTo=${encodeURIComponent(
        returnTo,
      )}`,
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "选择 Call call-3" }),
      ).toHaveAttribute("aria-pressed", "true"),
    );
    expect(
      within(screen.getByRole("region", { name: "当前 Call Usage" })).getByText(
        "call-3",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "返回问题或任务列表" }),
    ).toHaveAttribute("href", returnTo);
  });
  it("pages occurrences, prefix candidates and operations without mixing their cursors", async () => {
    vi.mocked(tokenDiagnosisV2Api.context).mockResolvedValue({
      ...contexts["ctx-2"],
      occurrences: {
        ...contexts["ctx-2"].occurrences,
        next_cursor: "occ-next",
      },
    });
    vi.mocked(tokenDiagnosisV2Api.prefix).mockResolvedValue({
      ...prefixes["call-2"],
      candidates: {
        ...prefixes["call-2"].candidates,
        next_cursor: "prefix-next",
      },
    });
    vi.mocked(tokenDiagnosisV2Api.operations).mockResolvedValue({
      ...operations,
      next_cursor: "op-next",
    });
    open("?call_id=call-2&branch_run_id=branch");
    for (const [name, api, cursor] of [
      ["occurrences", tokenDiagnosisV2Api.context, "occ-next"],
      ["前缀候选", tokenDiagnosisV2Api.prefix, "prefix-next"],
      ["操作", tokenDiagnosisV2Api.operations, "op-next"],
    ] as const) {
      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: `下一页 ${name}` }),
        ).toBeEnabled(),
      );
      fireEvent.click(screen.getByRole("button", { name: `下一页 ${name}` }));
      await waitFor(() =>
        expect(api).toHaveBeenLastCalledWith(
          expect.any(String),
          expect.any(AbortSignal),
          expect.objectContaining({
            cursor,
            start: metadata.window.start,
            end: metadata.window.end,
          }),
        ),
      );
    }
    expect(tokenDiagnosisV2Api.context).toHaveBeenLastCalledWith(
      "ctx-2",
      expect.any(AbortSignal),
      expect.objectContaining({ cursor: "occ-next" }),
    );
    expect(tokenDiagnosisV2Api.prefix).toHaveBeenLastCalledWith(
      "call-2",
      expect.any(AbortSignal),
      expect.objectContaining({ cursor: "prefix-next" }),
    );
  });
  it("does not show expired operation estimates as savings", async () => {
    const operation = operations.items[0];
    vi.mocked(tokenDiagnosisV2Api.operations).mockResolvedValue({
      ...operations,
      items: [
        {
          ...operation,
          evidence_expired: {
            ...operation.evidence_expired,
            before_context: true,
            summary_call: true,
          },
        },
      ],
    });
    open();
    const panel = await screen.findByRole("region", {
      name: "操作 controlled-operation",
    });
    expect(within(panel).getByText("服务器估算减少：未知")).toBeInTheDocument();
    expect(within(panel).getByText(/摘要 Call 证据已过期/)).toBeInTheDocument();
  });
  it("rejects current prefix evidence saved against another context revision", async () => {
    const prefix = prefixes["call-2"];
    vi.mocked(tokenDiagnosisV2Api.prefix).mockResolvedValue({
      ...prefix,
      comparison: prefix.comparison && {
        ...prefix.comparison,
        current_context_revision: 999,
      },
    });
    open("?call_id=call-2&branch_run_id=branch");
    expect(
      await screen.findByText(/当前前缀保存修订与 Context 修订不一致/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "文本前缀证据" }),
    ).not.toBeInTheDocument();
  });
  it("does not expose the old relation page while a new relation cursor is loading", async () => {
    const operation = operations.items[0];
    vi.mocked(tokenDiagnosisV2Api.operations).mockImplementation(
      (_id, _signal, _filters, options) =>
        options
          ? new Promise(() => {})
          : Promise.resolve({
              ...operations,
              items: [
                {
                  ...operation,
                  segments: { ...operation.segments, next_cursor: "rel-next" },
                },
              ],
            }),
    );
    open();
    const panel = await screen.findByRole("region", {
      name: "操作 controlled-operation",
    });
    expect(within(panel).getByText("retained")).toBeInTheDocument();
    fireEvent.click(
      within(panel).getByRole("button", {
        name: "下一页 关系 controlled-operation",
      }),
    );
    await screen.findByRole("status", { name: "加载操作关系" });
    expect(screen.queryByText("retained")).not.toBeInTheDocument();
  });
  it("groups differing models and phases while keeping synthetic steps unknown", async () => {
    vi.mocked(tokenDiagnosisV2Api.calls).mockResolvedValue({
      ...calls,
      items: calls.items.map((call, index) =>
        index === 2
          ? {
              ...call,
              model_name: "other-model",
              purpose: "summary",
              step_id: "synthetic-step",
              identity_source: "request",
            }
          : call,
      ),
    });
    open();
    expect(
      await screen.findByRole("heading", { name: "other-model / summary" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("synthetic-step")).not.toBeInTheDocument();
    expect(
      screen.getByText("业务 Step 顺序未知；不推断先后关系。"),
    ).toBeInTheDocument();
  });
  it("keeps a missing Context recoverable and reports null attribution and lifecycle explicitly", async () => {
    vi.mocked(tokenDiagnosisV2Api.context).mockRejectedValueOnce(
      Object.assign(new Error("gone"), { status: 404 }),
    );
    open("?call_id=call-3&branch_run_id=branch");
    expect(
      await screen.findByText("当前作用域未找到证据，可能已过期"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试Context" }));
    expect(await screen.findByText(/可靠性：未知/)).toBeInTheDocument();
    expect(screen.getByText(/生命周期：未知/)).toBeInTheDocument();
  });
  it("keeps historical prefix bounds but never borrows a newer Context's occurrence offsets", async () => {
    const context = contexts["ctx-3"],
      prefix = prefixes["call-3"];
    vi.mocked(tokenDiagnosisV2Api.context).mockResolvedValue({
      ...context,
      context: { ...context.context, context_revision: 2 },
      occurrences: {
        ...context.occurrences,
        items: [
          { ...context.occurrences.items[0], text_start: 900, text_end: 1200 },
        ],
      },
    });
    vi.mocked(tokenDiagnosisV2Api.prefix).mockResolvedValue({
      ...prefix,
      is_current: false,
      comparison: prefix.comparison && {
        ...prefix.comparison,
        current_context_revision: 1,
        first_difference_index: 0,
      },
      candidates: {
        ...prefix.candidates,
        items: prefix.candidates.items.map((item) => ({
          ...item,
          first_difference_index: 0,
        })),
      },
    });
    open("?call_id=call-3&branch_run_id=branch");
    const panel = await screen.findByRole("region", { name: "文本前缀证据" });
    await screen.findByRole("region", { name: "Context 构成" });
    expect(within(panel).queryAllByText(/900/)).toHaveLength(0);
    expect(within(panel).queryAllByText(/1,200/)).toHaveLength(0);
    expect(
      await screen.findByText(/历史前缀修订与当前 Context 不一致/),
    ).toBeInTheDocument();
    expect(
      within(panel).getByText("下界 / 上界 / 总文本字节"),
    ).toBeInTheDocument();
    expect(
      within(panel).getAllByText(/当前页无首差 occurrence 字节边界/).length,
    ).toBeGreaterThan(0);
  });
  it.each([
    ["before operation owner", { operation_id: "another-operation" }, {}],
    ["after operation owner", {}, { operation_id: "another-operation" }],
    ["before context role", { context_role: "prepared" }, {}],
    ["after context role", {}, { context_role: "prepared" }],
  ])(
    "does not bind a mismatched %s as an operation saving",
    async (_name, before, after) => {
      const operation = operations.items[0];
      vi.mocked(tokenDiagnosisV2Api.operations).mockResolvedValue({
        ...operations,
        items: [
          {
            ...operation,
            before_context: operation.before_context && {
              ...operation.before_context,
              ...before,
            },
            after_context: operation.after_context && {
              ...operation.after_context,
              ...after,
            },
          },
        ],
      });
      open();
      const panel = await screen.findByRole("region", {
        name: "操作 controlled-operation",
      });
      expect(
        within(panel).getByText("服务器估算减少：未知"),
      ).toBeInTheDocument();
    },
  );
  it.each(["task", "root"])(
    "refuses a summary whose %s belongs to a foreign route task",
    async (part) => {
      vi.mocked(tokenDiagnosisV2Api.task).mockResolvedValue({
        ...task,
        task_run_id: part === "task" ? "foreign-task" : task.task_run_id,
        root: {
          ...task.root,
          task_run_id: part === "root" ? "foreign-task" : task.root.task_run_id,
        },
      });
      open();
      expect(
        await screen.findByText(/任务证据与请求的 Task 不匹配/),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("region", { name: "任务用量范围" }),
      ).not.toBeInTheDocument();
      expect(tokenDiagnosisV2Api.calls).not.toHaveBeenCalled();
    },
  );
  it("excludes foreign task branches and exposes the mismatch without pretending the page is empty", async () => {
    vi.mocked(tokenDiagnosisV2Api.branches).mockResolvedValue({
      ...branches,
      items: [
        {
          ...branches.items[0],
          task_run_id: "foreign-task",
          branch_run_id: "foreign-branch",
        },
      ],
    });
    open();
    expect(
      await screen.findByText(/分支证据与请求的 Task 不匹配/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "选择分支 foreign-branch" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("当前页无分支")).not.toBeInTheDocument();
  });
});
