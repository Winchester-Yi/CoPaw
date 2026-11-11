import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { request } from "../../../api/request";
import { useIframeStore } from "../../../stores/iframeStore";
import { resetInstitutionCache } from "../distribution/institutionCache";
import { useWealthStore } from "../store";
import { TargetPicker } from "./TargetPicker";

vi.mock("../../../api/request", () => ({ request: vi.fn() }));
const virtualWindow = vi.hoisted(() => ({
  start: 0,
  length: Infinity,
  scrollOffset: 0,
}));
// JSDOM has no layout; real virtual scrolling is verified separately in browser QA.
vi.mock("@tanstack/react-virtual", async () => {
  const { useEffect, useReducer } = await import("react");
  return {
    useVirtualizer: ({
      count,
      getScrollElement,
    }: {
      count: number;
      getScrollElement: () => HTMLElement | null;
    }) => {
      const [, update] = useReducer((value: number) => value + 1, 0);
      useEffect(() => {
        const element = getScrollElement();
        element?.addEventListener("scroll", update);
        return () => element?.removeEventListener("scroll", update);
      }, [getScrollElement]);
      return {
        scrollOffset: virtualWindow.scrollOffset,
        getTotalSize: () => count * 72,
        getVirtualItems: () =>
          Array.from({ length: count }, (_, index) => ({
            key: index,
            index,
            start: index * 72,
            end: (index + 1) * 72,
          })).slice(
            virtualWindow.start,
            virtualWindow.start + virtualWindow.length,
          ),
        scrollToIndex: vi.fn(),
      };
    },
  };
});
let peopleCount = 10;
beforeEach(() => {
  Object.assign(virtualWindow, { start: 0, length: Infinity, scrollOffset: 0 });
  peopleCount = 10;
  resetInstitutionCache();
  useIframeStore
    .getState()
    .setContext({ bbk: "110", orgCode: "110", orgLvl: "30", sysId: "retail" });
  useWealthStore.setState(
    {
      ...useWealthStore.getInitialState(),
      accountId: "middle",
      plansLoaded: true,
    },
    true,
  );
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1000);
  vi.mocked(request).mockImplementation(async (path) => ({
    code: "200",
    data: path.endsWith("bbkTree")
      ? {
          mOrgCode: "110",
          mOrgName: "北京分行",
          mOrgLvl: 30,
          children: [{ mOrgCode: "a", mOrgName: "测试支行", mOrgLvl: 70 }],
        }
      : Array.from({ length: peopleCount }, (_, i) => ({
          sapID: `000${i}`,
          userName: `人员${i}`,
          branchID: "110",
          subBranchID: "a",
          subBranchName: "测试支行",
        })),
  }));
});
afterEach(() => {
  cleanup();
  resetInstitutionCache();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function ready() {
  render(<TargetPicker />);
  fireEvent.click(await screen.findByRole("button", { name: "测试支行" }));
  await screen.findByRole("button", { name: "人员0 0000" });
  return within(
    screen.getByRole("group", { name: "分发目标列表" }),
  ).getAllByRole("button");
}
it.each([
  [0, "Home"],
  [0, "ArrowLeft"],
  [0, "ArrowUp"],
  [9, "End"],
  [9, "ArrowRight"],
  [9, "ArrowDown"],
])("卡片 %i 的边界键 %s 不应在搜索更新后抢焦点", async (index, key) => {
  const cards = await ready();
  act(() => cards[Number(index)].focus());
  fireEvent.keyDown(cards[Number(index)], { key });
  const search = screen.getByLabelText("搜索分发目标");
  act(() => search.focus());
  fireEvent.change(search, { target: { value: "人员" } });
  expect(search).toHaveFocus();
  fireEvent.change(search, { target: { value: "人员1" } });
  expect(search).toHaveFocus();
});
it("合法的 Home/End 和方向键导航仍能移动卡片焦点", async () => {
  const cards = await ready();
  act(() => cards[0].focus());
  fireEvent.keyDown(cards[0], { key: "End" });
  await waitFor(() => expect(cards[9]).toHaveFocus());
  fireEvent.keyDown(cards[9], { key: "Home" });
  await waitFor(() => expect(cards[0]).toHaveFocus());
  fireEvent.keyDown(cards[0], { key: "ArrowRight" });
  await waitFor(() => expect(cards[1]).toHaveFocus());
  fireEvent.keyDown(cards[1], { key: "ArrowDown" });
  await waitFor(() => expect(cards[6]).toHaveFocus());
});

it("原焦点卡片卸载后，在实际可见行保留入口且不抢搜索焦点", async () => {
  peopleCount = 60;
  const cards = await ready();
  act(() => cards[0].focus());
  const search = screen.getByLabelText("搜索分发目标");
  act(() => search.focus());
  const list = screen.getByRole("group", { name: "分发目标列表" });
  Object.assign(virtualWindow, { start: 4, length: 5, scrollOffset: 432 });
  fireEvent.scroll(list);
  expect(search).toHaveFocus();
  expect(screen.queryByRole("button", { name: "人员0 0000" })).toBeNull();
  const entry = screen.getByRole("button", { name: "人员30 00030" });
  expect(list.querySelectorAll('button[tabindex="0"]')).toHaveLength(1);
  expect(entry).toHaveAttribute("tabindex", "0");
  act(() => entry.focus());
  fireEvent.keyDown(entry, { key: "ArrowRight" });
  const next = screen.getByRole("button", { name: "人员31 00031" });
  expect(next).toHaveFocus();
  fireEvent.click(next);
  expect(useWealthStore.getState().targetSapIds).toEqual(["00031"]);
  act(() => search.focus());
  Object.assign(virtualWindow, { start: 0, length: 2, scrollOffset: 0 });
  fireEvent.scroll(list);
  expect(search).toHaveFocus();
  expect(screen.getByRole("button", { name: "人员0 0000" })).toHaveAttribute(
    "tabindex",
    "0",
  );
  expect(list.querySelectorAll('button[tabindex="0"]')).toHaveLength(1);
  expect(useWealthStore.getState().targetSapIds).toEqual(["00031"]);
});

it("原焦点卡片仍在渲染范围内时保留原键盘入口", async () => {
  const cards = await ready();
  act(() => cards[6].focus());
  Object.assign(virtualWindow, { start: 1, length: 1, scrollOffset: 72 });
  const list = screen.getByRole("group", { name: "分发目标列表" });
  fireEvent.scroll(list);
  expect(cards[6]).toHaveFocus();
  expect(cards[6]).toHaveAttribute("tabindex", "0");
  expect(list.querySelectorAll('button[tabindex="0"]')).toHaveLength(1);
});
