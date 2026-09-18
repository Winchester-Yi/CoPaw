import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../request";
import {
  exportTaskReport,
  getTaskReportOptions,
  getTaskTypeReport,
} from "./taskTypeReport";
const auth = vi.hoisted(() => ({ bbk: "100" }));
vi.mock("../authHeaders", () => ({
  buildAuthHeaders: () => ({ "X-Bbk-Id": auth.bbk, "X-Source-Id": "RMASSIST" }),
}));
vi.mock("../config", () => ({ getApiUrl: (path: string) => `/api${path}` }));
vi.mock("../request", () => ({ request: vi.fn() }));
const base = {
  start_date: "2026-09-01",
  end_date: "2026-09-14",
  group_by: "manager",
  task_type: "push_plan",
} as const;
beforeEach(() => {
  vi.clearAllMocks();
  auth.bbk = "100";
});
afterEach(() => vi.unstubAllGlobals());
describe("task type report adapter", () => {
  it("rejects cross-month and reversed dates without requesting", async () => {
    for (const [start_date, end_date] of [
      ["2026-08-31", "2026-09-01"],
      ["2026-09-15", "2026-09-01"],
    ])
      await expect(
        getTaskTypeReport({ ...base, start_date, end_date }),
      ).rejects.toThrow("同一个月");
    expect(request).not.toHaveBeenCalled();
  });
  it("forwards the effective BBK header and forces local branch scope for report and options", async () => {
    auth.bbk = "110";
    const signal = new AbortController().signal;
    await getTaskTypeReport(
      { ...base, page: 2, page_size: 20, keyword: "张三" },
      signal,
    );
    const [url, options] = vi.mocked(request).mock.calls[0];
    const params = new URL(String(url), "http://localhost").searchParams;
    expect(params.get("first_bbk_id")).toBe("110");
    expect(params.get("page")).toBe("2");
    expect(params.get("keyword")).toBe("张三");
    expect(options).toEqual({ signal, headers: { "X-Bbk-Id": "110" } });
    await getTaskReportOptions({ end_date: base.end_date, kind: "orgs" });
    expect(request).toHaveBeenLastCalledWith(
      expect.stringContaining("first_bbk_id=110"),
      expect.objectContaining({ headers: { "X-Bbk-Id": "110" } }),
    );
    await expect(
      getTaskTypeReport({ ...base, first_bbk_id: "120" }),
    ).rejects.toThrow("当前分行");
    auth.bbk = "";
    await expect(getTaskTypeReport(base)).rejects.toThrow("缺少分行身份");
  });
  it("exports full filters and headers, strips pagination, and rejects non-Excel replies", async () => {
    auth.bbk = "110";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(["xlsx"]), {
        headers: {
          "Content-Type":
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const file = await exportTaskReport({
      ...base,
      page: 3,
      page_size: 20,
      skill_detail: true,
      user_id: "u1",
      org_id: "11001",
    });
    expect(file.size).toBeGreaterThan(0);
    const [url, options] = fetchMock.mock.calls[0];
    const params = new URL(url, "http://localhost").searchParams;
    expect(params.has("page")).toBe(false);
    expect(params.has("page_size")).toBe(false);
    expect(params.get("user_id")).toBe("u1");
    expect(params.get("skill_detail")).toBe("true");
    expect(options.headers["X-Bbk-Id"]).toBe("110");
    fetchMock.mockResolvedValue(
      new Response("bad", { headers: { "Content-Type": "text/plain" } }),
    );
    await expect(exportTaskReport(base)).rejects.toThrow("Excel");
  });
});
