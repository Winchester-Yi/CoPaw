import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { prepareShareMessages } from "./shareView";

vi.mock("react-router-dom", () => ({
  useParams: () => ({ token: "token-1" }),
}));

vi.mock("@/api/modules/chat", () => ({
  chatApi: {
    getChatShare: vi.fn().mockResolvedValue({
      chat_name: "测试会话",
      messages: [{ id: "message-1", role: "user", content: "你好" }],
    }),
  },
}));

vi.mock("antd", () => ({
  Alert: ({ message }: { message: string }) => <div>{message}</div>,
  Empty: ({ description }: { description: string }) => <div>{description}</div>,
  Spin: () => <div>loading</div>,
}));

vi.mock("@/components/agentscope-chat", () => ({
  Bubble: {
    List: ({ classNames }: { classNames?: { wrapper?: string } }) => (
      <div className={classNames?.wrapper} data-testid="share-bubble-list" />
    ),
  },
  AgentScopeRuntimeWebUIComposedProvider: ({
    children,
  }: {
    children: React.ReactNode;
  }) => <>{children}</>,
  HtmlPreviewTrackingProvider: ({
    children,
  }: {
    children: React.ReactNode;
  }) => <>{children}</>,
}));

vi.mock("../Chat/sessionApi", () => ({
  convertMessages: (messages: unknown[]) => messages,
}));

vi.mock("../Chat/components/RuntimeRequestCard", () => ({
  default: () => null,
}));
vi.mock(
  "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Card",
  () => ({ default: () => null }),
);

describe("ChatSharePage message preparation", () => {
  it("keeps structured cards and maps unknown cards to a read-only view", () => {
    const [message] = prepareShareMessages([
      {
        id: "turn-1",
        role: "assistant",
        cards: [
          { code: "AgentScopeRuntimeResponseCard", data: {} },
          { code: "ApprovalAction", data: { requestId: "approval-1" } },
          { code: "PlanInteraction", data: { planId: "plan-1" } },
          { code: "TaskRunGroupCard", data: { runId: "run-1" } },
          { code: "ResponseFeedback", data: { responseId: "response-1" } },
          { code: "UnknownCard", data: { value: "kept" } },
        ],
      },
    ] as never);
    expect(message.cards?.map((card) => card.code)).toEqual([
      "AgentScopeRuntimeResponseCard",
      "ApprovalAction",
      "PlanInteraction",
      "TaskRunGroupCard",
      "ResponseFeedback",
      "ReadOnlyStructuredCard",
    ]);
    expect(message.cards?.[message.cards.length - 1]?.data).toEqual({
      code: "UnknownCard",
      data: { value: "kept" },
    });
  });

  it("renders a bounded message viewport for long read-only conversations", async () => {
    const { default: ChatSharePage } = await import(".");
    render(<ChatSharePage />);

    await waitFor(() => {
      expect(screen.getByTestId("share-bubble-list")).toBeInTheDocument();
    });
    expect(screen.getByTestId("share-message-viewport")).toBeInTheDocument();
  });

  it("renders every Goal contract field in the read-only card", async () => {
    const { ReadOnlyGoalProposalCard } = await import(".");
    render(
      <ReadOnlyGoalProposalCard
        data={{
          card_type: "goal_proposal",
          objective: "完成可审计的分享页",
          completion_criteria: [
            {
              requirement: "页面可滚动",
              observable_assertion: "滚动容器高度小于内容高度",
              verification_method: "浏览器检查 scrollHeight",
              expected_outcome: "所有消息均可访问",
            },
          ],
          constraints: {
            must_preserve: ["原会话视觉结构"],
            must_not_do: ["启用交互操作"],
          },
          autonomy_boundary: "仅允许只读展示",
        }}
      />,
    );

    expect(screen.getByText("页面可滚动")).toBeInTheDocument();
    expect(screen.getByText(/可观察断言：/)).toHaveTextContent(
      "滚动容器高度小于内容高度",
    );
    expect(screen.getByText(/验证方式：/)).toHaveTextContent(
      "浏览器检查 scrollHeight",
    );
    expect(screen.getByText(/预期结果：/)).toHaveTextContent(
      "所有消息均可访问",
    );
    expect(screen.getByText("原会话视觉结构")).toBeInTheDocument();
    expect(screen.getByText("启用交互操作")).toBeInTheDocument();
    expect(screen.getByTestId("readonly-goal-proposal-card")).toHaveAttribute(
      "data-scrollable",
      "true",
    );
  });

  it("keeps structured card payloads visible without enabling interactions", async () => {
    const { ReadOnlyStructuredCard } = await import(".");
    render(
      <ReadOnlyStructuredCard
        code="审批记录"
        data={{ requestId: "approval-1", toolName: "shell", status: "pending" }}
      />,
    );

    expect(screen.getByText("审批记录")).toBeInTheDocument();
    expect(
      screen.getByTestId("readonly-structured-card-payload"),
    ).toHaveTextContent('"requestId": "approval-1"');
  });
});
