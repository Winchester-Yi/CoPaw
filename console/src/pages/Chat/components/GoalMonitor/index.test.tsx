import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GoalMonitor from "./index";

const mocks = vi.hoisted(() => ({
  getRecentGoal: vi.fn(),
  pauseGoal: vi.fn(),
  resumeGoal: vi.fn(),
  editGoal: vi.fn(),
}));
vi.mock("../../../../api/modules/chat", () => ({
  chatApi: {
    getRecentGoal: mocks.getRecentGoal,
    pauseGoal: mocks.pauseGoal,
    resumeGoal: mocks.resumeGoal,
    editGoal: mocks.editGoal,
  },
}));
vi.mock("../../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: { error: vi.fn() } }),
}));

const goal = {
  goal_id: "goal-1",
  state: "ACTIVE",
  revision: 1,
  turn_budget: 12,
  budget_cycle: 1,
  turns_used: 2,
  next_focus: "Implement monitor",
  state_reason: null,
  scope: {},
  contract: {
    objective: "Ship Goal Runtime",
    completion_criteria: [],
    constraints: { must_preserve: [], must_not_do: [] },
    autonomy_boundary: "No deployment",
  },
  criteria: [
    {
      criterion_id: "criterion-1",
      verified: true,
      consecutive_failures: 0,
      evidence_refs: [],
      criterion: {
        requirement: "API exists",
        observable_assertion: "route",
        verification_method: "OpenAPI",
        expected_outcome: "listed",
      },
    },
  ],
  control_commands: [],
  created_at: "",
  updated_at: "",
};

describe("GoalMonitor", () => {
  afterEach(cleanup);
  beforeEach(() => vi.clearAllMocks());

  it("shows durable delegation authorization and refreshes after steering", async () => {
    mocks.getRecentGoal.mockResolvedValue({
      ...goal,
      subagent_delegation_enabled: false,
    });
    render(<GoalMonitor chatId="chat-1" />);
    await waitFor(() => screen.getByRole("button", { name: "目标运行状态" }));
    fireEvent.click(screen.getByRole("button", { name: "目标运行状态" }));
    expect(screen.getByText("子代理委派：未授权")).toBeInTheDocument();
    mocks.getRecentGoal.mockResolvedValue({
      ...goal,
      subagent_delegation_enabled: true,
    });
    fireEvent(window, new Event("goal-authorization-changed"));
    await waitFor(() =>
      expect(screen.getByText("子代理委派：已授权")).toBeInTheDocument(),
    );
  });

  it("uses a compact trigger and expands the Goal summary", async () => {
    mocks.getRecentGoal.mockResolvedValue(goal);
    render(<GoalMonitor chatId="chat-1" />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "目标运行状态" }),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "目标运行状态" }));
    expect(screen.getByText("Ship Goal Runtime")).toBeInTheDocument();
    expect(screen.getByText("执行中")).toBeInTheDocument();
    expect(screen.getByText("已通过 1 / 1 项条件")).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "Goal 完成条件审查进度" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/执行轮次/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "暂停目标" }));
    await waitFor(() =>
      expect(mocks.pauseGoal).toHaveBeenCalledWith("goal-1", "chat-1"),
    );
  });

  it("uses the same resume hand-off as the SubAgent monitor entry", async () => {
    const onResume = vi.fn();
    mocks.getRecentGoal.mockResolvedValue({ ...goal, state: "PAUSED" });
    mocks.resumeGoal.mockResolvedValue({ ...goal, state: "ACTIVE" });
    render(<GoalMonitor chatId="chat-1" onResume={onResume} />);
    await waitFor(() => screen.getByRole("button", { name: "目标运行状态" }));
    fireEvent.click(screen.getByRole("button", { name: "目标运行状态" }));
    fireEvent.click(screen.getByRole("button", { name: "恢复目标" }));
    await waitFor(() => expect(onResume).toHaveBeenCalledWith("goal-1"));
  });

  it("submits a complete direct Contract edit", async () => {
    mocks.getRecentGoal.mockResolvedValue(goal);
    mocks.editGoal.mockResolvedValue({ ...goal, revision: 2 });
    render(<GoalMonitor chatId="chat-1" />);
    await waitFor(() => screen.getByRole("button", { name: "目标运行状态" }));
    fireEvent.click(screen.getByRole("button", { name: "目标运行状态" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑目标合同" }));
    fireEvent.change(screen.getByLabelText("目标"), {
      target: { value: "Edited Goal" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交合同编辑" }));
    await waitFor(() =>
      expect(mocks.editGoal).toHaveBeenCalledWith(
        "goal-1",
        "chat-1",
        expect.objectContaining({ objective: "Edited Goal" }),
      ),
    );
  });
});
