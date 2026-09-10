import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import FailurePanel from "./FailurePanel";
import { batchOperations } from "../../../api/modules/batchOperations";

vi.mock("../../../api/modules/batchOperations", () => ({
  batchOperations: { failures: vi.fn(), retry: vi.fn() },
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it.each([false, true])(
  "requires repaired auth confirmation and queues once (paused=%s)",
  async (paused) => {
    const item = {
      id: 7,
      attempt_count: 3,
      tenant_id: "alice",
      error_message: "auth expired",
      failure_type: "auth_expired",
    };
    vi.mocked(batchOperations.failures).mockResolvedValue({
      counts: [{ failure_type: "auth_expired", label: "鉴权过期", count: 1 }],
      items: [item],
      has_more: false,
    });
    vi.mocked(batchOperations.retry).mockResolvedValue({
      queued: 1,
      skipped: 0,
      dispatch_paused: paused,
    });
    render(<FailurePanel batchId="batch-a" onQueued={vi.fn()} />);
    await screen.findByText("鉴权过期：1");
    fireEvent.mouseDown(
      screen.getByRole("combobox", { name: "选择重试失败类型" }),
    );
    const option = (await screen.findAllByText("鉴权过期 (1)")).find((e) =>
      e.closest(".ant-select-item-option"),
    );
    fireEvent.click(option!);
    await waitFor(() =>
      expect(batchOperations.failures).toHaveBeenLastCalledWith("batch-a", [
        "auth_expired",
      ]),
    );
    const button = screen.getByRole("button", {
      name: "重试预览的 1 个失败任务",
    });
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByText("已刷新鉴权或修复配置"));
    await waitFor(() => expect(button).not.toBeDisabled());
    fireEvent.click(button);
    await screen.findByText(
      `已加入重试队列 1 个，状态已变化跳过 0 个${
        paused ? "；批调度已暂停，等待恢复" : ""
      }`,
    );
    expect(batchOperations.retry).toHaveBeenCalledWith(
      "batch-a",
      [item],
      true,
      false,
    );
    expect(batchOperations.retry).toHaveBeenCalledTimes(1);
  },
);
