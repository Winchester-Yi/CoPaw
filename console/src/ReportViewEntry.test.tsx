import type { ReactNode } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ReportViewEntry from "./ReportViewEntry";

const mocks = vi.hoisted(() => ({
  getStatus: vi.fn(),
  getToken: vi.fn(),
  initialize: vi.fn(),
  clearToken: vi.fn(),
}));

vi.mock("./api/modules/auth", () => ({
  authApi: { getStatus: mocks.getStatus },
}));
vi.mock("./api/config", () => ({
  getApiToken: mocks.getToken,
  getApiUrl: (path: string) => `/api${path}`,
  clearAuthToken: mocks.clearToken,
}));
vi.mock("./api/authHeaders", () => ({
  buildAuthHeaders: () => ({ "X-User-Id": "report-user" }),
}));
vi.mock("./components/agentscope-chat/DynamicRenderContext", () => ({
  DynamicRenderProvider: ({ children }: { children: ReactNode }) => (
    <div data-testid="template-provider">{children}</div>
  ),
  useDynamicRender: () => ({ initialize: mocks.initialize }),
}));
vi.mock("./pages/ReportView", () => ({
  default: () => <div>报告内容</div>,
}));

function renderEntry() {
  return render(
    <MemoryRouter initialEntries={["/reportView?templateId=1&resultId=2"]}>
      <Routes>
        <Route path="/reportView" element={<ReportViewEntry />} />
        <Route path="/login" element={<div>登录页</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getStatus.mockResolvedValue({ enabled: false });
  mocks.getToken.mockReturnValue(null);
  mocks.initialize.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ReportView standalone entry", () => {
  it("provides and initializes templates without preloading all contents", async () => {
    renderEntry();

    expect(await screen.findByText("报告内容")).toBeInTheDocument();
    expect(screen.getByTestId("template-provider")).toContainElement(
      screen.getByText("报告内容"),
    );
    expect(mocks.initialize).toHaveBeenCalledWith({ skipPreload: true });
  });

  it("keeps the report behind authentication before initializing templates", async () => {
    mocks.getStatus.mockResolvedValue({ enabled: true });
    renderEntry();

    expect(await screen.findByText("登录页")).toBeInTheDocument();
    expect(screen.queryByText("报告内容")).not.toBeInTheDocument();
    expect(mocks.initialize).not.toHaveBeenCalled();
  });

  it("verifies a stored token with the existing identity headers", async () => {
    mocks.getStatus.mockResolvedValue({ enabled: true });
    mocks.getToken.mockReturnValue("report-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
    renderEntry();

    expect(await screen.findByText("报告内容")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/verify", {
      headers: {
        Authorization: "Bearer report-token",
        "X-User-Id": "report-user",
      },
    });
  });

  it("clears a rejected token and redirects to login", async () => {
    mocks.getStatus.mockResolvedValue({ enabled: true });
    mocks.getToken.mockReturnValue("expired-token");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    renderEntry();

    expect(await screen.findByText("登录页")).toBeInTheDocument();
    await waitFor(() => expect(mocks.clearToken).toHaveBeenCalledOnce());
    expect(mocks.initialize).not.toHaveBeenCalled();
  });
});
