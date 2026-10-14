import {
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
  type DiagnosisPage,
  type EvaluationDTO,
  type FindingDTO,
  type IssueGroupDTO,
} from "../../../api/modules/tokenDiagnosisV2";
import TokenIssuesPage from "./index";

vi.mock("../../../api/modules/tokenDiagnosisV2", async (original) => ({
  ...(await original<typeof import("../../../api/modules/tokenDiagnosisV2")>()),
  tokenDiagnosisV2Api: {
    issues: vi.fn(),
    instances: vi.fn(),
    evaluation: vi.fn(),
    dashboard: vi.fn(),
  },
}));

const metadata = {
  scope: { source_id: "source", tenant_id: "tenant" },
  window: {
    start: "2026-10-12T00:00:00Z",
    end: "2026-10-13T00:00:00Z",
    semantics: "UTC half-open [start,end)",
  },
  as_of: "2026-10-13T05:56:09.633902Z",
};
const group: IssueGroupDTO = {
  group_id: "s:signature-1",
  reliable_grouping: true,
  rule_id: "context_growth",
  recommendation_code: "review_summary_and_budget",
  hit_count: 12,
  distinct_task_count: 4,
  first_seen: "2026-10-12T14:00:00Z",
  latest_seen: "2026-10-12T15:00:00Z",
};
const unreliableGroup: IssueGroupDTO = {
  ...group,
  group_id: "f:unreliable-1",
  reliable_grouping: false,
  hit_count: 2,
  distinct_task_count: 2,
};
const finding: FindingDTO = {
  finding_id: "finding-1",
  task_run_id: "task-1",
  branch_run_id: "branch-1",
  evaluation_id: "evaluation-1",
  instance_key: "instance-1",
  signature: "signature-1",
  signature_version: 1,
  reliable_grouping: true,
  recommendation_code: "review_summary_and_budget",
  rule_id: "context_growth",
  rule_version: 1,
  threshold_version: 1,
  agent_id: "agent",
  provider_id: "provider",
  model_name: "model",
  call_id: "call-1",
  context_id: "context-1",
  step_id: "step-1",
  operation_id: null,
  created_at: "2026-10-12T14:00:00Z",
  evaluation_state: "candidate",
};
const issuePage: DiagnosisPage<IssueGroupDTO> = {
  ...metadata,
  items: [group, unreliableGroup],
  next_cursor: "issues-next",
};
const instancePage = {
  ...metadata,
  group_id: group.group_id,
  items: [finding],
  next_cursor: null,
};
const evaluation: EvaluationDTO = {
  ...metadata,
  evaluation: {
    evaluation_id: "evaluation-1",
    task_run_id: "task-1",
    branch_run_id: "branch-1",
    object_type: "call",
    object_id: "call-1",
    rule_id: "context_growth",
    state: "candidate",
    input_revision: 10,
    snapshot_id: "snapshot-1",
    rule_version: 1,
    threshold_version: 1,
    created_at: "2026-10-12T14:00:00Z",
    replaces_evaluation_id: null,
  },
  is_current: true,
  values: [
    {
      value_key: "growth",
      unit: "tokens",
      value_type: "integer",
      integer_value: 100,
      decimal_value: null,
      enum_value: null,
      lower_bound: 80,
      upper_bound: 120,
      threshold: 90,
    },
  ],
  gaps: [],
  refs: [],
  findings: [finding],
  recommendation: {
    action_code: "review_summary_and_budget",
    location: "Context assembly",
    prerequisites: ["Confirm attribution"],
    exceptions: [],
    risks: ["May lose evidence"],
    verification: ["Compare quality"],
  },
};
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
    <MemoryRouter initialEntries={[`/analytics/token-issues${search}`]}>
      <Routes>
        <Route path="/analytics/token-issues" element={<TokenIssuesPage />} />
      </Routes>
      <Location />
    </MemoryRouter>,
  );
}
afterEach(cleanup);
beforeEach(() => {
  vi.mocked(tokenDiagnosisV2Api.issues)
    .mockReset()
    .mockResolvedValue(issuePage);
  vi.mocked(tokenDiagnosisV2Api.instances)
    .mockReset()
    .mockResolvedValue(instancePage);
  vi.mocked(tokenDiagnosisV2Api.evaluation)
    .mockReset()
    .mockResolvedValue(evaluation);
  vi.mocked(tokenDiagnosisV2Api.dashboard)
    .mockReset()
    .mockResolvedValue({
      ...metadata,
      window_attempt_usage: {} as never,
      lifecycle_distribution: {} as never,
      projection_filters: {},
      agent_attribution: {
        executing_agents: { items: [], limited: false, population: "" },
        root_agents: { items: [], limited: false, population: "" },
      },
      rule_coverage: [
        {
          rule_id: "context_growth",
          branch_count: 4,
          evaluation_count: 4,
          candidate: 1,
          not_hit: 0,
          insufficient_evidence: 1,
          not_applicable: 1,
          pending: 1,
          failed: 1,
          population: "current",
        },
      ],
      prefix: {} as never,
      limits: { available: false, reason_code: null },
    });
});

describe("Token issues evidence", () => {
  it("shows reliable metrics and keeps non-aggregatable wording", async () => {
    open();
    expect(
      await screen.findByText("12 次命中 / 4 个 Task"),
    ).toBeInTheDocument();
    expect(await screen.findByText("规则覆盖状态")).toBeInTheDocument();
    expect(screen.getByText("证据不足")).toBeInTheDocument();
    expect(screen.getByText("无法可靠聚合")).toBeInTheDocument();
  });

  it("shows coverage service unavailability while preserving issue groups", async () => {
    vi.mocked(tokenDiagnosisV2Api.dashboard).mockRejectedValue({ status: 503 });
    open();
    await waitFor(() =>
      expect(screen.getByText("12 次命中 / 4 个 Task")).toBeInTheDocument(),
    );
    expect(
      await screen.findByText("Token 诊断数据服务暂不可用"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重试规则覆盖状态" }),
    ).toBeInTheDocument();
  });

  it("shows an unbound group when the URL selection is absent from the current page", async () => {
    open("?group_id=s:missing");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toContain("start="),
    );
    await waitFor(() =>
      expect(screen.getByText(/当前页未找到指定问题组/)).toBeInTheDocument(),
    );
    expect(tokenDiagnosisV2Api.instances).not.toHaveBeenCalled();
  });

  it("shows an unbound finding when the URL selection is absent from the current instance page", async () => {
    open("?group_id=s:signature-1&finding_id=missing");
    expect(
      await screen.findByText(/当前页未找到指定问题实例/),
    ).toBeInTheDocument();
    expect(tokenDiagnosisV2Api.evaluation).not.toHaveBeenCalled();
  });

  it("loads a real group instance and builds an exact execution deep link", async () => {
    open("?model_name=model&cursor=issues-cursor");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toContain("start="),
    );
    fireEvent.click(
      (await screen.findAllByRole("button", { name: /查看问题组/ }))[0],
    );
    expect(await screen.findByText("instance-1")).toBeInTheDocument();
    fireEvent.click(screen.getByText("instance-1"));
    const link = await screen.findByRole("link", { name: /打开执行详情/ });
    expect(link).toHaveAttribute(
      "href",
      expect.stringContaining("/analytics/token-executions/task-1?"),
    );
    expect(link.getAttribute("href")).toContain("branch_run_id=branch-1");
    expect(link.getAttribute("href")).toContain("call_id=call-1");
    expect(link.getAttribute("href")).toContain("context_id=context-1");
    expect(link.getAttribute("href")).toContain("evaluation_id=evaluation-1");
    expect(link.getAttribute("href")).toContain(
      "returnTo=%2Fanalytics%2Ftoken-issues",
    );
  });

  it("keeps filters, cursors, selection, and hash in returnTo", async () => {
    open(
      "?model_name=model&agent_id=agent&root_agent_id=root&rule_id=context_growth&state=candidate&cursor=issues-cursor&instances_cursor=instance-cursor&origin=https%3A%2F%2Fhost.example&hideMenu=true#evidence",
    );
    await waitFor(() => {
      const current = screen.getByTestId("location").textContent || "";
      expect(current).toContain("start=2026-10-12T00%3A00%3A00Z");
      expect(current).toContain("end=2026-10-13T00%3A00%3A00Z");
    });
    fireEvent.click(
      (await screen.findAllByRole("button", { name: /查看问题组/ }))[0],
    );
    fireEvent.click(await screen.findByText("instance-1"));
    const link = await screen.findByRole("link", { name: /打开执行详情/ });
    expect(vi.mocked(tokenDiagnosisV2Api.evaluation)).toHaveBeenCalledWith(
      "evaluation-1",
      expect.any(AbortSignal),
      expect.objectContaining({ cursor: undefined }),
    );
    const evaluationCalls = vi.mocked(tokenDiagnosisV2Api.evaluation).mock
      .calls;
    expect(evaluationCalls[evaluationCalls.length - 1]?.[2]).not.toHaveProperty(
      "instances_cursor",
    );
    const returnTo = new URL(
      link.getAttribute("href") || "",
      "https://console.invalid",
    );
    const deepParams = returnTo.searchParams;
    expect(deepParams.get("start")).toBe(metadata.window.start);
    expect(deepParams.get("end")).toBe(metadata.window.end);
    expect(deepParams.get("model_name")).toBe("model");
    expect(deepParams.get("agent_id")).toBe("agent");
    expect(deepParams.get("root_agent_id")).toBe("root");
    expect(deepParams.get("origin")).toBe("https://host.example");
    expect(deepParams.get("hideMenu")).toBe("true");
    for (const key of [
      "rule_id",
      "state",
      "cursor",
      "instances_cursor",
      "group_id",
      "finding_id",
    ])
      expect(deepParams.has(key)).toBe(false);
    const returnPath = deepParams.get("returnTo");
    expect(returnPath).toContain("model_name=model");
    expect(returnPath).toContain("cursor=issues-cursor");
    expect(returnPath).toContain("group_id=s%3Asignature-1");
    expect(returnPath).toContain("finding_id=finding-1");
    expect(returnPath).toContain("start=2026-10-12T00%3A00%3A00Z");
    expect(returnPath).toContain("end=2026-10-13T00%3A00%3A00Z");
    expect(returnPath).toContain("#evidence");
  });

  it("clears cursors and selected group when filters change", async () => {
    open(
      "?model_name=old&cursor=issues-cursor&group_id=group-1&instances_cursor=instance-cursor",
    );
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toContain("start="),
    );
    const model = await screen.findByLabelText("模型");
    fireEvent.change(model, { target: { value: "new" } });
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() => {
      const location = screen.getByTestId("location").textContent || "";
      expect(location).toContain("model_name=new");
      expect(location).not.toContain("cursor=");
      expect(location).not.toContain("group_id=");
      expect(location).not.toContain("instances_cursor=");
    });
  });

  it("renders evaluation bounds and recommendation safety details", async () => {
    open("?group_id=s:signature-1&finding_id=finding-1");
    expect(await screen.findByText("growth")).toBeInTheDocument();
    const region = await screen.findByRole("region", { name: "规则实例详情" });
    expect(within(region).getByText(/下界 80/)).toBeInTheDocument();
    expect(within(region).getByText("Confirm attribution")).toBeInTheDocument();
    expect(within(region).getByText("May lose evidence")).toBeInTheDocument();
    expect(
      within(region).queryByRole("button", { name: "自动优化" }),
    ).not.toBeInTheDocument();
  });

  it.each([
    ["stale", { is_current: false }],
    ["expired", { evidence_expired: true }],
  ])("blocks %s execution links", async (_name, override) => {
    vi.mocked(tokenDiagnosisV2Api.evaluation).mockResolvedValue({
      ...evaluation,
      ...override,
    } as EvaluationDTO);
    open("?group_id=s:signature-1&finding_id=finding-1");
    expect(
      await screen.findByText(/只读历史详情|历史规则结果/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /打开执行详情/ }),
    ).not.toBeInTheDocument();
  });

  it("rejects foreign or mismatched instances without rendering them", async () => {
    vi.mocked(tokenDiagnosisV2Api.instances).mockResolvedValue({
      ...instancePage,
      items: [{ ...finding, rule_id: "safe_tool_repeat" }],
    });
    open("?group_id=s:signature-1");
    expect(
      await screen.findByText("问题实例身份不匹配；拒绝绑定返回数据。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("instance-1")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /打开执行详情/ }),
    ).not.toBeInTheDocument();
  });

  it("rejects an explicit foreign group id even when other group fields match", async () => {
    vi.mocked(tokenDiagnosisV2Api.instances).mockResolvedValue({
      ...instancePage,
      items: [{ ...finding, group_id: "foreign-group" } as FindingDTO],
    });
    open("?group_id=s:signature-1");
    expect(
      await screen.findByText("问题实例身份不匹配；拒绝绑定返回数据。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("instance-1")).not.toBeInTheDocument();
  });

  it("accepts an ungrouped finding with no signature despite its raw reliable flag", async () => {
    const item = {
      ...finding,
      finding_id: "unreliable-1",
      signature: null,
      reliable_grouping: true,
    };
    vi.mocked(tokenDiagnosisV2Api.instances).mockResolvedValue({
      ...instancePage,
      group_id: unreliableGroup.group_id,
      items: [item],
    });
    open("?group_id=f:unreliable-1");
    expect(await screen.findByText("instance-1")).toBeInTheDocument();
    expect(
      screen.queryByText("问题实例身份不匹配；拒绝绑定返回数据。"),
    ).not.toBeInTheDocument();
  });

  it("rejects an instance page from a different window", async () => {
    vi.mocked(tokenDiagnosisV2Api.instances).mockResolvedValue({
      ...instancePage,
      window: { ...metadata.window, end: "2026-10-14T00:00:00Z" },
    });
    open("?group_id=s:signature-1");
    expect(
      await screen.findByText("问题实例身份不匹配；拒绝绑定返回数据。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("instance-1")).not.toBeInTheDocument();
  });

  it("rejects an evaluation from another scope or window", async () => {
    vi.mocked(tokenDiagnosisV2Api.evaluation).mockResolvedValue({
      ...evaluation,
      scope: { source_id: "foreign", tenant_id: "tenant" },
    });
    open("?group_id=s:signature-1&finding_id=finding-1");
    expect(
      await screen.findByText(
        "规则实例身份与当前问题实例不匹配；拒绝绑定外部或过期证据。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /打开执行详情/ }),
    ).not.toBeInTheDocument();
  });

  it("does not link while evaluation is unresolved", async () => {
    vi.mocked(tokenDiagnosisV2Api.evaluation).mockImplementation(
      () => new Promise(() => undefined),
    );
    open("?group_id=s:signature-1&finding_id=finding-1");
    expect(await screen.findByText("instance-1")).toBeInTheDocument();
    expect(
      screen.getByRole("status", { name: "加载规则实例" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /打开执行详情/ }),
    ).not.toBeInTheDocument();
  });

  it("does not link when evaluation request fails", async () => {
    vi.mocked(tokenDiagnosisV2Api.evaluation).mockRejectedValue(
      new Error("failed"),
    );
    open("?group_id=s:signature-1&finding_id=finding-1");
    expect(await screen.findByText("failed")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /打开执行详情/ }),
    ).not.toBeInTheDocument();
  });

  it.each(["pending", "failed"])(
    "does not link a %s finding",
    async (state) => {
      vi.mocked(tokenDiagnosisV2Api.instances).mockResolvedValue({
        ...instancePage,
        items: [
          {
            ...finding,
            evaluation_state: state as FindingDTO["evaluation_state"],
          },
        ],
      });
      open("?group_id=s:signature-1&finding_id=finding-1");
      expect(await screen.findByText("instance-1")).toBeInTheDocument();
      expect(
        screen.queryByRole("link", { name: /打开执行详情/ }),
      ).not.toBeInTheDocument();
    },
  );

  it("rejects an evaluation for a different rule", async () => {
    vi.mocked(tokenDiagnosisV2Api.evaluation).mockResolvedValue({
      ...evaluation,
      evaluation: { ...evaluation.evaluation, rule_id: "safe_tool_repeat" },
    });
    open("?group_id=s:signature-1&finding_id=finding-1");
    expect(
      await screen.findByText(
        "规则实例身份与当前问题实例不匹配；拒绝绑定外部或过期证据。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /打开执行详情/ }),
    ).not.toBeInTheDocument();
  });

  it("aborts an in-flight issues request on unmount", async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(tokenDiagnosisV2Api.issues).mockImplementation(
      (requestSignal) => {
        signal = requestSignal;
        return new Promise(() => undefined);
      },
    );
    const view = open();
    await waitFor(() => expect(signal).toBeDefined());
    view.unmount();
    expect(signal?.aborted).toBe(true);
  });
});
