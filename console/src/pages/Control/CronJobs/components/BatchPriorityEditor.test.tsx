import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import BatchPriorityEditor from "./BatchPriorityEditor";
import { batchOperations } from "../../../../api/modules/batchOperations";

vi.mock("../../../../api/modules/batchOperations", () => ({
  batchOperations: { savePriority: vi.fn() },
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("saves the reordered user and primary-branch lists", async () => {
  vi.mocked(batchOperations.savePriority).mockResolvedValue({});
  render(
    <BatchPriorityEditor
      jobId="parent"
      initial={{ user_ids: ["alice", "bob"], branch_ids: ["121", "110"] }}
    />,
  );
  fireEvent.click(screen.getByLabelText("优先用户 bob 上移"));
  fireEvent.click(screen.getByLabelText("优先一级分行 110 上移"));
  fireEvent.click(screen.getByText("保存优先策略"));
  await screen.findByText("优先策略已保存，从新批次生效");
  expect(batchOperations.savePriority).toHaveBeenCalledWith("parent", {
    user_ids: ["bob", "alice"],
    branch_ids: ["110", "121"],
  });
});

it("keeps edited values available after a failed save", async () => {
  vi.mocked(batchOperations.savePriority).mockRejectedValue(
    new Error("同步失败"),
  );
  render(
    <BatchPriorityEditor
      jobId="parent"
      initial={{ user_ids: ["alice"], branch_ids: [] }}
    />,
  );
  fireEvent.click(screen.getByText("保存优先策略"));
  await screen.findByText("同步失败");
  await waitFor(() =>
    expect(
      screen.getByText("保存优先策略").closest("button"),
    ).not.toBeDisabled(),
  );
  expect(screen.getByLabelText("优先用户 alice 移除")).toBeInTheDocument();
});
