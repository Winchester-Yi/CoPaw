import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ChatTaskSourceStore } from "./chatTaskSourceStore";
import { tagTask } from "@/api/modules/cronTaskSources";
import type { SourcedCronTask } from "@/api/types/cronTaskSource";

const context = {
  actor_id: "u",
  tenant_id: "u",
  source_id: "s",
  agent_id: "a",
};
const job = (id: string, owner: "legacy" | "scheduler" = "legacy") =>
  tagTask(
    { id, name: id, task: { visible_in_my_tasks: true } } as never,
    owner,
    context,
    "a",
  );
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};
const loader = vi.fn();
let store: ChatTaskSourceStore;
beforeEach(() => {
  loader.mockReset().mockResolvedValue([]);
  store = new ChatTaskSourceStore(loader);
  store.activate(context, "a");
});

afterEach(() => store.reset());

it("polls displayed and open tasks only, preserving full membership", async () => {
  const runtime = vi.fn().mockImplementation(async (_owner, _context, ids) =>
    ids.map((id: string) => ({
      id,
      status: "ok",
      enabled: true,
      task: { visible_in_my_tasks: id === "mine", chat_id: `chat-${id}` },
    })),
  );
  store = new ChatTaskSourceStore(loader, runtime);
  store.activate(context, "a");
  const hidden = (id: string) =>
    ({
      ...job(id),
      task: { visible_in_my_tasks: false, chat_id: `chat-${id}` },
    }) as SourcedCronTask;
  loader.mockResolvedValue([job("mine"), hidden("open"), hidden("other")]);
  await store.refresh("legacy");
  const other = store.getSnapshot().legacy.items[2];
  const release = store.retainOpenChat(
    "chat-open",
    "a",
    store.getSnapshot().epoch,
  );
  await store.refresh("legacy", "poll");
  expect(runtime.mock.calls[0][2]).toEqual(["mine", "open"]);
  expect([...store.presence("a").legacy!]).toEqual(["mine", "open", "other"]);
  expect(store.getSnapshot().legacy.items[2]).toBe(other);
  expect(store.getSnapshot().legacy.error).toBeNull();
  expect(loader).toHaveBeenCalledTimes(1);
  release();
  runtime.mockClear();
  await store.refresh("legacy", "poll");
  expect(runtime.mock.calls[0][2]).toEqual(["mine"]);
});

it("合并同一来源在途读取，另一来源独立发布", async () => {
  const slow = deferred<SourcedCronTask[]>();
  loader.mockImplementation((owner) =>
    owner === "legacy"
      ? slow.promise
      : Promise.resolve([job("new", "scheduler")]),
  );
  const first = store.refresh("legacy");
  const second = store.refresh("legacy");
  await store.refresh("scheduler");
  expect(store.getSnapshot().scheduler.items[0].id).toBe("new");
  expect(loader).toHaveBeenCalledTimes(2);
  slow.resolve([job("old")]);
  await Promise.all([first, second]);
  expect(store.presence("a").legacy?.has("old")).toBe(true);
});

it("失败保留展示但不使用旧空集合断言删除", async () => {
  loader.mockResolvedValueOnce([job("known")]);
  await store.refresh("legacy");
  loader.mockRejectedValueOnce(new Error("offline"));
  await store.refresh("legacy");
  expect(store.getSnapshot().legacy.items[0].id).toBe("known");
  expect(store.presence("a").legacy).toBeNull();
  expect(store.getSnapshot().legacy.error).toContain("保留已知任务");
  await store.refresh("legacy");
  expect(store.presence("a").legacy?.size).toBe(0);
});

it("动作开始使负结论失效，动作前响应不能覆盖完成后的确认", async () => {
  const slow = deferred<SourcedCronTask[]>();
  loader.mockReturnValueOnce(slow.promise);
  const previous = store.refresh("legacy");
  await Promise.resolve();
  const finish = store.beginMutation("legacy", "a");
  expect(store.presence("a").legacy).toBeNull();
  await store.refresh("legacy");
  expect(loader).toHaveBeenCalledTimes(1);
  loader.mockResolvedValueOnce([job("after")]);
  await finish();
  slow.resolve([]);
  await previous;
  expect(store.presence("a").legacy?.has("after")).toBe(true);
  expect(loader).toHaveBeenCalledTimes(2);
});

it("A→B→A 不接受原 A 回包，旧 finally 不清除新请求", async () => {
  const slow = deferred<SourcedCronTask[]>();
  loader.mockReturnValueOnce(slow.promise);
  const previous = store.refresh("legacy");
  await Promise.resolve();
  store.activate({ ...context, agent_id: "b" }, "b");
  store.activate(context, "a");
  loader.mockResolvedValueOnce([job("fresh")]);
  await store.refresh("legacy");
  slow.resolve([job("stale")]);
  await previous;
  expect(store.getSnapshot().legacy.items.map((item) => item.id)).toEqual([
    "fresh",
  ]);
});

it("隐藏暂停新读取，恢复只发起每来源一轮", async () => {
  const release = store.retain(context, "a");
  await store.refresh();
  loader.mockClear();
  store.setVisible(false);
  await store.refresh();
  expect(loader).not.toHaveBeenCalled();
  store.setVisible(true);
  await store.refresh();
  expect(loader).toHaveBeenCalledTimes(2);
  release();
});

it("乐观删除只改展示，确认失败恢复最后成功任务且存在性未知", async () => {
  loader.mockResolvedValueOnce([job("known")]);
  await store.refresh("legacy");
  const finish = store.beginMutation("legacy", "a");
  store.setJobs(() => [], "a", store.getSnapshot().epoch);
  expect(store.jobs()).toEqual([]);
  expect(store.presence("a").legacy).toBeNull();
  loader.mockRejectedValueOnce(new Error("offline"));
  await finish();
  expect(store.jobs().map((item) => item.id)).toEqual(["known"]);
  expect(store.presence("a").legacy).toBeNull();
});

it("任务创建事件使事件前在途清单失效，只确认旧任务来源", async () => {
  const release = store.retain(context, "a");
  await store.refresh();
  const slow = deferred<SourcedCronTask[]>();
  loader.mockReturnValueOnce(slow.promise);
  const previous = store.refresh("legacy");
  await Promise.resolve();
  loader.mockResolvedValueOnce([job("created")]);
  document.dispatchEvent(new Event("taskCreated"));
  await store.refresh("legacy");
  slow.resolve([]);
  await previous;
  expect(store.presence("a").legacy?.has("created")).toBe(true);
  release();
});

it("同来源多个动作全部结算后只确认一次，不查询另一来源", async () => {
  const first = store.beginMutation("legacy", "a");
  const second = store.beginMutation("legacy", "a");
  await first();
  expect(loader).not.toHaveBeenCalled();
  await second();
  expect(loader).toHaveBeenCalledTimes(1);
  expect(loader.mock.calls[0][0]).toBe("legacy");
});

it("周期只查状态，保留定义及完整 membership", async () => {
  const runtime = vi.fn().mockResolvedValue([
    {
      id: "known",
      status: "ok",
      enabled: false,
      state: { last_status: "running" },
      task: { is_running: true },
    },
  ]);
  store = new ChatTaskSourceStore(loader, runtime);
  store.activate(context, "a");
  loader.mockResolvedValueOnce([job("known")]);
  await store.refresh("legacy");
  const membership = store.presence("a").legacy;
  await store.refresh("legacy", "poll");
  expect(loader).toHaveBeenCalledTimes(1);
  expect(runtime).toHaveBeenCalledWith(
    "legacy",
    context,
    ["known"],
    expect.any(AbortSignal),
  );
  expect(store.jobs()[0]).toMatchObject({
    name: "known",
    enabled: false,
    state: { last_status: "running" },
  });
  expect(store.presence("a").legacy).toBe(membership);
});
it("状态失败保留完整清单，404 只探测一次并降级旧查询", async () => {
  const runtime = vi.fn().mockRejectedValue({ status: 503 });
  store = new ChatTaskSourceStore(loader, runtime);
  store.activate(context, "a");
  loader.mockResolvedValue([job("known")]);
  await store.refresh("legacy");
  const membership = store.presence("a").legacy;
  await store.refresh("legacy", "poll");
  expect(store.presence("a").legacy).toBe(membership);
  expect(loader).toHaveBeenCalledTimes(1);
  runtime.mockRejectedValue({ status: 404 });
  await store.refresh("legacy", "poll");
  await store.refresh("legacy", "poll");
  expect(runtime).toHaveBeenCalledTimes(2);
  expect(loader).toHaveBeenCalledTimes(3);
});

it("full confirmation preempts a slow runtime reply", async () => {
  const slow = deferred<never[]>();
  const runtime = vi.fn().mockReturnValue(slow.promise);
  store = new ChatTaskSourceStore(loader, runtime);
  store.activate(context, "a");
  loader.mockResolvedValueOnce([job("known")]);
  await store.refresh("legacy");
  const poll = store.refresh("legacy", "poll");
  await Promise.resolve();
  loader.mockResolvedValueOnce([job("fresh")]);
  await store.refresh("legacy");
  slow.resolve([]);
  await poll;
  expect(store.jobs().map((job) => job.id)).toEqual(["fresh"]);
});

it("unavailable runtime IDs trigger one full confirmation, not membership replacement", async () => {
  const runtime = vi
    .fn()
    .mockResolvedValue([{ id: "known", status: "unavailable" }]);
  store = new ChatTaskSourceStore(loader, runtime);
  store.activate(context, "a");
  loader
    .mockResolvedValueOnce([job("known")])
    .mockRejectedValueOnce(new Error("offline"));
  await store.refresh("legacy");
  await store.refresh("legacy", "poll");
  expect(loader).toHaveBeenCalledTimes(2);
  expect(store.jobs()[0].id).toBe("known");
  expect(store.presence("a").legacy).toBeNull();
});
it("state_missing and older scheduler version retain factual state without full retry", async () => {
  const managed = {
    ...job("known", "scheduler"),
    definition_version: 1,
    state_version: 5,
    management_version: 2,
  };
  const runtime = vi
    .fn()
    .mockResolvedValueOnce([
      { id: "known", status: "state_missing", definition_version: 1 },
    ])
    .mockResolvedValueOnce([
      {
        id: "known",
        status: "ok",
        definition_version: 1,
        state_version: 4,
        management_version: 2,
        enabled: false,
      },
    ]);
  store = new ChatTaskSourceStore(loader, runtime);
  store.activate(context, "a");
  loader.mockResolvedValueOnce([managed]);
  await store.refresh("scheduler");
  await store.refresh("scheduler", "poll");
  expect(store.jobs()[0]).toBe(managed);
  expect(store.getSnapshot().scheduler.error).toContain("缺失");
  await store.refresh("scheduler", "poll");
  expect(store.jobs()[0]).toBe(managed);
  expect(loader).toHaveBeenCalledTimes(1);
});
