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

it("still requires stopped execution confirmation for timeout retries", async () => {
  const item = {
    id: 8,
    attempt_count: 1,
    tenant_id: "alice",
    error_message: "timeout",
    failure_type: "execution_timeout",
  };
  vi.mocked(batchOperations.failures).mockResolvedValue({
    counts: [
      { failure_type: "execution_timeout", label: "执行超时", count: 1 },
    ],
    items: [item],
    has_more: false,
  });
  vi.mocked(batchOperations.retry).mockResolvedValue({ queued: 1, skipped: 0 });
  render(<FailurePanel batchId="batch-timeout" onQueued={vi.fn()} />);
  await screen.findByText("执行超时：1");
  fireEvent.mouseDown(
    screen.getByRole("combobox", { name: "选择重试失败类型" }),
  );
  const option = (await screen.findAllByText("执行超时 (1)")).find((e) =>
    e.closest(".ant-select-item-option"),
  );
  fireEvent.click(option!);
  const checkbox = await screen.findByRole("checkbox", {
    name: "已核对旧执行及其子任务，确认已停止，可重新执行",
  });
  const button = screen.getByRole("button", {
    name: "重试预览的 1 个失败任务",
  });
  expect(button).toBeDisabled();
  fireEvent.click(checkbox);
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  await waitFor(() =>
    expect(batchOperations.retry).toHaveBeenCalledWith(
      "batch-timeout",
      [item],
      true,
    ),
  );
});

it.each([
  [false, "auth_expired", "鉴权过期"],
  [true, "auth_expired", "鉴权过期"],
  [false, "configuration", "配置错误"],
  [true, "configuration", "配置错误"],
] as const)(
  "queues a manual retry without repair confirmation (paused=%s, type=%s)",
  async (paused, failureType, label) => {
    const item = {
      id: 7,
      attempt_count: 3,
      tenant_id: "alice",
      error_message: "auth expired",
      failure_type: failureType,
    };
    vi.mocked(batchOperations.failures).mockResolvedValue({
      counts: [{ failure_type: failureType, label, count: 1 }],
      items: [item],
      has_more: false,
    });
    vi.mocked(batchOperations.retry).mockResolvedValue({
      queued: 1,
      skipped: 0,
      dispatch_paused: paused,
    });
    render(<FailurePanel batchId="batch-a" onQueued={vi.fn()} />);
    await screen.findByText(`${label}：1`);
    expect(
      screen.queryByText(/重试会重新执行整个任务/),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("已刷新鉴权或修复配置")).not.toBeInTheDocument();
    fireEvent.mouseDown(
      screen.getByRole("combobox", { name: "选择重试失败类型" }),
    );
    const option = (await screen.findAllByText(`${label} (1)`)).find((e) =>
      e.closest(".ant-select-item-option"),
    );
    fireEvent.click(option!);
    await waitFor(() =>
      expect(batchOperations.failures).toHaveBeenLastCalledWith("batch-a", [
        failureType,
      ]),
    );
    const button = screen.getByRole("button", {
      name: "重试预览的 1 个失败任务",
    });
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
      false,
    );
    expect(batchOperations.retry).toHaveBeenCalledTimes(1);
  },
);
