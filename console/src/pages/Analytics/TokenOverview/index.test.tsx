import {
  act,
  cleanup,
  fireEvent,
  waitFor,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  tokenDiagnosisV2Api,
  DIAGNOSIS_RULE_IDS,
  type DashboardDTO,
  type DiagnosisPage,
  type TaskListItemDTO,
} from "../../../api/modules/tokenDiagnosisV2";
import TokenOverview from "./index";

vi.mock("../../../api/modules/tokenDiagnosisV2", async (original) => ({
  ...(await original<typeof import("../../../api/modules/tokenDiagnosisV2")>()),
  tokenDiagnosisV2Api: { dashboard: vi.fn(), tasks: vi.fn() },
}));
// Numeric wire values captured from the real Monitor reader over isolated SQLite.
const usage = {
  known_input_tokens: 6000,
  known_output_tokens: 0,
  known_total_tokens: 6000,
  actual_total_tokens: null,
  observed_call_count: 3,
  reported_call_count: 2,
  partial_call_count: 0,
  unknown_call_count: 1,
  usage_complete: false,
};
const metadata = {
  scope: { source_id: "source", tenant_id: "tenant" },
  window: {
    start: "2026-10-12T00:00:00Z",
    end: "2026-10-13T00:00:00Z",
    semantics: "UTC half-open [start,end)",
  },
  as_of: "2026-10-13T03:52:26.345656Z",
};
function dashboard(): DashboardDTO {
  return {
    ...metadata,
    window_attempt_usage: { ...usage },
    lifecycle_distribution: {
      task_count: 1,
      sample_count: 0,
      mean_total_tokens: null,
      min_total_tokens: null,
      max_total_tokens: null,
      population: "terminal tasks with complete lifecycle Usage",
      quantiles: {
        sample_count: 0,
        p50: null,
        p90: null,
        p95: null,
        method: "nearest_rank",
        available: false,
        reason_code: "no_eligible_tasks",
      },
    },
    projection_filters: {
      window_attempt_usage: [
        "start",
        "end",
        "agent_id",
        "root_agent_id",
        "model_name",
        "branch_run_id",
        "state",
      ],
    },
    agent_attribution: {
      executing_agents: {
        items: [{ agent_id: "executor", usage }],
        limited: false,
        population: "window attempts grouped by agent",
      },
      root_agents: {
        items: [{ agent_id: "root-agent", usage }],
        limited: false,
        population: "window attempts grouped by agent",
      },
    },
    rule_coverage: DIAGNOSIS_RULE_IDS.map((rule_id, index) => ({
      rule_id,
      branch_count: 1,
      evaluation_count: index === 0 ? 3 : 0,
      candidate: index === 0 ? 1 : 0,
      not_hit: 0,
      insufficient_evidence: index === 0 ? 2 : 0,
      not_applicable: 0,
      pending: index === 0 ? 0 : 1,
      failed: 0,
      population:
        "current evaluation objects; pending/failed count branches without a current head",
    })),
    prefix: {
      request_count: 1,
      matched_text_bytes: 1,
      eligible_text_bytes: 5,
      excluded: {
        pending: 0,
        failed: 0,
        candidate_limit: 0,
        media: 0,
        partial_context: 0,
        missing_evidence: 2,
        no_history: 0,
        empty_text: 0,
      },
      population: "window attempts with a current saved comparison",
      limited: false,
      ratio: 0.2,
    },
    limits: { available: false, reason_code: "deployment_limits_unavailable" },
  };
}
const task: TaskListItemDTO = {
  task_run_id: "task",
  branch_run_id: "branch",
  parent_branch_run_id: null,
  source_id: "source",
  tenant_id: "tenant",
  trace_id: null,
  agent_id: "root-agent",
  role: "main",
  purpose: "query",
  phase_id: "phase",
  started_at: "2026-10-12T11:00:00",
  ended_at: "2026-10-12T14:00:00",
  execution_state: "completed",
  analysis_state: "ready",
  data_revision: 8,
  analyzed_revision: 8,
  max_input_tokens: 12000,
  max_output_tokens: null,
  compaction_epoch: 0,
  expected_attempt_count: 3,
  gap_count: 0,
  gap_tracking_limited: false,
  completeness: {
    branch_count: 1,
    expected_attempt_count: 3,
    observed_call_count: 3,
    missing_child_count: 0,
    gap_count: 0,
    gap_tracking_limited: false,
    capture_state: "complete",
    usage_complete: false,
  },
  lifecycle_usage: usage,
  selected_contribution: usage,
  window_attempt_usage: usage,
};
const tasks: DiagnosisPage<TaskListItemDTO> = {
  ...metadata,
  items: [task],
  next_cursor: null,
};
function Location() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname + location.search + location.hash}
    </output>
  );
}
function open(search = "") {
  return render(
    <MemoryRouter initialEntries={[`/analytics/token-overview${search}`]}>
      <TokenOverview />
      <Location />
    </MemoryRouter>,
  );
}
afterEach(cleanup);
beforeEach(() => {
  vi.mocked(tokenDiagnosisV2Api.dashboard)
    .mockReset()
    .mockResolvedValue(dashboard());
  vi.mocked(tokenDiagnosisV2Api.tasks).mockReset().mockResolvedValue(tasks);
});
describe("Token overview evidence semantics", () => {
  it("labels partial known usage, distinguishes zero from unknown and uses global task count", async () => {
    open();
    const usageRegion = await screen.findByRole("region", {
      name: "窗口已知用量",
    });
    expect(
      within(usageRegion).getByText("已知 Token 总量（部分）"),
    ).toBeInTheDocument();
    expect(within(usageRegion).getAllByText("6,000")[0]).toBeInTheDocument();
    expect(within(usageRegion).getByText("0")).toBeInTheDocument();
    expect(within(usageRegion).getByText(/真实总量：未知/)).toBeInTheDocument();
    expect(within(usageRegion).getByText("2 / 3（66.7%）")).toBeInTheDocument();
    expect(screen.getByText("已记录 Task：1")).toBeInTheDocument();
    expect(screen.queryByLabelText("规则")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("状态")).not.toBeInTheDocument();
    expect(screen.getByText(/物理 attempt started_at/)).toBeInTheDocument();
    expect(screen.getAllByText(/根 Task started_at/)[0]).toBeInTheDocument();
    const taskList = screen.getByRole("region", { name: "已记录 Task 列表" });
    expect(
      within(taskList).getByText(
        /执行 Agent 与模型按生命周期内对应尝试选择样本/,
      ),
    ).toBeInTheDocument();
    expect(
      within(taskList).getByText(/根 Agent 按根任务归属选择/),
    ).toBeInTheDocument();
  });
  it("renders byte-weighted conservative prefix and unavailable null participation", async () => {
    const { unmount } = open();
    const prefix = await screen.findByRole("region", {
      name: "保守文本前缀比例",
    });
    expect(within(prefix).getByText("20.0%")).toBeInTheDocument();
    expect(within(prefix).getByText("1 / 5 bytes")).toBeInTheDocument();
    expect(within(prefix).getByText("参与请求：1")).toBeInTheDocument();
    expect(within(prefix).getByText("缺少比较证据：2")).toBeInTheDocument();
    unmount();
    const noPrefix = dashboard();
    noPrefix.prefix = {
      ...noPrefix.prefix,
      request_count: 0,
      matched_text_bytes: 0,
      eligible_text_bytes: 0,
      ratio: null,
    };
    vi.mocked(tokenDiagnosisV2Api.dashboard).mockResolvedValue(noPrefix);
    open();
    const unavailable = await screen.findByRole("region", {
      name: "保守文本前缀比例",
    });
    expect(within(unavailable).getByText("不可用")).toBeInTheDocument();
    expect(within(unavailable).queryByText("0.0%")).not.toBeInTheDocument();
  });
  it("keeps pending and failed rule coverage visible separately from evaluation outcomes", async () => {
    const data = dashboard();
    data.rule_coverage[1].pending = 0;
    data.rule_coverage[1].failed = 1;
    vi.mocked(tokenDiagnosisV2Api.dashboard).mockResolvedValue(data);
    open();
    const coverage = await screen.findByRole("region", {
      name: "规则评估覆盖",
    });
    const rows = within(coverage).getAllByRole("row");
    expect(rows).toHaveLength(9);
    expect(
      within(coverage).getByRole("columnheader", { name: "待评估分支" }),
    ).toBeInTheDocument();
    expect(
      within(coverage).getByRole("columnheader", { name: "分析失败分支" }),
    ).toBeInTheDocument();
    expect(rows[2]).toHaveTextContent("R02 内容持续携带");
    expect(within(rows[2]).getAllByRole("cell")[6]).toHaveTextContent("1");
    expect(rows[3]).toHaveTextContent("R03 工具结果规模");
    expect(within(rows[3]).getAllByRole("cell")[5]).toHaveTextContent("1");
  });
  it("task link preserves filters, host parameters, exact actual window and returnTo, clears resource cursors", async () => {
    const exactWindow = {
      ...metadata.window,
      start: "2026-10-12T00:00:00.123456Z",
      end: "2026-10-13T00:00:00.654321Z",
    };
    vi.mocked(tokenDiagnosisV2Api.tasks).mockResolvedValue({
      ...tasks,
      window: exactWindow,
    });
    open(
      "?agent_id=executor&root_agent_id=root-agent&model_name=model&hideMenu=true&source=host&origin=Y&cursor=old&relation_cursor=relation&branch_run_id=old-branch&call_id=old-call&context_id=old-context&evaluation_id=old-evaluation",
    );
    const link = await screen.findByRole("link", { name: "task" });
    const url = new URL(link.getAttribute("href")!, "https://console.invalid");
    expect(url.pathname).toBe("/analytics/token-executions/task");
    for (const [key, value] of Object.entries({
      agent_id: "executor",
      root_agent_id: "root-agent",
      model_name: "model",
      hideMenu: "true",
      source: "host",
      origin: "Y",
      start: exactWindow.start,
      end: exactWindow.end,
    }))
      expect(url.searchParams.get(key)).toBe(value);
    expect(url.searchParams.has("cursor")).toBe(false);
    expect(url.searchParams.has("relation_cursor")).toBe(false);
    expect(url.searchParams.has("branch_run_id")).toBe(false);
    for (const key of ["call_id", "context_id", "evaluation_id"])
      expect(url.searchParams.has(key)).toBe(false);
    expect(url.searchParams.get("returnTo")).toContain(
      "/analytics/token-overview?",
    );
  });
  it("503 gives a retryable unavailable state, never a zero-issue conclusion", async () => {
    vi.mocked(tokenDiagnosisV2Api.dashboard).mockRejectedValue(
      Object.assign(new Error("503"), { status: 503 }),
    );
    open();
    expect(
      await screen.findByText("Token 诊断数据服务暂不可用"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重试总览" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "规则评估覆盖" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/0 个问题|暂无问题/)).not.toBeInTheDocument();
  });
  it("paging locks returned microsecond window before cursor and leaves global metrics unpaged", async () => {
    vi.mocked(tokenDiagnosisV2Api.tasks).mockResolvedValue({
      ...tasks,
      next_cursor: "next",
      window: {
        ...metadata.window,
        start: "2026-10-12T00:00:00.123456Z",
        end: "2026-10-13T00:00:00.654321Z",
      },
    });
    open("?hideMenu=true");
    fireEvent.click(await screen.findByRole("button", { name: "下一页 Task" }));
    await screen.findByText(/cursor=next/, { selector: "output" });
    expect(
      vi.mocked(tokenDiagnosisV2Api.tasks).mock.lastCall?.[1],
    ).toMatchObject({
      cursor: "next",
      start: "2026-10-12T00:00:00.123456Z",
      end: "2026-10-13T00:00:00.654321Z",
    });
    expect(
      vi.mocked(tokenDiagnosisV2Api.dashboard).mock.lastCall?.[1],
    ).not.toHaveProperty("cursor");
    expect(screen.getByText("已记录 Task：1")).toBeInTheDocument();
  });
  it("filter apply reloads and aborts previous requests; late responses cannot replace the active window", async () => {
    let resolveOld: ((value: DashboardDTO) => void) | undefined;
    vi.mocked(tokenDiagnosisV2Api.dashboard).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    open(
      "?hideMenu=true&source=host&agent_id=old&cursor=page&state=failed&rule_id=context_growth",
    );
    await waitFor(() =>
      expect(tokenDiagnosisV2Api.dashboard).toHaveBeenCalledTimes(1),
    );
    const oldSignal = vi.mocked(tokenDiagnosisV2Api.dashboard).mock.calls[0][0];
    const latest = dashboard();
    latest.window_attempt_usage = { ...usage, known_total_tokens: 12345 };
    vi.mocked(tokenDiagnosisV2Api.dashboard).mockResolvedValue(latest);
    fireEvent.change(screen.getByLabelText("执行 Agent"), {
      target: { value: "new" },
    });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    const region = await screen.findByRole("region", { name: "窗口已知用量" });
    expect(within(region).getByText("12,345")).toBeInTheDocument();
    expect(oldSignal.aborted).toBe(true);
    expect(
      vi.mocked(tokenDiagnosisV2Api.dashboard).mock.lastCall?.[1],
    ).toMatchObject({ agent_id: "new" });
    expect(
      vi.mocked(tokenDiagnosisV2Api.dashboard).mock.lastCall?.[1],
    ).not.toHaveProperty("state");
    expect(
      vi.mocked(tokenDiagnosisV2Api.tasks).mock.lastCall?.[1],
    ).not.toHaveProperty("rule_id");
    await act(async () => {
      resolveOld?.(dashboard());
    });
    expect(within(region).getByText("12,345")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("hideMenu=true");
    expect(screen.getByTestId("location")).not.toHaveTextContent("cursor=");
    fireEvent.click(screen.getByRole("button", { name: "重置筛选" }));
    await waitFor(() =>
      expect(
        vi.mocked(tokenDiagnosisV2Api.dashboard).mock.lastCall?.[1]?.agent_id,
      ).toBeUndefined(),
    );
    expect(screen.getByTestId("location")).not.toHaveTextContent("agent_id=");
  });
  it("uses aggregate count beyond one visible page and displays real zero total distinctly", async () => {
    const data = dashboard();
    data.lifecycle_distribution.task_count = 91;
    data.window_attempt_usage = {
      ...usage,
      known_total_tokens: 0,
      known_input_tokens: 0,
      actual_total_tokens: 0,
      usage_complete: true,
    };
    vi.mocked(tokenDiagnosisV2Api.dashboard).mockResolvedValue(data);
    open();
    const region = await screen.findByRole("region", { name: "窗口已知用量" });
    expect(screen.getByText("已记录 Task：91")).toBeInTheDocument();
    expect(
      within(region).getByText("已知 Token 总量（完整）"),
    ).toBeInTheDocument();
    expect(within(region).getByText(/真实总量：0/)).toBeInTheDocument();
  });
  it("scrolls and focuses the Task landing only after async content is ready, once per hash entry", async () => {
    const scrollIntoView = vi.fn();
    const originalScroll = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollIntoView",
    );
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    try {
      let finishTasks:
        | ((value: DiagnosisPage<TaskListItemDTO>) => void)
        | undefined;
      vi.mocked(tokenDiagnosisV2Api.tasks).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishTasks = resolve;
          }),
      );
      open("#task-list");
      await screen.findByRole("region", { name: "窗口已知用量" });
      expect(scrollIntoView).not.toHaveBeenCalled();
      await act(async () => {
        finishTasks?.(tasks);
      });
      const target = screen.getByRole("region", { name: "已记录 Task 列表" });
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" });
      expect(scrollIntoView.mock.instances[0]).toBe(target);
      expect(target).toHaveFocus();
      fireEvent.click(screen.getByRole("button", { name: /刷\s*新/ }));
      await screen.findByRole("region", { name: "窗口已知用量" });
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(screen.queryByText("已定位到 Task 列表")).not.toBeInTheDocument();
    } finally {
      if (originalScroll)
        Object.defineProperty(
          HTMLElement.prototype,
          "scrollIntoView",
          originalScroll,
        );
      else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
    }
  });
});
