import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { useAgentStore } from "../../stores/agentStore";
import { useIframeStore } from "../../stores/iframeStore";
import Page from ".";
import { buildAuthHeaders } from "../../api/authHeaders";

const response = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useIframeStore.getState().clearContext();
  useAgentStore.getState().setSelectedAgent("default");
});

it("sends the selected agent and ignores the previous agent's late list", async () => {
  vi.stubGlobal("__env__", { baseUrl: "http://localhost" });
  useIframeStore
    .getState()
    .setContext({ source: "source-a", userId: "user-a", manager: true });
  useAgentStore.getState().setSelectedAgent("agent-a");
  expect(buildAuthHeaders()["X-Agent-Id"]).toBe("agent-a");
  let resolveOld!: (response: Response) => void;
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const agent = new Headers(init?.headers).get("X-Agent-Id");
    if (url.includes("/capabilities"))
      return Promise.resolve(response({ capture_enabled: true }));
    if (url.includes("/runs?")) {
      if (agent === "agent-a")
        return new Promise<Response>((resolve) => {
          resolveOld = resolve;
        });
      return Promise.resolve(response({ items: [], next_cursor: null }));
    }
    return Promise.resolve(response({}));
  });
  vi.stubGlobal("fetch", fetchMock);
  render(
    <MemoryRouter initialEntries={["/agent-observability/runs?cursor=old"]}>
      <Routes>
        <Route path="/agent-observability/runs" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.map(([u, o]) => [
        u,
        new Headers(o?.headers).get("X-Agent-Id"),
      ]),
    ).toContainEqual([expect.stringContaining("/runs?"), "agent-a"]),
  );
  act(() => useAgentStore.getState().setSelectedAgent("agent-b"));
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(
        ([url, options]) =>
          String(url).includes("/runs?") &&
          new Headers(options?.headers).get("X-Agent-Id") === "agent-b",
      ),
    ).toBe(true),
  );
  const calls = fetchMock.mock.calls.filter(([url]) =>
    String(url).includes("/runs?"),
  );
  expect(new Headers(calls[0][1]?.headers).get("X-Agent-Id")).toBe("agent-a");
  expect(String(calls[calls.length - 1][0])).not.toContain("cursor=old");
  await act(async () =>
    resolveOld(
      response({
        items: [{ run_id: "old-agent-run", status: "succeeded" }],
        next_cursor: null,
      }),
    ),
  );
  expect(
    await screen.findByText(/当前时间范围内暂无执行记录/),
  ).toBeInTheDocument();
  expect(screen.queryByText("old-agent-run")).not.toBeInTheDocument();
});

it.each([
  ["/agent-observability/runs", "/agent-observability/runs?"],
  ["/agent-observability/runs/run-1", "/agent-observability/runs/run-1"],
])("sends admin and current Source headers for %s", async (path, endpoint) => {
  vi.stubGlobal("__env__", { baseUrl: "http://localhost" });
  useIframeStore.getState().setContext({
    source: "source-admin",
    userId: "tenant-admin",
    manager: false,
    isSuperManager: true,
  });
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    expect(init?.headers).toBeDefined();
    const url = String(input);
    if (url.includes("/capabilities"))
      return Promise.resolve(response({ capture_enabled: true }));
    if (url.includes("/runs/run-1"))
      return Promise.resolve(
        response({
          run: { run_id: "run-1", status: "succeeded" },
          attempts: [],
          spans: [],
        }),
      );
    return Promise.resolve(response({ items: [], next_cursor: null }));
  });
  vi.stubGlobal("fetch", fetchMock);
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/agent-observability/runs" element={<Page />} />
        <Route path="/agent-observability/runs/:runId" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes(endpoint)),
    ).toBe(true),
  );
  const query = fetchMock.mock.calls.find(([url]) =>
    String(url).includes(endpoint),
  )!;
  const headers = new Headers(query[1]?.headers);
  expect(headers.get("X-User-Role")).toBe("admin");
  expect(headers.get("X-Source-Id")).toBe("source-admin");
  expect(headers.get("X-Tenant-Id")).toBe("tenant-admin");
  expect(new URL(String(query[0])).searchParams.has("source_id")).toBe(false);
  expect(
    await screen.findByText("当前 Source：source-admin"),
  ).toBeInTheDocument();
});

it.each(["/agent-observability/runs", "/agent-observability/runs/run-1"])(
  "denies an unauthorized role on %s without querying",
  async (path) => {
    vi.stubGlobal("__env__", { baseUrl: "http://localhost" });
    useIframeStore.getState().setContext({
      source: "source-a",
      userId: "user-a",
      manager: false,
      isSuperManager: false,
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/agent-observability/runs" element={<Page />} />
          <Route path="/agent-observability/runs/:runId" element={<Page />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(
      await screen.findByText("无权访问 Agent 可观测"),
    ).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  },
);
