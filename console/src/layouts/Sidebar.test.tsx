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
  return <output data-testid="location">{useLocation().pathname}</output>;
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
