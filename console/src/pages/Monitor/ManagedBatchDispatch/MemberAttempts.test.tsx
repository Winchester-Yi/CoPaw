import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { managedBatchBoard } from "@/api/modules/managedBatchBoard";
import MemberAttempts from "./MemberAttempts";

vi.mock("@/api/modules/managedBatchBoard", () => ({
  managedBatchBoard: { memberAttempts: vi.fn() },
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const target = { source: "bank", batchId: "batch-a", memberId: "member-a" };

it("零次重试没有按钮；点击正数才读取该成员业务尝试", async () => {
  vi.mocked(managedBatchBoard.memberAttempts).mockResolvedValue({
    items: [
      { execution_id: "first", business_attempt: 1, current: false, status: "error",
        settled_status: "error", started_at: null, finished_at: null,
        error_type: "Prompt", error_message: "旧尝试失败" },
      { execution_id: "retry", business_attempt: 2, current: true, status: "success",
        settled_status: "success", started_at: null, finished_at: null,
        error_type: null, error_message: "" },
    ],
  });
  const { rerender } = render(<MemberAttempts {...target} count={0} />);
  expect(screen.getByText("重试次数：0")).toBeInTheDocument();
  expect(screen.queryByRole("button")).toBeNull();
  rerender(<MemberAttempts {...target} count={1} />);
  expect(managedBatchBoard.memberAttempts).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "重试次数 1，查看尝试历史" }));
  expect(await screen.findByText("首次执行")).toBeInTheDocument();
  expect(screen.getByText("重试 1")).toBeInTheDocument();
  expect(screen.getByText("当前")).toBeInTheDocument();
  expect(screen.getByText("旧尝试失败")).toBeInTheDocument();
  expect(managedBatchBoard.memberAttempts).toHaveBeenCalledWith(
    "bank", "batch-a", "member-a", expect.any(AbortSignal),
  );
});
