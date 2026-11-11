import { beforeEach, expect, it, vi } from "vitest";
import { request } from "../request";
import { cronCalendarApi } from "./cronCalendar";

vi.mock("../request", () => ({ request: vi.fn() }));
beforeEach(() => vi.clearAllMocks());

it("日历预览传原规则与日历开关，不传浏览器时间或任务写入字段", async () => {
  vi.mocked(request).mockResolvedValue({ next_run_times: [] });
  const signal = new AbortController().signal;
  await cronCalendarApi.preview(
    {
      actor_id: "alice",
      tenant_id: "alice",
      source_id: "bank",
      agent_id: "default",
    },
    {
      type: "cron",
      cron: "0 9 LW * *",
      timezone: "Asia/Shanghai",
      run_on_non_workday: false,
    },
    signal,
  );
  expect(request).toHaveBeenCalledWith("/scheduler/cron/calendar/resolve", {
    method: "POST",
    signal,
    headers: {
      "X-User-Id": "alice",
      "X-Tenant-Id": "alice",
      "X-Source-Id": "bank",
      "X-Agent-Id": "default",
    },
    body: JSON.stringify({
      cron: "0 9 LW * *",
      timezone: "Asia/Shanghai",
      run_on_non_workday: false,
      count: 3,
    }),
  });
});
