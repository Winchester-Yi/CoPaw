import { beforeEach, expect, it, vi } from "vitest";
import { request } from "../request";
import { systemCronApi } from "./systemCron";

vi.mock("../request", () => ({ request: vi.fn() }));
beforeEach(() => vi.clearAllMocks());
const context = {
  actor_id: "alice",
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
};

it("系统状态查询沿用当前真实Header，不按source空维度伪造用户", async () => {
  vi.mocked(request).mockResolvedValue({ items: [] });
  await systemCronApi.jobs(context, "source");
  const [path, options] = vi.mocked(request).mock.calls[0];
  expect(path).toBe("/scheduler/cron/system/jobs?scope_kind=source");
  const headers = new Headers(options?.headers);
  expect(headers.get("X-User-Id")).toBe("alice");
  expect(headers.get("X-Tenant-Id")).toBe("alice");
  expect(headers.get("X-Source-Id")).toBe("bank");
  expect(headers.has("X-User-Role")).toBe(false);
});

it("重新提交配置只提交类型和请求号，不上传旧配置快照", async () => {
  vi.mocked(request).mockResolvedValue({ job_id: "system" });
  await systemCronApi.refresh(context, "dream", "save-1");
  const [path, options] = vi.mocked(request).mock.calls[0];
  expect(path).toBe("/scheduler/cron/system/jobs/refresh");
  expect(JSON.parse(String(options?.body))).toEqual({
    task_type: "dream",
    request_id: "save-1",
  });
});

it("已登记任务重试只使用管理行版本，不解除未知或自动重跑", async () => {
  vi.mocked(request).mockResolvedValue({ sync_status: "pending" });
  await systemCronApi.retry(
    context,
    { job_id: "system/1", version: 7 },
    "retry-1",
  );
  const [path, options] = vi.mocked(request).mock.calls[0];
  expect(path).toBe("/scheduler/cron/system/jobs/system%2F1/retry");
  expect(JSON.parse(String(options?.body))).toEqual({
    request_id: "retry-1",
    expected_version: 7,
  });
  expect(Object.keys(systemCronApi).sort()).toEqual([
    "jobs",
    "refresh",
    "retry",
  ]);
});
