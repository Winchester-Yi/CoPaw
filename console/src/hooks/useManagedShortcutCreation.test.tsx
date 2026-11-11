import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import type {
  ManagedCronSpec,
  ManagedJobDetail,
  ManagedOperation,
} from "@/api/types/schedulerCron";
import { useManagedShortcutCreation } from "./useManagedShortcutCreation";

const current = vi.hoisted(() => ({
  actor_id: "alice",
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
}));
vi.mock("./useCronContext", () => ({
  getCurrentCronContext: () => ({ ...current }),
}));
vi.mock("@/api/modules/schedulerCron", () => ({
  schedulerCronApi: {
    create: vi.fn(),
    retry: vi.fn(),
    operation: vi.fn(),
    detail: vi.fn(),
  },
}));
const context = { ...current };
const spec = {
  name: "report",
  enabled: true,
  task_type: "agent",
  request: { input: "hello" },
  schedule: {
    type: "cron",
    cron: "0 9 * * *",
    timezone: "Asia/Shanghai",
    run_on_non_workday: false,
  },
  dispatch: {
    type: "channel",
    channel: "console",
    mode: "final",
    target: { user_id: "alice", session_id: "" },
  },
} as ManagedCronSpec;
const operation = {
  operation_id: "operation-1",
  job_id: "job-1",
  status: "completed",
  stage: "completed",
  action: "create",
  version: 3,
  expected_version: 1,
  logical_delete_applied: false,
} satisfies ManagedOperation;
const detail = {
  management_owner: "scheduler",
  agent_id: "default",
  management_status: "ready",
  spec: { ...spec, id: "job-1", tenant_id: "alice", source_id: "bank" },
} as ManagedJobDetail;
beforeEach(() => {
  vi.clearAllMocks();
  Object.assign(current, context);
  vi.mocked(schedulerCronApi.create).mockResolvedValue({ operation });
  vi.mocked(schedulerCronApi.detail).mockResolvedValue(detail);
});
afterEach(() => vi.useRealTimers());

it("网络未知后重发完全相同的内容和请求号，不换新任务", async () => {
  vi.mocked(schedulerCronApi.create).mockRejectedValueOnce(
    new Error("网络未知"),
  );
  const { result } = renderHook(() =>
    useManagedShortcutCreation(context, "case:1"),
  );
  await act(async () => {
    await expect(result.current.create(spec)).rejects.toThrow("网络未知");
  });
  expect(result.current.locked).toBe(true);
  await act(async () => {
    expect(await result.current.create(spec)).toEqual(detail);
  });
  const calls = vi.mocked(schedulerCronApi.create).mock.calls;
  expect(calls).toHaveLength(2);
  expect(calls[0]).toEqual(calls[1]);
  expect(calls[0][2]).toEqual(expect.any(String));
  expect(result.current.locked).toBe(false);
});

it("已受理不算创建成功，只在操作完成且详情ready后返回", async () => {
  vi.useFakeTimers();
  vi.mocked(schedulerCronApi.create).mockResolvedValue({
    operation: { ...operation, status: "pending", stage: "prepare" },
  });
  vi.mocked(schedulerCronApi.operation).mockResolvedValue(operation);
  const { result } = renderHook(() =>
    useManagedShortcutCreation(context, "case:1"),
  );
  let done = false;
  let pending: Promise<ManagedJobDetail>;
  await act(async () => {
    pending = result.current.create(spec).then((value) => {
      done = true;
      return value;
    });
  });
  expect(done).toBe(false);
  expect(schedulerCronApi.detail).not.toHaveBeenCalled();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
    await pending!;
  });
  expect(done).toBe(true);
  expect(schedulerCronApi.operation).toHaveBeenCalledWith(
    context,
    "job-1",
    "operation-1",
  );
});

it("失败管理操作在同一任务上重试，不重新创建", async () => {
  vi.mocked(schedulerCronApi.create).mockResolvedValue({
    operation: {
      ...operation,
      status: "failed",
      error_code: "swe_prepare_failed",
    },
  });
  vi.mocked(schedulerCronApi.retry).mockResolvedValue({ operation });
  const { result } = renderHook(() =>
    useManagedShortcutCreation(context, "case:1"),
  );
  await act(async () => {
    await expect(result.current.create(spec)).rejects.toThrow(
      "swe_prepare_failed",
    );
  });
  await act(async () => {
    await result.current.create(spec);
  });
  expect(schedulerCronApi.create).toHaveBeenCalledTimes(1);
  expect(schedulerCronApi.retry).toHaveBeenCalledWith(
    context,
    "job-1",
    "operation-1",
  );
});

it("已有请求结果未确认时禁止改内容，详情失败重试也不重跑创建", async () => {
  vi.mocked(schedulerCronApi.detail).mockRejectedValueOnce(
    new Error("详情不可用"),
  );
  const { result } = renderHook(() =>
    useManagedShortcutCreation(context, "case:1"),
  );
  await act(async () => {
    await expect(result.current.create(spec)).rejects.toThrow("详情不可用");
  });
  await act(async () => {
    await expect(
      result.current.create({ ...spec, name: "edited" }),
    ).rejects.toThrow(/上次请求/);
  });
  await act(async () => {
    await result.current.create(spec);
  });
  expect(schedulerCronApi.create).toHaveBeenCalledTimes(1);
});

it("切换身份后旧响应拒绝为AbortError，不向新工作区继续查询", async () => {
  let complete!: (value: { operation: ManagedOperation }) => void;
  vi.mocked(schedulerCronApi.create).mockReturnValue(
    new Promise((resolve) => {
      complete = resolve;
    }),
  );
  const { result, rerender } = renderHook(
    ({ ctx }) => useManagedShortcutCreation(ctx, "case:1"),
    { initialProps: { ctx: context } },
  );
  let settled: Promise<unknown>;
  act(() => {
    settled = result.current.create(spec).catch((error: Error) => error.name);
  });
  Object.assign(current, { source_id: "other" });
  rerender({ ctx: { ...context, source_id: "other" } });
  await act(async () => {
    complete({ operation });
    expect(await settled!).toBe("AbortError");
  });
  expect(schedulerCronApi.detail).not.toHaveBeenCalled();
  await waitFor(() => expect(result.current.loading).toBe(false));
});
