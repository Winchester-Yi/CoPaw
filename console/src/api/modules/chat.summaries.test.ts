import { beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../request", () => ({ request: mocks.request }));
import { chatApi } from "./chat";

beforeEach(() => vi.clearAllMocks());

it("requests summaries with a cursor and no page number or exact total", async () => {
  const page = { items: [], has_more: false, page_size: 50, next_cursor: null };
  mocks.request.mockResolvedValue(page);
  expect(
    await chatApi.listChatSummaries({
      page_size: 50,
      cursor: "a+1",
      channel: "console",
    }),
  ).toBe(page);
  expect(mocks.request).toHaveBeenCalledWith(
    "/chats/-/summaries?page_size=50&cursor=a%2B1&channel=console",
  );
});

it("keeps the original complete chat page request", async () => {
  mocks.request.mockResolvedValue({ items: [], total: 0 });
  await chatApi.listChatsPage({ page_size: 50, page: 2 });
  expect(mocks.request).toHaveBeenCalledWith("/chats?page_size=50&page=2");
});
