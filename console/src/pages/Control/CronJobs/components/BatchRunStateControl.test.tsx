import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import BatchRunStateControl from "./BatchRunStateControl";
import { batchOperations } from "../../../../api/modules/batchOperations";

vi.mock("../../../../api/modules/batchOperations", () => ({
  batchOperations: { getRunState: vi.fn(), setRunState: vi.fn() },
}));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

const parent = {
  id: "parent",
  enabled: false,
  meta: { broadcast_dispatch_intents_enabled: true },
};
const running = {
  source_id: "s",
  tenant_id: "owner",
  parent_job_id: "parent",
  paused: false,
  version: 1,
  resumed_at: null,
  updated_at: "2026-09-09T10:00:00+08:00",
  claimed_count: 0,
  inflight_count: 0,
};

it.each([
  { id: "normal", meta: {} },
  { id: "broadcast", meta: { broadcast_source_job_id: "p" } },
  {
    id: "child",
    meta: {
      broadcast_dispatch_intents_enabled: true,
      broadcast_source_job_id: "p",
    },
  },
])("does not initialize or expose an entire-batch switch for $id", (job) => {
  render(<BatchRunStateControl job={job} />);
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  expect(batchOperations.getRunState).not.toHaveBeenCalled();
});

it("pauses independently using the server version", async () => {
  vi.mocked(batchOperations.getRunState).mockResolvedValue(running);
  vi.mocked(batchOperations.setRunState).mockResolvedValue({
    ...running,
    paused: true,
    version: 2,
  });
  render(<BatchRunStateControl job={parent} />);
  const toggle = await screen.findByRole("switch", { name: "批调度运行状态" });
  await waitFor(() => expect(toggle).not.toBeDisabled());
  expect(toggle).toBeChecked();
  fireEvent.click(toggle);
  await screen.findByText("已暂停后续调度；交接中及运行中任务自然收尾");
  expect(toggle).not.toBeChecked();
  expect(batchOperations.setRunState).toHaveBeenCalledWith("parent", true, 1);
});

it("does not pretend a failed save succeeded and requires refresh", async () => {
  vi.mocked(batchOperations.getRunState).mockResolvedValue(running);
  vi.mocked(batchOperations.setRunState).mockRejectedValue(
    new Error("状态已被修改，请刷新后重试"),
  );
  render(<BatchRunStateControl job={parent} />);
  const toggle = await screen.findByRole("switch", { name: "批调度运行状态" });
  await waitFor(() => expect(toggle).not.toBeDisabled());
  fireEvent.click(toggle);
  await screen.findByText("状态已被修改，请刷新后重试");
  expect(toggle).toBeChecked();
  expect(toggle).toBeDisabled();
  vi.mocked(batchOperations.getRunState).mockResolvedValue({
    ...running,
    paused: true,
    version: 2,
  });
  fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
  await waitFor(() => expect(toggle).not.toBeChecked());
});

it("ignores a late read after switching parent", async () => {
  let resolveOld!: (value: typeof running) => void;
  vi.mocked(batchOperations.getRunState)
    .mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    )
    .mockResolvedValueOnce({
      ...running,
      parent_job_id: "second",
      paused: true,
    });
  const view = render(<BatchRunStateControl job={parent} />);
  view.rerender(<BatchRunStateControl job={{ ...parent, id: "second" }} />);
  await screen.findByText("已暂停后续调度；交接中及运行中任务自然收尾");
  await act(async () => resolveOld(running));
  expect(screen.getByRole("switch")).not.toBeChecked();
});

it("leaves an unavailable state disabled instead of inferring from the parent", async () => {
  vi.mocked(batchOperations.getRunState).mockRejectedValue(
    new Error("迁移未就绪"),
  );
  render(<BatchRunStateControl job={parent} />);
  await screen.findByText("迁移未就绪");
  expect(screen.getByRole("switch")).toBeDisabled();
  expect(screen.getByText("状态待确认")).toBeInTheDocument();
});

it("ignores a late save after switching parent", async () => {
  let resolveSave!: (value: typeof running) => void;
  vi.mocked(batchOperations.getRunState)
    .mockResolvedValueOnce(running)
    .mockResolvedValueOnce({ ...running, parent_job_id: "second" });
  vi.mocked(batchOperations.setRunState).mockReturnValue(
    new Promise((resolve) => {
      resolveSave = resolve;
    }),
  );
  const view = render(<BatchRunStateControl job={parent} />);
  await waitFor(() => expect(screen.getByRole("switch")).not.toBeDisabled());
  fireEvent.click(screen.getByRole("switch"));
  view.rerender(<BatchRunStateControl job={{ ...parent, id: "second" }} />);
  await waitFor(() => expect(screen.getByRole("switch")).not.toBeDisabled());
  await act(async () => resolveSave({ ...running, paused: true, version: 2 }));
  expect(screen.getByRole("switch")).toBeChecked();
  expect(screen.getByRole("switch")).not.toBeDisabled();
});
