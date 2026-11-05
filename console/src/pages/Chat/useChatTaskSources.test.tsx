import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useChatTaskSources } from "./useChatTaskSources";
import { loadTaskSource, tagTask } from "@/api/modules/cronTaskSources";
import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";
import { chatTaskSourceStore } from "@/stores/chatTaskSourceStore";
import type { SourcedCronTask } from "@/api/types/cronTaskSource";

vi.mock("@/api/modules/cronTaskSources", async (original) => ({
  ...(await original<object>()),
  loadTaskSource: vi.fn(),
}));
beforeEach(() => {
  chatTaskSourceStore.reset();
  vi.mocked(loadTaskSource).mockReset().mockResolvedValue([]);
  useIframeStore.setState({
    userId: "u",
    source: "s",
    manager: false,
    isSuperManager: false,
  });
  useAgentStore.setState({ selectedAgent: "a" });
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
});
afterEach(() => {
  cleanup();
  chatTaskSourceStore.reset();
  vi.useRealTimers();
});

it("两个订阅者共用一轮，悬挂的旧来源不阻塞新来源展示", async () => {
  vi.mocked(loadTaskSource).mockImplementation(
    (owner, context, _signal, key) =>
      owner === "legacy"
        ? new Promise(() => {})
        : Promise.resolve([
            tagTask({ id: "new" } as never, owner, context, key),
          ]),
  );
  const first = renderHook(() => useChatTaskSources());
  renderHook(() => useChatTaskSources());
  await waitFor(() => expect(first.result.current.jobs[0]?.id).toBe("new"));
  expect(loadTaskSource).toHaveBeenCalledTimes(2);
});

it("Agent 切换立即隔离旧数据，迟到响应不能覆盖新列表", async () => {
  let resolve!: (items: SourcedCronTask[]) => void;
  vi.mocked(loadTaskSource).mockImplementation(
    (owner, context, _signal, key) => {
      if (owner === "scheduler") return Promise.resolve([]);
      if (context.agent_id === "a")
        return new Promise((done) => {
          resolve = done;
        });
      return Promise.resolve([
        tagTask({ id: "new" } as never, owner, context, key),
      ]);
    },
  );
  const { result } = renderHook(() => useChatTaskSources());
  await waitFor(() => expect(resolve).toBeDefined());
  await act(async () => {
    useAgentStore.setState({ selectedAgent: "b" });
  });
  await waitFor(() => expect(result.current.jobs[0]?.id).toBe("new"));
  await act(async () => {
    resolve([]);
  });
  expect(result.current.jobs.map((job) => job.id)).toEqual(["new"]);
});

it("来源查询失败保留已知任务，存在性 unknown 且展示错误", async () => {
  vi.mocked(loadTaskSource).mockImplementation(
    async (owner, context, _signal, key) =>
      owner === "legacy"
        ? [tagTask({ id: "known" } as never, owner, context, key)]
        : [],
  );
  const { result } = renderHook(() => useChatTaskSources());
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  vi.mocked(loadTaskSource).mockRejectedValueOnce(new Error("offline"));
  await act(async () => {
    await result.current.refreshJobs({ owner: "legacy" });
  });
  expect(result.current.jobs[0].id).toBe("known");
  expect(result.current.presence.legacy).toBeNull();
  expect(result.current.error).toContain("保留已知任务");
});

it("隐藏时停止轮询，多个订阅者恢复只启动一轮", async () => {
  vi.useFakeTimers();
  renderHook(() => useChatTaskSources());
  renderHook(() => useChatTaskSources());
  await act(async () => {
    await Promise.resolve();
  });
  vi.mocked(loadTaskSource).mockClear();
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "hidden",
  });
  await act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  expect(loadTaskSource).not.toHaveBeenCalled();
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  await act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  expect(loadTaskSource).toHaveBeenCalledTimes(2);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  expect(loadTaskSource).toHaveBeenCalledTimes(2); // Empty known sources need no periodic HTTP.
});

it("认证清空后停止查询，同身份重新认证才恢复且只确认一轮", async () => {
  const { result } = renderHook(() => useChatTaskSources());
  await waitFor(() => expect(result.current.loaded).toBe(true));
  vi.mocked(loadTaskSource).mockClear();
  await act(async () => {
    window.dispatchEvent(new Event("swe:auth-cleared"));
  });
  expect(result.current.active).toBe(false);
  expect(result.current.jobs).toEqual([]);
  await act(async () => {
    useIframeStore.setState({ source: "after-logout" });
  });
  expect(result.current.active).toBe(false);
  expect(loadTaskSource).not.toHaveBeenCalled();
  await act(async () => {
    window.dispatchEvent(new Event("swe:auth-ready"));
  });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  expect(loadTaskSource).toHaveBeenCalledTimes(2);
});
