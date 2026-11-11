import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { request } from "@/api/request";
import { ManagedRechecksTable } from "./ManagedRechecksTable";

vi.mock("@/api/request", () => ({ request: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const context = {
  actor_id: "owner",
  tenant_id: "owner",
  source_id: "bank",
  agent_id: "default",
};

it("loads active records, uses the next cursor and refreshes history separately", async () => {
  vi.mocked(request).mockImplementation(async (path) => {
    if (path.includes("view=history"))
      return {
        items: [
          {
            recheck_id: "done",
            job_name: "历史任务",
            status: "success",
            chat_id: "chat-1",
          },
        ],
      };
    if (path.includes("cursor=active-next")) return { items: [] };
    return {
      items: [
        { recheck_id: "pending", job_name: "当前任务 A", status: "queued" },
      ],
      next_cursor: "active-next",
    };
  });
  render(
    <MemoryRouter>
      <ManagedRechecksTable context={context} />
    </MemoryRouter>,
  );
  expect(await screen.findByText("当前任务 A")).toBeInTheDocument();
  expect(screen.getByText("待执行")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  expect(await screen.findByText("暂无回检任务")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "历史记录" }));
  expect(await screen.findByText("历史任务")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "查看原会话" })).toHaveAttribute(
    "href",
    "/chat/chat-1",
  );
  const previousCalls = vi.mocked(request).mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(previousCalls + 1));
  expect(vi.mocked(request).mock.lastCall?.[0]).toContain("view=history&limit=20");
});

it("shows a failed request separately from an empty result", async () => {
  vi.mocked(request).mockRejectedValue(new Error("服务不可用"));
  render(
    <MemoryRouter>
      <ManagedRechecksTable context={context} />
    </MemoryRouter>,
  );
  expect(await screen.findByText("服务不可用")).toBeInTheDocument();
  expect(screen.queryByText("暂无回检任务")).not.toBeInTheDocument();
});
