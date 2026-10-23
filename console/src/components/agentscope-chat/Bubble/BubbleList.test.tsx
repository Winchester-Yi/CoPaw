import { fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import BubbleList from "./BubbleList";

vi.mock("@/components/agentscope-chat", () => ({
  useProviderContext: () => ({
    getPrefixCls: (name: string) => `swe-${name}`,
  }),
}));

vi.mock("./Bubble", () => ({
  default: ({ id }: { id?: string }) => <div data-bubble-id={id} />,
}));

vi.mock("./ScrollToBottom", () => ({
  default: () => null,
}));

vi.mock("./style/list", () => ({
  default: () => null,
}));

vi.mock("./hooks/usePaginationItemsData", () => ({
  usePaginationItems: (items: unknown[]) => ({
    items,
    noMore: true,
    loadMore: vi.fn(),
  }),
}));

describe("BubbleList", () => {
  it("reports reading history when a reverse list has negative scrollTop", () => {
    const onBottomStateChange = vi.fn();
    const ref = createRef<{
      getScrollElement: () => HTMLDivElement | null;
      scrollToBottom: () => void;
    }>();
    render(
      <BubbleList
        ref={ref}
        order="desc"
        pagination={false}
        onBottomStateChange={onBottomStateChange}
      />,
    );
    const element = ref.current!.getScrollElement()!;
    element.scrollTop = -300;
    fireEvent.scroll(element);
    expect(onBottomStateChange).toHaveBeenLastCalledWith(false);
    element.scrollTop = 0;
    fireEvent.scroll(element);
    expect(onBottomStateChange).toHaveBeenLastCalledWith(true);
  });
  it("places top content at the visual top of a reverse-ordered list", () => {
    render(
      <BubbleList
        items={[{ id: "message-1" }]}
        order="desc"
        pagination={false}
        topContent={<div data-testid="top-content">加载更早历史</div>}
      />,
    );

    const topContent = screen.getByTestId("top-content");
    expect(topContent.parentElement?.lastElementChild).toBe(topContent);
  });

  it("only auto-scrolls the initial render when requested by a chat timeline", () => {
    const ref = createRef<{
      getScrollElement: () => HTMLDivElement | null;
      scrollToBottom: () => void;
    }>();
    const rendered = render(
      <BubbleList
        autoScrollToBottom="initial"
        items={[{ id: "message-1" }]}
        order="desc"
        pagination={false}
        ref={ref}
      />,
    );
    const scrollElement = ref.current?.getScrollElement();
    expect(scrollElement).not.toBeNull();
    if (!scrollElement) return;
    scrollElement.scrollTop = -48;

    rendered.rerender(
      <BubbleList
        autoScrollToBottom="initial"
        items={[{ id: "message-1" }, { id: "message-2" }]}
        order="desc"
        pagination={false}
        ref={ref}
      />,
    );

    expect(scrollElement.scrollTop).toBe(-48);
  });

  it("reports that the reader is at the latest message after scrolling to bottom", () => {
    const onBottomStateChange = vi.fn();
    const ref = createRef<{
      getScrollElement: () => HTMLDivElement | null;
      scrollToBottom: () => void;
    }>();
    render(
      <BubbleList
        items={[{ id: "message-1" }]}
        onBottomStateChange={onBottomStateChange}
        order="desc"
        pagination={false}
        ref={ref}
      />,
    );
    onBottomStateChange.mockClear();

    ref.current?.scrollToBottom();

    expect(onBottomStateChange).toHaveBeenCalledWith(true);
  });
});
