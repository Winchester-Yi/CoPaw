import { expect, it, vi, beforeEach } from "vitest";
import { request } from "../request";
import { loadRuntimeStatuses } from "./cronRuntime";
vi.mock("../request", () => ({ request: vi.fn() }));
const context = {
  actor_id: "u",
  tenant_id: "u",
  source_id: "s",
  agent_id: "a",
};
beforeEach(() => vi.resetAllMocks());
it.each([
  null,
  { id: "a", status: "ok", enabled: true, state: {}, task: {} },
  { id: "a", status: "wat" },
])("rejects malformed result %s", async (item) => {
  vi.mocked(request).mockResolvedValue({ items: [item] });
  await expect(loadRuntimeStatuses("legacy", context, ["a"])).rejects.toThrow();
});
it("aborting after first response prevents next batch", async () => {
  const controller = new AbortController();
  vi.mocked(request).mockImplementation(async () => {
    controller.abort();
    return { items: [] };
  });
  await expect(
    loadRuntimeStatuses(
      "legacy",
      context,
      Array.from({ length: 201 }, (_, i) => String(i)),
      controller.signal,
    ),
  ).rejects.toMatchObject({ name: "AbortError" });
  expect(request).toHaveBeenCalledTimes(1);
});
it.each([1, 199, 200, 201, 401])(
  "%i IDs use bounded batches",
  async (count) => {
    vi.mocked(request).mockImplementation(
      async (_path, options) =>
        ({
          items: JSON.parse(options!.body as string).job_ids.map(
            (id: string) => ({ id, status: "unavailable" }),
          ),
        }) as never,
    );
    expect(
      await loadRuntimeStatuses(
        "legacy",
        context,
        Array.from({ length: count }, (_, i) => String(i)),
      ),
    ).toHaveLength(count);
    expect(request).toHaveBeenCalledTimes(Math.ceil(count / 200));
  },
);
it("second batch failure discards the whole source", async () => {
  vi.mocked(request)
    .mockImplementationOnce(
      async (_path, options) =>
        ({
          items: JSON.parse(options!.body as string).job_ids.map(
            (id: string) => ({ id, status: "unavailable" }),
          ),
        }) as never,
    )
    .mockRejectedValueOnce(new Error("offline"));
  await expect(
    loadRuntimeStatuses(
      "legacy",
      context,
      Array.from({ length: 201 }, (_, i) => String(i)),
    ),
  ).rejects.toThrow("offline");
});

function runtimeItem(preview: string) {
  return {
    id: "a",
    status: "ok",
    enabled: true,
    state: {
      last_status: "success",
      last_run_at: null,
      last_error: null,
      last_error_truncated: false,
      next_run_at: null,
      next_run_times: [],
    },
    task: {
      visible_in_my_tasks: true,
      chat_id: "chat-a",
      session_id: "session-a",
      is_running: false,
      is_paused: false,
      pause_reason: null,
      auto_paused_at: null,
      has_scheduled_result: true,
      latest_scheduled_preview: preview,
      latest_scheduled_preview_truncated: false,
      last_scheduled_run_at: null,
      unread_execution_count: 1,
    },
  };
}
it("accepts the backend Unicode character limit for a complete runtime item", async () => {
  const item = runtimeItem("😀".repeat(256));
  vi.mocked(request).mockResolvedValue({ items: [item] });
  await expect(loadRuntimeStatuses("legacy", context, ["a"])).resolves.toEqual([
    item,
  ]);
});
it("rejects previews beyond the Unicode character limit", async () => {
  vi.mocked(request).mockResolvedValue({
    items: [runtimeItem("😀".repeat(257))],
  });
  await expect(loadRuntimeStatuses("legacy", context, ["a"])).rejects.toThrow(
    "invalid_task_runtime_item",
  );
});
