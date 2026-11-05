import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useTaskSessionView } from "./useTaskSessionView";
import { filterStaleTaskSessions } from "./sessionApi/taskSessions";

const mocks = vi.hoisted(() => ({
  sessions: [] as unknown[],
  setSessions: vi.fn(),
  getLoadedSessionView: vi.fn(),
  projectSessionView: vi.fn(),
  getSessionList: vi.fn(),
}));
vi.mock("@/components/agentscope-chat", () => ({
  useChatAnywhereSessionsState: () => ({
    sessions: mocks.sessions,
    setSessions: mocks.setSessions,
  }),
}));
vi.mock("./sessionApi", () => ({ default: mocks }));
afterEach(cleanup);

it("快照投影在合并 currentOnly 后过滤，unknown 恢复不重查摘要且保留消息", async () => {
  const task = {
    id: "task",
    messages: [{ id: "live" }],
    generating: true,
    meta: {
      session_kind: "task",
      task_job_id: "gone",
      management_owner: "legacy",
    },
  };
  mocks.sessions = [task, { id: "normal", messages: [{ id: "normal-live" }] }];
  let presence = {
    legacy: new Set<string>(),
    scheduler: new Set<string>(),
  } as { legacy: Set<string> | null; scheduler: Set<string> | null };
  mocks.getLoadedSessionView.mockImplementation(() =>
    filterStaleTaskSessions(
      [
        { ...task, messages: [] },
        { id: "normal", messages: [] },
      ],
      presence,
    ),
  );
  mocks.projectSessionView.mockImplementation((sessions) =>
    filterStaleTaskSessions(sessions, presence),
  );
  const { rerender } = renderHook(
    ({ value }) => useTaskSessionView(value, "scope", true),
    { initialProps: { value: presence } },
  );
  expect(mocks.setSessions).toHaveBeenLastCalledWith([mocks.sessions[1]]);
  mocks.sessions = [mocks.sessions[1]];
  presence = { legacy: null, scheduler: new Set() };
  await act(async () => {
    rerender({ value: presence });
  });
  expect(
    mocks.setSessions.mock.lastCall?.[0].map(
      (session: { id: string }) => session.id,
    ),
  ).toEqual(["task", "normal"]);
  expect(mocks.setSessions.mock.lastCall?.[0][0].messages).toEqual([
    { id: "live" },
  ]);
  expect(mocks.setSessions.mock.lastCall?.[0][1].messages).toEqual([
    { id: "normal-live" },
  ]);
  expect(mocks.getSessionList).not.toHaveBeenCalled();
});
