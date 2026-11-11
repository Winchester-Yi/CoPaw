import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { SystemSyncSubmission } from "@/api/types/systemCron";
import { useIframeStore } from "@/stores/iframeStore";
import { useAgentStore } from "@/stores/agentStore";
import HeartbeatPage from ".";

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  update: vi.fn(),
  status: vi.fn(),
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  t: (key: string) => key,
}));
vi.mock("@/api", () => ({
  default: {
    getHeartbeatConfig: mocks.get,
    updateHeartbeatConfig: mocks.update,
  },
}));
vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: mocks.message }),
}));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: mocks.t }) }));
vi.mock("@/components/SystemCronSyncStatus", () => ({
  SystemCronSyncStatus: (props: { submission?: SystemSyncSubmission }) => {
    mocks.status(props);
    return <div data-testid="sync-status" />;
  },
}));
beforeEach(() => {
  vi.clearAllMocks();
  useIframeStore.setState({ userId: "alice", source: "bank" });
  useAgentStore.setState({ selectedAgent: "default" });
  mocks.get.mockResolvedValue({ enabled: true, every: "6h", target: "main" });
});
afterEach(cleanup);

it("心跳页将失败受理回执交给同步状态区，不把已保存配置说成回滚", async () => {
  const submission = {
    submission_status: "failed",
    request_id: "heartbeat-save",
    error_code: "system_sync_not_submitted",
  };
  mocks.update.mockResolvedValue({ scheduler_sync: submission });
  render(<HeartbeatPage />);
  fireEvent.click(await screen.findByRole("button", { name: "common.save" }));
  await waitFor(() =>
    expect(mocks.status).toHaveBeenCalledWith(
      expect.objectContaining({ submission }),
    ),
  );
  expect(mocks.update).toHaveBeenCalledOnce();
  expect(mocks.message.success).not.toHaveBeenCalled();
  expect(mocks.message.warning).toHaveBeenCalledWith(
    expect.stringContaining("配置已保存"),
  );
});
