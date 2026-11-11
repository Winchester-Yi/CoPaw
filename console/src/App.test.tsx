import type { ReactNode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const loads = vi.hoisted(() => ({
  console: vi.fn(),
  wealth: vi.fn(),
  report: vi.fn(),
}));

vi.mock("./ReportViewEntry", () => {
  loads.report();
  return { default: () => <div>经营方案预览</div> };
});

vi.mock("./ConsoleRoutes", () => {
  loads.console();
  return { default: () => <div>Console 页面</div> };
});
vi.mock("./pages/WealthWorkbench", () => {
  loads.wealth();
  return { default: () => <div>智拓工作台</div> };
});
vi.mock("antd-style", () => ({ createGlobalStyle: () => () => null }));
vi.mock("@agentscope-ai/design", () => ({
  ConfigProvider: ({ children }: { children: ReactNode }) => children,
  bailianTheme: {},
}));
vi.mock("antd", () => ({
  App: ({ children }: { children: ReactNode }) => children,
  theme: { defaultAlgorithm: {}, darkAlgorithm: {} },
}));
vi.mock("./contexts/ThemeContext", () => ({
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
  useTheme: () => ({ isDark: false }),
}));
vi.mock("./contexts/BrandThemeContext", () => ({
  BrandThemeProvider: ({ children }: { children: ReactNode }) => children,
  useBrandTheme: () => ({ theme: { primaryColor: "#3769FC" } }),
}));

afterEach(cleanup);

describe("platform loading boundary", () => {
  it.each([
    "/wealth/board",
    "/console/wealth/today?hideLogo=true&hideMenu=true",
  ])("opens %s without importing Console pages", async (path) => {
    window.history.replaceState(null, "", path);
    render(<App />);

    expect(await screen.findByText("智拓工作台")).toBeInTheDocument();
    expect(loads.wealth).toHaveBeenCalledTimes(1);
    expect(loads.console).not.toHaveBeenCalled();
  });

  it.each([
    "/reportView?templateId=1&resultId=report-1",
    "/console/reportView?hideMenu=true&needReportHtml=Y",
  ])("opens %s without importing Console pages", async (path) => {
    window.history.replaceState(null, "", path);
    render(<App />);

    expect(await screen.findByText("经营方案预览")).toBeInTheDocument();
    expect(loads.report).toHaveBeenCalledTimes(1);
    expect(loads.console).not.toHaveBeenCalled();
  });

  it("loads Console pages when a Console route is visited", async () => {
    window.history.replaceState(null, "", "/console/chat?hideMenu=true");
    render(<App />);

    expect(await screen.findByText("Console 页面")).toBeInTheDocument();
    expect(loads.console).toHaveBeenCalledTimes(1);
  });
});
