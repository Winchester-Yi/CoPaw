import { expect, it, vi } from "vitest";
import { loadRuntimeStatuses } from "./cronRuntime";
import { request } from "../request";
vi.mock("../request", () => ({ request: vi.fn() }));
const context = {
  actor_id: "u",
  tenant_id: "u",
  source_id: "s",
  agent_id: "a",
};
it("201 IDs use sequential bounded requests and explicit context", async () => {
  vi.mocked(request).mockImplementation(
    async (_path, options) =>
      ({
        items: JSON.parse(options!.body as string).job_ids.map(
          (id: string) => ({ id, status: "unavailable" }),
        ),
      }) as never,
  );
  const result = await loadRuntimeStatuses(
    "legacy",
    context,
    Array.from({ length: 201 }, (_, i) => `j${i}`),
  );
  expect(result).toHaveLength(201);
  expect(request).toHaveBeenCalledTimes(2);
  expect(request).toHaveBeenCalledWith(
    "/cron/jobs/-/runtime-statuses",
    expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ "X-User-Id": "u" }),
    }),
  );
});
it("partial or duplicate response fails the source", async () => {
  vi.mocked(request).mockResolvedValue({ items: [] });
  await expect(
    loadRuntimeStatuses("scheduler", context, ["a"]),
  ).rejects.toThrow();
});
it("no IDs starts no HTTP", async () => {
  vi.mocked(request).mockClear();
  expect(await loadRuntimeStatuses("legacy", context, [])).toEqual([]);
  expect(request).not.toHaveBeenCalled();
});
