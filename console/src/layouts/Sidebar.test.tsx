import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import Sidebar from "./Sidebar";

const iframeState = vi.hoisted(() => ({
  isSuperManager: false,
  manager: false,
  hideChat: false,
  source: "RMASSIST" as string | null,
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

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string | { defaultValue?: string }) =>
      typeof fallback === "string" ? fallback : fallback?.defaultValue || key,
  }),
}));

vi.mock("../api/modules/auth", () => ({
  authApi: {
    getStatus: vi.fn().mockResolvedValue({ enabled: false }),
  },
}));

vi.mock("../contexts/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

vi.mock("../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: {} }),
}));

vi.mock("../stores/iframeStore", () => ({
  useIframeStore: (selector: (state: typeof iframeState) => unknown) =>
    selector(iframeState),
}));

afterEach(() => {
  cleanup();
  iframeState.source = "RMASSIST";
});

describe("Sidebar skill config visibility", () => {
  it("shows the skill config menu for the RMASSIST source", () => {
    render(
      <MemoryRouter>
        <Sidebar selectedKey="skill-config" />
      </MemoryRouter>,
    );

    expect(screen.getByText("Skill 配置")).toBeInTheDocument();
  });

  it("hides the skill config menu for other sources", () => {
    iframeState.source = "OTHER";

    render(
      <MemoryRouter>
        <Sidebar selectedKey="models" />
      </MemoryRouter>,
    );

    expect(screen.queryByText("Skill 配置")).not.toBeInTheDocument();
  });
});

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname + location.search}
    </output>
  );
}

describe("Sidebar Claw report navigation", () => {
  it("places the Claw dashboard after operations and navigates to its route", () => {
    iframeState.source = "OTHER";
    render(
      <MemoryRouter>
        <Sidebar selectedKey="analytics-business-overview" />
        <LocationProbe />
      </MemoryRouter>,
    );

    const operations = screen.getByText("运营看板");
    const claw = screen.getByText("Claw技能运行看板");
    expect(
      operations.compareDocumentPosition(claw) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.click(claw);
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/analytics/claw-data-overview",
    );
    expect(screen.getAllByText("Claw技能运行看板")).toHaveLength(1);
  });
});

describe("Token navigation in collapsed sidebar", () => {
  it("names the three icon buttons and drops detail locators while retaining host and window filters", () => {
    const search = new URLSearchParams({
      hideMenu: "true",
      source: "host",
      userId: "user",
      start: "2026-10-12T00:00:00Z",
      end: "2026-10-13T00:00:00Z",
      agent_id: "executor",
      model_name: "model",
      call_id: "old-call",
      context_id: "old-context",
      evaluation_id: "old-evaluation",
      operation_id: "old-operation",
      step_id: "old-step",
      group_id: "old-group",
      finding_id: "old-finding",
      cursor: "old-cursor",
      relation_cursor: "old-relation",
      branch_run_id: "old-branch",
    });
    const { container } = render(
      <MemoryRouter
        initialEntries={[`/analytics/token-executions/task?${search}`]}
      >
        <Sidebar selectedKey="analytics-token-executions" />
        <LocationProbe />
      </MemoryRouter>,
    );
    const collapse = container.querySelector('button[class*="collapseToggle"]');
    expect(collapse).not.toBeNull();
    fireEvent.click(collapse!);
    expect(
      screen.getByRole("button", { name: "Token 总览" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Token 执行" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Token 问题" }));
    const url = new URL(
      screen.getByTestId("location").textContent!,
      "https://console.invalid",
    );
    expect(url.pathname).toBe("/analytics/token-issues");
    for (const key of [
      "hideMenu",
      "source",
      "userId",
      "start",
      "end",
      "agent_id",
      "model_name",
    ])
      expect(url.searchParams.get(key)).toBe(search.get(key));
    for (const key of [
      "call_id",
      "context_id",
      "evaluation_id",
      "operation_id",
      "step_id",
      "group_id",
      "finding_id",
      "cursor",
      "relation_cursor",
      "branch_run_id",
    ])
      expect(url.searchParams.has(key)).toBe(false);
  });
});
