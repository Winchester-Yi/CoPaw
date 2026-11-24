import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import MainLayout from ".";

const iframeState = vi.hoisted(() => ({
  hideMenu: false,
  hideChat: false,
  source: "ruice" as string | null,
}));

vi.mock(
  "@agentscope-ai/icons",
  () =>
    new Proxy(
      {},
      {
        get: (_, property) => (property === "then" ? undefined : () => null),
        has: () => true,
      },
    ),
);
vi.mock("../../pages/Chat", () => ({ default: () => null }));
vi.mock("../../pages/AgentObservability", () => ({
  default: () => <div>可观测路由</div>,
}));

vi.mock("../../stores/iframeStore", () => ({
  useIframeStore: Object.assign(
    (selector: (state: typeof iframeState) => unknown) => selector(iframeState),
    { subscribe: vi.fn(), getState: () => iframeState },
  ),
  getIframeContext: () => iframeState,
}));

vi.mock("../../stores/chatPresentationStore", () => ({
  useChatPresentationStore: (
    selector: (state: { showContentOnly: boolean }) => unknown,
  ) => selector({ showContentOnly: false }),
}));

vi.mock("../../stores/sourceSystemConfigStore", () => ({
  useSourceSystemConfigStore: (
    selector: (state: { loadEffectiveConfig: () => void }) => unknown,
  ) => selector({ loadEffectiveConfig: vi.fn() }),
}));

vi.mock("@/components/agentscope-chat/DynamicRenderContext", () => ({
  useDynamicRender: () => ({ initialize: vi.fn() }),
}));

vi.mock("../Header", () => ({
  default: () => <header data-testid="global-header" />,
}));

vi.mock("../Sidebar", () => ({
  default: ({
    withoutHeader,
    selectedKey,
  }: {
    withoutHeader?: boolean;
    selectedKey: string;
  }) => (
    <aside
      data-testid="global-sidebar"
      data-without-header={String(Boolean(withoutHeader))}
      data-selected-key={selectedKey}
    />
  ),
}));

vi.mock("../../components/ConsoleCronBubble", () => ({
  default: () => null,
}));

afterEach(() => {
  cleanup();
  iframeState.hideMenu = false;
  iframeState.hideChat = false;
  iframeState.source = "ruice";
});

describe("MainLayout global shell", () => {
  it.each(["/agent-observability/runs", "/agent-observability/runs/run-id"])(
    "registers the observability route %s",
    (path) => {
      render(
        <MemoryRouter initialEntries={[path]}>
          <MainLayout />
        </MemoryRouter>,
      );
      expect(screen.getByText("可观测路由")).toBeInTheDocument();
      expect(screen.getByTestId("global-sidebar")).toHaveAttribute(
        "data-selected-key",
        "agent-observability",
      );
    },
  );
  it("hides only the Header when source is ruice", () => {
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <MainLayout />
      </MemoryRouter>,
    );

    expect(screen.queryByTestId("global-header")).not.toBeInTheDocument();
    expect(screen.getByTestId("global-sidebar")).toHaveAttribute(
      "data-without-header",
      "true",
    );
  });

  it("renders Header and Sidebar for other sources", () => {
    iframeState.source = "other";

    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <MainLayout />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("global-header")).toBeInTheDocument();
    expect(screen.getByTestId("global-sidebar")).toHaveAttribute(
      "data-without-header",
      "false",
    );
  });
});
