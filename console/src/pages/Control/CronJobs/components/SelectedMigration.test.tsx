import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SelectedMigration from "./SelectedMigration";
import { cronMigrationApi } from "@/api/modules/cronMigration";
import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";

vi.mock("@/api/modules/cronMigration", () => ({
  cronMigrationApi: {
    submit: vi.fn(),
    get: vi.fn(),
    recent: vi.fn(),
    confirm: vi.fn(),
  },
}));
const preview = {
  operation_id: "op-one",
  status: "awaiting_confirmation",
  root_count: 1,
  member_count: 4,
  groups: [{ job_id: "parent", status: "preview_ready", member_count: 4 }],
};
beforeEach(() => {
  vi.resetAllMocks();
  useIframeStore.setState({
    userId: "alice",
    source: "bank",
    manager: false,
    isSuperManager: false,
  });
  useAgentStore.setState({ selectedAgent: "default" });
  vi.mocked(cronMigrationApi.submit).mockResolvedValue(preview);
  vi.mocked(cronMigrationApi.confirm).mockResolvedValue({
    ...preview,
    status: "completed",
  });
});
afterEach(cleanup);

it("previews the expanded group before any migration confirmation", async () => {
  render(
    <SelectedMigration selectedIds={["parent"]} clearSelection={vi.fn()} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "迁移到新版（1）" }));
  expect(await screen.findByText("1 个父任务，3 个子任务")).toBeTruthy();
  expect(cronMigrationApi.confirm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认迁移上述范围" }));
  await waitFor(() =>
    expect(cronMigrationApi.confirm).toHaveBeenCalledWith(
      expect.objectContaining({ actor_id: "alice", agent_id: "default" }),
      "op-one",
    ),
  );
  expect(await screen.findByText(/状态：已完成/)).toBeTruthy();
});

it("scope changes discard a delayed preview", async () => {
  let finish!: (value: typeof preview) => void;
  vi.mocked(cronMigrationApi.submit).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  render(
    <SelectedMigration selectedIds={["parent"]} clearSelection={vi.fn()} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "迁移到新版（1）" }));
  await act(async () => {
    useAgentStore.setState({ selectedAgent: "other" });
  });
  await act(async () => {
    finish(preview);
  });
  expect(screen.queryByRole("button", { name: "确认迁移上述范围" })).toBeNull();
});

it("reopens persisted migration progress without resubmitting the tasks", async () => {
  vi.mocked(cronMigrationApi.recent).mockResolvedValue({
    items: [{ ...preview, status: "failed" }],
  });
  render(<SelectedMigration selectedIds={[]} clearSelection={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "迁移进度" }));
  expect(
    await screen.findByRole("button", { name: "重试未完成部分" }),
  ).toBeTruthy();
  expect(cronMigrationApi.submit).not.toHaveBeenCalled();
});
