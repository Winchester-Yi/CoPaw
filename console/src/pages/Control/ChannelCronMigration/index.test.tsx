import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ChannelCronMigration from "./index";

const api = vi.hoisted(() => ({
  channelJobs: vi.fn(),
  channelRecent: vi.fn(),
}));
vi.mock("@/api/modules/cronMigration", () => ({ cronMigrationApi: api }));
vi.mock("@/api/cronTaskContext", () => ({
  cronAccessKey: () => "channel",
  currentCronContext: () => ({ source_id: "bank" }),
}));
vi.mock("@/stores/iframeStore", () => ({
  useIframeStore: (selector: (state: { manager: boolean }) => unknown) =>
    selector({ manager: true }),
}));
vi.mock("@/stores/agentStore", () => ({
  useAgentStore: () => "default",
}));

afterEach(cleanup);

it("展示匹配总量和子任务计数，服务端翻页并清空上页选择", async () => {
  api.channelRecent.mockResolvedValue({ items: [] });
  api.channelJobs.mockImplementation((_context, params: URLSearchParams) => {
    const page = params.get("page");
    return Promise.resolve({
      items: [
        {
          job_id: page === "2" ? "independent" : "parent",
          name: page === "2" ? "独立任务甲" : "父任务甲",
          tenant_id: "alice",
          agent_id: "default",
          migration_status: "not_migrated",
          child_count: page === "2" ? 0 : 7,
          cron_expr: page === "2" ? "*/10 * * * *" : "15 9 * * 1-5",
        },
      ],
      total: 21,
      task_total: 28,
    });
  });
  render(<ChannelCronMigration />);
  const parent = await screen.findByText("父任务甲");
  expect(screen.getByRole("status")).toHaveTextContent(/^共 28 个任务$/);
  expect(
    screen.getByRole("columnheader", { name: "子任务数" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("columnheader", { name: "Cron 表达式" }),
  ).toBeInTheDocument();
  const parentRow = within(parent.closest("tr")!);
  expect(parentRow.getByRole("cell", { name: "7" })).toBeInTheDocument();
  expect(
    parentRow.getByRole("cell", { name: "15 9 * * 1-5" }),
  ).toBeInTheDocument();
  fireEvent.click(parentRow.getByRole("checkbox"));
  expect(screen.getByRole("button", { name: "预览迁移（1）" })).toBeEnabled();
  fireEvent.click(screen.getByTitle("2"));
  expect(await screen.findByText("独立任务甲")).toBeInTheDocument();
  expect(
    screen.getByRole("cell", { name: "*/10 * * * *" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(/^共 28 个任务$/);
  expect(screen.queryByText("父任务甲")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "预览迁移（0）" })).toBeDisabled();
  expect(api.channelJobs.mock.lastCall?.[1].toString()).toBe("page=2&limit=20");

  const search = screen.getByRole("searchbox", { name: "任务名或 ID" });
  fireEvent.change(search, { target: { value: "父任务甲" } });
  fireEvent.keyDown(search, { key: "Enter", code: "Enter" });
  await waitFor(() => {
    const params = api.channelJobs.mock.lastCall?.[1] as URLSearchParams;
    expect(params.get("page")).toBe("1");
    expect(params.get("query")).toBe("父任务甲");
  });
});
