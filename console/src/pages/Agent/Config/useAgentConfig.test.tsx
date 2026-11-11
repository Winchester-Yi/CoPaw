import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useIframeStore } from "@/stores/iframeStore";
import { useAgentStore } from "@/stores/agentStore";
import { useAgentConfig } from "./useAgentConfig";

const mocks = vi.hoisted(() => ({
  api: {
    getAgentRunningConfig: vi.fn(),
    getAgentLanguage: vi.fn(),
    getUserTimezone: vi.fn(),
    updateAgentRunningConfig: vi.fn(),
  },
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  t: (key: string) => key,
}));
vi.mock("@/api", () => ({ default: mocks.api }));
vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: mocks.message }),
}));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: mocks.t }) }));
beforeEach(() => {
  vi.clearAllMocks();
  useIframeStore.setState({ userId: "alice", source: "bank", manager: false });
  useAgentStore.setState({ selectedAgent: "default" });
  mocks.api.getAgentRunningConfig.mockResolvedValue({});
  mocks.api.getAgentLanguage.mockResolvedValue({ language: "zh" });
  mocks.api.getUserTimezone.mockResolvedValue({ timezone: "Asia/Shanghai" });
});
afterEach(cleanup);

it("梦境配置保存成功但同步未受理，保留保存事实并传出失败请求号", async () => {
  const submission = {
    submission_status: "failed",
    request_id: "save-1",
    error_code: "system_sync_not_submitted",
  };
  mocks.api.updateAgentRunningConfig.mockResolvedValue({
    scheduler_sync: submission,
  });
  const { result } = renderHook(useAgentConfig);
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => result.current.handleSave());
  expect(result.current.syncSubmission).toEqual(submission);
  expect(mocks.message.success).not.toHaveBeenCalled();
  expect(mocks.message.warning).toHaveBeenCalledWith(
    expect.stringContaining("配置已保存"),
  );
  expect(
    mocks.api.updateAgentRunningConfig.mock.calls[0][0],
  ).not.toHaveProperty("scheduler_sync");
});

it("受理成功也只提示已受理，不宣称平台已同步", async () => {
  mocks.api.updateAgentRunningConfig.mockResolvedValue({
    scheduler_sync: { submission_status: "accepted", request_id: "save-2" },
  });
  const { result } = renderHook(useAgentConfig);
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => result.current.handleSave());
  expect(mocks.message.success).toHaveBeenCalledWith(
    "配置已保存，平台同步已受理",
  );
  expect(result.current.syncSubmission?.request_id).toBe("save-2");
});

it("来源切换后旧保存响应不能覆盖新来源回执或显示旧成功提示", async () => {
  let finish!: (value: unknown) => void;
  mocks.api.updateAgentRunningConfig.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const { result } = renderHook(useAgentConfig);
  await waitFor(() => expect(result.current.loading).toBe(false));
  let saving!: Promise<void>;
  act(() => {
    saving = result.current.handleSave();
  });
  await waitFor(() =>
    expect(mocks.api.updateAgentRunningConfig).toHaveBeenCalled(),
  );
  act(() => useIframeStore.setState({ source: "other" }));
  await act(async () => {
    finish({
      scheduler_sync: {
        submission_status: "accepted",
        request_id: "old-source",
      },
    });
    await saving;
  });
  expect(result.current.syncContext.source_id).toBe("other");
  expect(result.current.syncSubmission).toBeUndefined();
  expect(mocks.message.success).not.toHaveBeenCalled();
});
