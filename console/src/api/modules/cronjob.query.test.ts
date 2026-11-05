import { beforeEach, expect, it, vi } from "vitest";
import { request } from "../request";
import { cronJobApi } from "./cronjob";

vi.mock("../request", () => ({ request: vi.fn() }));
beforeEach(() => vi.clearAllMocks());

it("保留旧无参查询，新增捕获 context 的只读调用", async () => {
  const context = {
    actor_id: "actor",
    tenant_id: "tenant",
    source_id: "source",
    agent_id: "agent",
  };
  const controller = new AbortController();
  await cronJobApi.listCronJobs();
  expect(request).toHaveBeenLastCalledWith("/cron/jobs");
  await cronJobApi.listCronJobsForContext(context, controller.signal);
  const [path, options] = vi.mocked(request).mock.calls[1];
  expect(path).toBe("/cron/jobs");
  expect(options?.signal).toBe(controller.signal);
  expect(new Headers(options?.headers).get("X-Tenant-Id")).toBe("tenant");
  expect(new Headers(options?.headers).get("X-User-Id")).toBe("actor");
});
